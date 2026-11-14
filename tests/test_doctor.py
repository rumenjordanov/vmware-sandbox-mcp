"""The doctor command: every check, against the fake vmrun process."""

from __future__ import annotations

import pytest

from vmware_sandbox_mcp import server
from vmware_sandbox_mcp.doctor import Doctor, snapshot_powered_on
from vmware_sandbox_mcp.manager import SandboxError

VMSD = """.encoding = "UTF-8"
snapshot0.uid = "1"
snapshot0.filename = "Debian 13.x 64-bit-Snapshot1.vmsn"
snapshot0.displayName = "Snapshot 1"
snapshot1.uid = "2"
snapshot1.filename = "Debian 13.x 64-bit-Snapshot2.vmsn"
snapshot1.displayName = "live"
snapshot1.type = "1"
"""


@pytest.fixture
def doc(env, tmp_path):
    vmrun_exe = tmp_path / "vmrun.exe"
    vmrun_exe.write_text("", encoding="utf-8")
    env.base_vmx.with_suffix(".vmsd").write_text(VMSD, encoding="utf-8")
    env.fake.next_result = (b"doctor-ok\nsudo-no\n", b"", 0)
    env.settings = {
        "VMSANDBOX_BASE_VMX": str(env.base_vmx),
        "VMSANDBOX_SNAPSHOT": "Snapshot 1",
        "VMSANDBOX_CLONES_DIR": str(env.clones),
        "VMSANDBOX_TRANSFER_DIR": str(env.config.transfer_dir),
        "VMSANDBOX_GUEST_USER": "user",
        "VMSANDBOX_GUEST_PASSWORD": "s3cret-pw",
        "VMSANDBOX_VMRUN_PATH": str(vmrun_exe),
        "VMSANDBOX_BOOT_TIMEOUT_SECONDS": "60",
    }
    env.lines = []

    def run(**kwargs):
        doctor = Doctor(
            env.settings,
            runner=env.fake,
            out=env.lines.append,
            clock=lambda: env.now[0],
            sleep=lambda s: env.now.__setitem__(0, env.now[0] + s),
            **kwargs,
        )
        code = doctor.run()
        return code, dict((title, status) for status, title in doctor.results)

    env.run = run
    return env


def test_healthy_setup_passes_and_leaves_nothing_behind(doc):
    code, results = doc.run()
    assert code == 0
    assert results["Sandbox boot test"] == "OK"
    assert results["Running commands"] == "OK"
    assert results["Cleanup"] == "OK"
    assert results["Installing software"] == "WARN"  # no sudo is a warning, not a failure
    assert not doc.fake.running
    assert [p.name for p in doc.clones.iterdir() if p.is_dir()] == []
    assert not list(doc.config.transfer_dir.iterdir())


def test_sudo_available_is_reported(doc):
    doc.fake.next_result = (b"doctor-ok\nsudo-yes\n", b"", 0)
    code, results = doc.run()
    assert code == 0 and results["Installing software"] == "OK"


def test_missing_setting_stops_early(doc):
    del doc.settings["VMSANDBOX_GUEST_PASSWORD"]
    code, results = doc.run()
    assert code == 1
    assert results == {"Settings": "FAIL"}
    assert any("VMSANDBOX_GUEST_PASSWORD" in line for line in doc.lines)
    assert doc.fake.calls == []


def test_overlapping_folders_are_reported(doc):
    doc.settings["VMSANDBOX_TRANSFER_DIR"] = str(doc.clones / "inside")
    code, results = doc.run()
    assert code == 1 and results == {"Settings": "FAIL"}


def test_wrong_snapshot_name_lists_the_real_ones_and_skips_boot(doc):
    doc.settings["VMSANDBOX_SNAPSHOT"] = "clean"
    code, results = doc.run()
    assert code == 1
    assert results["Snapshot"] == "FAIL"
    assert results["Sandbox boot test"] == "SKIP"
    assert any("'Snapshot 1'" in line for line in doc.lines)
    assert "clone" not in doc.fake.commands()


def test_snapshot_taken_while_running_is_rejected(doc):
    (doc.base_vmx.parent / "Debian 13.x 64-bit-Snapshot1.vmem").write_bytes(b"")
    code, results = doc.run()
    assert code == 1 and results["Snapshot"] == "FAIL"
    assert "clone" not in doc.fake.commands()


def test_snapshot_powered_on_detection(doc):
    assert snapshot_powered_on(doc.base_vmx, "Snapshot 1") is False
    assert snapshot_powered_on(doc.base_vmx, "live") is True
    assert snapshot_powered_on(doc.base_vmx, "unknown") is None


def test_vmrun_failure_is_reported(doc):
    doc.fake.fail.add("list")
    code, results = doc.run()
    assert code == 1 and results["vmrun"] == "FAIL"
    assert results["Sandbox boot test"] == "SKIP"


def test_wrong_password_fails_fast_and_cleans_up(doc):
    doc.fake.bad_login = True
    started = doc.now[0]
    code, results = doc.run()
    assert code == 1
    assert results["Sandbox boot test"] == "FAIL"
    assert any("VMSANDBOX_GUEST_PASSWORD" in line for line in doc.lines)
    assert doc.now[0] - started < 30  # not the whole 60 s boot timeout
    assert not doc.fake.running
    assert [p.name for p in doc.clones.iterdir() if p.is_dir()] == []


def test_password_never_appears_in_output(doc):
    doc.fake.bad_login = True
    doc.run()
    doc.fake.bad_login = False
    doc.fake.fail.add("start")
    doc.run()
    assert "s3cret-pw" not in "\n".join(doc.lines)


def test_no_boot_flag_creates_nothing(doc):
    code, results = doc.run(boot=False)
    assert code == 0
    assert results["Sandbox boot test"] == "SKIP"
    assert "clone" not in doc.fake.commands()


def test_doctor_only_reads_the_base_vm(doc):
    doc.run()
    base = str(doc.base_vmx)
    for argv in doc.fake.calls:
        if base in argv:
            assert argv[3] in ("listSnapshots", "clone")


def test_create_reports_rejected_login_quickly(env):
    env.fake.bad_login = True
    started = env.now[0]
    with pytest.raises(SandboxError, match="rejected the guest login"):
        env.manager.create()
    assert env.now[0] - started < env.config.boot_timeout_seconds
    assert env.manager.list() == []


def test_cli_dispatches_to_doctor(monkeypatch):
    seen = {}
    monkeypatch.setattr("vmware_sandbox_mcp.doctor.main", lambda argv: seen.setdefault("argv", argv) and 0 or 0)
    monkeypatch.setattr("sys.argv", ["vmware-sandbox-mcp", "doctor", "--no-boot"])
    with pytest.raises(SystemExit) as exit_info:
        server.main()
    assert exit_info.value.code == 0 and seen["argv"] == ["--no-boot"]


def test_cli_rejects_unknown_arguments(monkeypatch):
    monkeypatch.setattr("sys.argv", ["vmware-sandbox-mcp", "bogus"])
    with pytest.raises(SystemExit) as exit_info:
        server.main()
    assert exit_info.value.code == 2
