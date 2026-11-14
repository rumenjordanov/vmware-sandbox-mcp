"""The audit log: one entry per operation, successes and refusals alike."""

from __future__ import annotations

import pytest

from vmware_sandbox_mcp.audit import AuditLog
from vmware_sandbox_mcp.config import ConfigError, load_config
from vmware_sandbox_mcp.manager import SandboxError


def test_full_session_is_recorded_in_order(env):
    (env.config.transfer_dir / "in.txt").write_text("hello", encoding="utf-8")
    env.fake.next_result = (b"hi\n", b"oops", 3)
    sandbox_id = env.manager.create("nat")
    env.manager.run(sandbox_id, "echo hi; exit 3", 30)
    env.manager.copy_in(sandbox_id, "in.txt", "/tmp/in.txt")
    env.fake.guest_files["/tmp/out.txt"] = b"result"
    env.manager.copy_out(sandbox_id, "/tmp/out.txt", "out.txt")
    env.manager.screenshot(sandbox_id)
    env.manager.destroy(sandbox_id)

    entries = env.audit()
    assert [e["event"] for e in entries] == ["create", "run", "copy_in", "copy_out", "screenshot", "destroy"]
    assert all(e["ok"] and e["sandbox_id"] == sandbox_id for e in entries)
    assert all(e["time"].endswith("Z") and "seconds" in e for e in entries)
    create, run, copy_in, copy_out, shot, _ = entries
    assert create["network"] == "nat"
    assert run["command"] == "echo hi; exit 3" and run["timeout_seconds"] == 30
    assert (run["exit_code"], run["timed_out"], run["stdout_bytes"], run["stderr_bytes"]) == (3, False, 3, 4)
    assert copy_in["host_path"] == "in.txt" and copy_in["guest_path"] == "/tmp/in.txt" and copy_in["bytes"] == 5
    assert copy_out["saved_to"].endswith("out.txt") and copy_out["bytes"] == 6
    assert shot["bytes"] == len(env.fake.screen)


def test_command_output_is_not_recorded(env):
    env.fake.next_result = (b"TOP-SECRET-OUTPUT", b"SECRET-ERR", 0)
    sandbox_id = env.manager.create()
    env.manager.run(sandbox_id, "cat secrets")
    text = env.audit_path.read_text(encoding="utf-8")
    assert "TOP-SECRET-OUTPUT" not in text and "SECRET-ERR" not in text


def test_guest_password_never_reaches_the_log(env):
    sandbox_id = env.manager.create()
    env.manager.run(sandbox_id, "true")
    env.fake.fail.add("CopyFileFromHostToGuest")
    with pytest.raises(SandboxError):
        env.manager.run(sandbox_id, "true")
    env.fake.fail.add("start")
    with pytest.raises(SandboxError):
        env.manager.create()
    assert "s3cret-pw" not in env.audit_path.read_text(encoding="utf-8")


def test_refused_operations_are_recorded_with_the_reason(env):
    sandbox_id = env.manager.create()
    with pytest.raises(SandboxError):
        env.manager.copy_out(sandbox_id, "/etc/passwd", "../escape.txt")
    with pytest.raises(SandboxError):
        env.manager.run("sbx-00000000", "id")
    with pytest.raises(SandboxError):
        env.manager.create("bridged")

    escape, unknown, bad_network = env.audit()[1:]
    assert escape["event"] == "copy_out" and not escape["ok"] and escape["host_path"] == "../escape.txt"
    assert "transfer folder" in escape["error"]
    assert unknown["event"] == "run" and not unknown["ok"] and unknown["command"] == "id"
    assert bad_network["event"] == "create" and not bad_network["ok"] and bad_network["network"] == "bridged"


def test_cap_refusal_is_recorded(env):
    for _ in range(env.config.max_sandboxes):
        env.manager.create()
    with pytest.raises(SandboxError):
        env.manager.create()
    last = env.audit()[-1]
    assert last["event"] == "create" and not last["ok"] and "limit" in last["error"]


def test_automatic_destruction_is_recorded(env):
    sandbox_id = env.manager.create()
    env.now[0] += env.config.max_age_minutes * 60 + 1
    assert env.manager.reap_expired() == [sandbox_id]
    last = env.audit()[-1]
    assert (last["event"], last["sandbox_id"], last["reason"], last["ok"]) == ("auto_destroy", sandbox_id, "expired", True)


def test_orphan_cleanup_is_recorded(env):
    sandbox_id = env.manager.create()
    env.fake.running.clear()  # as after a host reboot
    assert env.manager.cleanup_orphans() == [sandbox_id]
    last = env.audit()[-1]
    assert last["event"] == "auto_destroy" and last["reason"].startswith("orphaned")


def test_multiline_and_unicode_commands_stay_one_line_each(env):
    sandbox_id = env.manager.create()
    command = 'echo "línea uno"\necho "二"\n'
    env.manager.run(sandbox_id, command)
    assert len(env.audit_path.read_text(encoding="utf-8").splitlines()) == 2
    assert env.audit()[-1]["command"] == command


def test_listing_is_not_logged(env):
    env.manager.list()
    assert env.audit() == []


def test_unwritable_log_does_not_block_operations(env, tmp_path):
    env.manager._audit = AuditLog(tmp_path / "missing-folder" / "audit.jsonl")
    sandbox_id = env.manager.create()
    env.manager.destroy(sandbox_id)
    assert env.manager.list() == []


def test_no_path_means_no_log(tmp_path):
    AuditLog(None).record("create", ok=True)  # must not raise
    assert list(tmp_path.iterdir()) == []


# ---- where the log may live ------------------------------------------------

@pytest.fixture
def settings(env, tmp_path):
    vmrun_exe = tmp_path / "vmrun.exe"
    vmrun_exe.write_text("", encoding="utf-8")
    return {
        "VMSANDBOX_BASE_VMX": str(env.base_vmx),
        "VMSANDBOX_SNAPSHOT": "Snapshot 1",
        "VMSANDBOX_CLONES_DIR": str(env.clones),
        "VMSANDBOX_TRANSFER_DIR": str(env.config.transfer_dir),
        "VMSANDBOX_GUEST_USER": "user",
        "VMSANDBOX_GUEST_PASSWORD": "s3cret-pw",
        "VMSANDBOX_VMRUN_PATH": str(vmrun_exe),
    }


def test_default_location_is_the_clones_folder(env, settings):
    assert load_config(settings).audit_log == env.clones / "audit.jsonl"


def test_custom_location_is_used_and_its_folder_created(settings, tmp_path):
    settings["VMSANDBOX_AUDIT_LOG"] = str(tmp_path / "logs" / "sandbox audit.jsonl")
    assert load_config(settings).audit_log == tmp_path / "logs" / "sandbox audit.jsonl"
    assert (tmp_path / "logs").is_dir()


@pytest.mark.parametrize("where", ["transfer", "transfer-sub", "base", "sandbox", "state", "folder"])
def test_unsafe_locations_are_refused(env, settings, where):
    settings["VMSANDBOX_AUDIT_LOG"] = str({
        "transfer": env.config.transfer_dir / "audit.jsonl",
        "transfer-sub": env.config.transfer_dir / "a" / ".." / "b" / "audit.jsonl",
        "base": env.base_vmx.parent / "audit.jsonl",
        "sandbox": env.clones / "sbx-0123abcd" / "audit.jsonl",
        "state": env.clones / "state.json",
        "folder": env.clones,
    }[where])
    with pytest.raises(ConfigError, match="VMSANDBOX_AUDIT_LOG"):
        load_config(settings)


def test_guest_password_is_blanked_wherever_it_would_be_written(tmp_path):
    path = tmp_path / "a.jsonl"
    log = AuditLog(path, redact=('pa"ss\\word', "", "hunter2"))
    log.record("type", text='login: pa"ss\\word then hunter2', keys=3)
    log.record("run", command="echo hunter2 | sudo -S true")
    text = path.read_text(encoding="utf-8")
    assert "hunter2" not in text and 'pa\\"ss' not in text
    import json as _json
    entries = [_json.loads(line) for line in text.splitlines()]  # still valid JSON
    assert entries[0]["text"] == "login: [guest password] then [guest password]"
    assert entries[1]["command"] == "echo [guest password] | sudo -S true"


def test_blanking_never_touches_numbers(tmp_path):
    path = tmp_path / "a.jsonl"
    AuditLog(path, redact=("12345",)).record("copy_in", bytes=12345, seconds=1.2345, host_path="f12345.txt", ok=True)
    import json as _json
    entry = _json.loads(path.read_text(encoding="utf-8"))
    assert entry["bytes"] == 12345 and entry["seconds"] == 1.2345
    assert entry["host_path"] == "f[guest password].txt"
