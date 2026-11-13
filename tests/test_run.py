import pytest

from vmware_sandbox_mcp.guest_run import OUTPUT_LIMIT_BYTES, build_script
from vmware_sandbox_mcp.manager import SandboxError

NASTY = """echo "it's" `whoami` $(id) ; rm -rf / & | > < \\ %PATH% ^ "
second line"""


def test_run_returns_output_and_exit_code(env):
    sandbox_id = env.manager.create()
    env.fake.next_result = (b"hello\n", b"warn\n", 3)
    result = env.manager.run(sandbox_id, "echo hello", 20)
    assert (result.stdout, result.stderr, result.exit_code) == ("hello\n", "warn\n", 3)
    assert not result.timed_out and not result.stdout_truncated
    assert env.fake.ran == [("echo hello\n", 20)]


def test_command_text_never_reaches_a_command_line(env):
    sandbox_id = env.manager.create()
    env.manager.run(sandbox_id, NASTY, 5)
    assert env.fake.ran[0][0] == NASTY + "\n"  # arrived intact, as file content
    for argv in env.fake.calls:
        for arg in argv:
            assert "whoami" not in arg and "rm -rf" not in arg


def test_guest_program_arguments_are_server_generated_only(env):
    sandbox_id = env.manager.create()
    env.manager.run(sandbox_id, NASTY, 5)
    run_calls = [a for a in env.fake.calls if "runProgramInGuest" in a and "/bin/true" not in a]
    program_args = run_calls[0][run_calls[0].index("runProgramInGuest") + 2 :]
    assert program_args[0] == "/bin/bash" and program_args[3] == "5"
    assert all(" " not in a and '"' not in a for a in program_args)


def test_wrapper_extracts_exactly_the_command(env):
    script = build_script("line one\nline two").decode()
    lines = script.split("\n")
    marker = next(l for l in lines if l.startswith("tail -n +"))
    first = int(marker.split("+")[1].split()[0])
    assert "\n".join(lines[first - 1 :]) == "line one\nline two\n"


def test_timeout_reported(env):
    sandbox_id = env.manager.create()
    env.fake.next_result = (b"partial", b"", 124)
    result = env.manager.run(sandbox_id, "sleep 99", 1)
    assert result.timed_out and result.exit_code == 124 and result.stdout == "partial"


def test_exit_137_without_elapsed_time_is_not_a_timeout(env):
    sandbox_id = env.manager.create()
    env.fake.next_result = (b"", b"Killed", 137)
    assert not env.manager.run(sandbox_id, "x", 60).timed_out


def test_truncation_flag(env):
    sandbox_id = env.manager.create()
    env.fake.next_result = (b"x" * (OUTPUT_LIMIT_BYTES + 10), b"", 0)
    result = env.manager.run(sandbox_id, "yes", 5)
    assert result.stdout_truncated and len(result.stdout) == OUTPUT_LIMIT_BYTES


def test_non_utf8_output_does_not_crash(env):
    sandbox_id = env.manager.create()
    env.fake.next_result = (b"\xff\xfe ok", b"", 0)
    assert "ok" in env.manager.run(sandbox_id, "x", 5).stdout


@pytest.mark.parametrize("timeout", [0, -1, 3601])
def test_timeout_bounds(env, timeout):
    sandbox_id = env.manager.create()
    with pytest.raises(SandboxError, match="timeout_seconds"):
        env.manager.run(sandbox_id, "true", timeout)


def test_run_on_unknown_sandbox(env):
    with pytest.raises(SandboxError):
        env.manager.run("sbx-deadbeef", "true", 5)
    assert env.fake.calls == []


def test_hung_guest_gives_clear_error_and_cleans_guest_files(env):
    sandbox_id = env.manager.create()
    env.fake.hang = True
    with pytest.raises(SandboxError, match="stopped responding"):
        env.manager.run(sandbox_id, "sleep 1000", 5)
    assert env.fake.guest_files == {}


def test_guest_temp_files_removed_after_run(env):
    sandbox_id = env.manager.create()
    env.manager.run(sandbox_id, "true", 5)
    assert env.fake.guest_files == {}
