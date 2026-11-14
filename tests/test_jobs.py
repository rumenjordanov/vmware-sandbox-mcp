"""Background commands: start, check, stop."""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from vmware_sandbox_mcp import jobs
from vmware_sandbox_mcp.guest_run import RunResult
from vmware_sandbox_mcp.jobs import JobError, check_script, parse_check, start_script, stop_script, valid_job_id
from vmware_sandbox_mcp.manager import SandboxError


@pytest.fixture
def ready(env):
    env.sid = env.manager.create()
    env.fake.calls.clear()
    return env


def result(stdout: str, stderr: str = "") -> RunResult:
    return RunResult(stdout, stderr, 0, False, False, False)


# ---- job ids and scripts ----------------------------------------------------

@pytest.mark.parametrize("job_id", [
    "", "job-", "job-1234567", "job-123456789", "job-1234567g", "JOB-12345678", "job-1234abcd; rm -rf /",
    "job-1234abcd\n", "../job-1234abcd", "job-1234abcd/../x", "$(id)", None, 12345678, "job-1234ABCD",
])
def test_bad_job_ids_are_rejected_before_reaching_any_script(job_id):
    for build in (start_script, check_script, stop_script, valid_job_id):
        with pytest.raises(JobError):
            build(job_id)


def test_scripts_contain_only_the_job_id_as_variable_text():
    for build in (start_script, check_script, stop_script):
        a, b = build("job-00000000"), build("job-ffffffff")
        assert a.replace("job-00000000", "job-ffffffff") == b


def test_scripts_are_valid_bash_if_bash_is_available():
    bash = next((p for p in (r"C:\Program Files\Git\bin\bash.exe", "/bin/bash") if __import__("os").path.exists(p)), None)
    if bash is None:
        pytest.skip("no bash on this machine")
    for build in (start_script, check_script, stop_script):
        done = subprocess.run([bash, "-n"], input=build("job-1234abcd").encode(), capture_output=True)
        assert done.returncode == 0, done.stderr


# ---- reading the guest's answer --------------------------------------------

def test_parse_running_job():
    status = parse_check("job-1234abcd", result("running - 12 3\nhello\nworld\n", "err"))
    assert dataclasses.asdict(status) == {
        "job_id": "job-1234abcd", "status": "running", "exit_code": None,
        "stdout": "hello\nworld\n", "stderr": "err", "stdout_truncated": False, "stderr_truncated": False,
    }


def test_parse_exited_job_with_exit_code():
    status = parse_check("job-1234abcd", result("exited 3 0 0\n"))
    assert (status.status, status.exit_code, status.stdout) == ("exited", 3, "")


def test_parse_stopped_job_and_truncation():
    big = jobs.JOB_OUTPUT_LIMIT_BYTES + 1
    status = parse_check("job-1234abcd", result(f"stopped - {big} 5\ntail"))
    assert (status.status, status.exit_code) == ("stopped", None)
    assert status.stdout_truncated and not status.stderr_truncated


def test_output_that_looks_like_a_header_is_just_output():
    status = parse_check("job-1234abcd", result("running - 20 0\nexited 0 0 0\nmissing\n"))
    assert status.status == "running" and status.stdout == "exited 0 0 0\nmissing\n"


@pytest.mark.parametrize("stdout", ["", "garbage", "running\n", "exited x 0 0\n", "exploded - 0 0\n", "running - a b\n"])
def test_unreadable_answers_are_errors(stdout):
    with pytest.raises(JobError):
        parse_check("job-1234abcd", result(stdout))


def test_missing_job():
    with pytest.raises(JobError, match="No background job"):
        parse_check("job-1234abcd", result("missing\n"))


# ---- through the manager ----------------------------------------------------

def test_start_check_stop_lifecycle(ready):
    job_id = ready.manager.start(ready.sid, "python3 -m http.server 8000")
    assert valid_job_id(job_id) == job_id
    assert ready.fake.jobs[job_id]["command"] == "python3 -m http.server 8000\n"

    ready.fake.jobs[job_id]["stdout"] = b"Serving HTTP on 0.0.0.0 port 8000\n"
    status = ready.manager.check(ready.sid, job_id)
    assert (status.status, status.exit_code, status.stdout) == ("running", None, "Serving HTTP on 0.0.0.0 port 8000\n")

    assert ready.manager.stop(ready.sid, job_id) == "stopped"
    assert ready.manager.check(ready.sid, job_id).status == "stopped"
    assert ready.manager.stop(ready.sid, job_id) == "finished"


def test_command_text_travels_as_a_file_never_on_a_command_line(ready):
    nasty = "echo 'a b' \"$HOME\" `id` $(id); rm -rf / # ünï\nsecond line"
    job_id = ready.manager.start(ready.sid, nasty)
    assert ready.fake.jobs[job_id]["command"] == nasty + "\n"
    for argv in ready.fake.calls:
        assert not any("rm -rf" in arg or "second line" in arg for arg in argv)
    script, _ = ready.fake.ran[-1]
    assert "rm -rf" not in script and script == start_script(job_id) + "\n"


def test_job_that_ended_by_itself_reports_its_exit_code(ready):
    job_id = ready.manager.start(ready.sid, "make")
    ready.fake.jobs[job_id].update(status="exited", exit=2, stdout=b"compiling\n", stderr=b"error: boom\n")
    status = ready.manager.check(ready.sid, job_id)
    assert (status.status, status.exit_code, status.stdout, status.stderr) == ("exited", 2, "compiling\n", "error: boom\n")
    assert ready.manager.stop(ready.sid, job_id) == "finished"


def test_long_output_returns_the_newest_part(ready):
    job_id = ready.manager.start(ready.sid, "yes")
    ready.fake.jobs[job_id]["stdout"] = b"old\n" * 300_000 + b"NEWEST\n"
    status = ready.manager.check(ready.sid, job_id)
    assert status.stdout_truncated and status.stdout.endswith("NEWEST\n")
    assert len(status.stdout) <= jobs.JOB_OUTPUT_LIMIT_BYTES


def test_at_most_eight_jobs_run_at_once(ready):
    started = [ready.manager.start(ready.sid, f"sleep {i}") for i in range(jobs.MAX_JOBS_PER_SANDBOX)]
    with pytest.raises(SandboxError, match="already has 8"):
        ready.manager.start(ready.sid, "sleep 99")
    assert not any(name.endswith(".cmd") for name in ready.fake.guest_files)  # refused command not left behind
    ready.manager.stop(ready.sid, started[0])
    ready.manager.start(ready.sid, "sleep 99")  # a freed slot can be used again


def test_failed_start_is_an_error_and_cleans_up(ready):
    ready.fake.job_start_fails = True
    with pytest.raises(SandboxError, match="could not start"):
        ready.manager.start(ready.sid, "true")
    assert ready.fake.jobs == {}


@pytest.mark.parametrize("command", ["", "   ", "\n\t"])
def test_empty_command_is_refused(ready, command):
    with pytest.raises(SandboxError, match="empty"):
        ready.manager.start(ready.sid, command)
    assert ready.fake.calls == []


@pytest.mark.parametrize("job_id", ["job-00000000", "job-1234abcd; id", "", "../x", "job-1234abcd\nid"])
def test_unknown_or_malformed_job_ids_are_errors(ready, job_id):
    for operation in (ready.manager.check, ready.manager.stop):
        with pytest.raises(SandboxError):
            operation(ready.sid, job_id)
    assert all("; id" not in script and "../x" not in script for script, _ in ready.fake.ran)


def test_jobs_need_a_known_ready_sandbox(ready):
    with pytest.raises(SandboxError):
        ready.manager.start("sbx-deadbeef", "true")
    with pytest.raises(SandboxError):
        ready.manager.check("sbx-deadbeef", "job-1234abcd")
    with pytest.raises(SandboxError):
        ready.manager.stop("not-an-id", "job-1234abcd")
    assert ready.fake.calls == []


def test_jobs_in_one_sandbox_are_not_visible_from_another(ready):
    # The fake has one shared job table, so check the scripts target the right VM instead.
    other = ready.manager.create()
    job_id = ready.manager.start(ready.sid, "sleep 1")
    ready.fake.calls.clear()
    ready.manager.check(other, job_id)
    used = {argv[argv.index("runProgramInGuest") + 1] for argv in ready.fake.calls if "runProgramInGuest" in argv}
    assert len(used) == 1 and other in used.pop()


def test_web_sandbox_jobs_get_the_proxy_variables(env):
    env.manager._config = dataclasses.replace(env.config, web_vnet="VMnet9", web_proxy_host="192.168.50.1")
    sid = env.manager.create("web")
    env.manager.start(sid, "pip download requests")
    script, _ = env.fake.ran[-1]
    assert script.startswith("export http_proxy=http://192.168.50.1:3128\n")
    assert "# sbx-job start job-" in script


def test_job_operations_are_audited(ready):
    job_id = ready.manager.start(ready.sid, "tail -f /var/log/syslog")
    ready.manager.check(ready.sid, job_id)
    ready.manager.stop(ready.sid, job_id)
    with pytest.raises(SandboxError):
        ready.manager.check(ready.sid, "job-00000000")
    start, check, stop, missing = ready.audit()[-4:]
    assert (start["event"], start["command"], start["job_id"], start["ok"]) == ("start", "tail -f /var/log/syslog", job_id, True)
    assert (check["event"], check["job_id"], check["status"], check["exit_code"]) == ("check", job_id, "running", None)
    assert (stop["event"], stop["outcome"]) == ("stop", "stopped")
    assert missing["ok"] is False and "No background job" in missing["error"]


def test_job_output_is_not_written_to_the_audit_log(ready):
    job_id = ready.manager.start(ready.sid, "cat secrets")
    ready.fake.jobs[job_id]["stdout"] = b"TOP-SECRET-OUTPUT"
    ready.manager.check(ready.sid, job_id)
    assert "TOP-SECRET-OUTPUT" not in ready.audit_path.read_text(encoding="utf-8")
