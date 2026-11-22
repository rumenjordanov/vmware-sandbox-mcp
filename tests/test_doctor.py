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


# ---- network="web" checks ---------------------------------------------------

GOOD_WEB = {
    "public": "200", "private": "refused", "hostself": "refused", "badport": "refused",
    "port135": "closed", "port139": "closed", "port445": "closed", "port3128": "open", "route": "no",
    "unlisted": "refused",
}


class FakeProxy:
    def __init__(self, error=None):
        self.error, self.started, self.stopped = error, False, False

    def start(self):
        if self.error:
            raise self.error
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def webdoc(doc, monkeypatch):
    monkeypatch.setattr("vmware_sandbox_mcp.config.vnet_problem", lambda vnet: None)
    doc.settings["VMSANDBOX_WEB_VNET"] = "VMnet9"
    doc.settings["VMSANDBOX_WEB_PROXY_HOST"] = "192.168.50.1"
    doc.proxy = FakeProxy()

    def run_web(**seen):
        lines = "".join(f"{key}={value}\n" for key, value in {**GOOD_WEB, **seen}.items())
        doc.fake.next_result = (b"doctor-ok\nsudo-no\n" + lines.encode(), b"", 0)
        return doc.run(proxy_factory=lambda config: doc.proxy)

    doc.run_web = run_web
    return doc


def test_web_checks_are_skipped_when_web_is_not_configured(doc):
    _, results = doc.run()
    assert "Web proxy" not in results


def test_healthy_web_setup_passes(webdoc):
    code, results = webdoc.run_web()
    assert code == 0
    for title in ("Web proxy", "Web sandbox reaches the proxy", "Web access through the proxy",
                  "Proxy refuses private addresses and other ports", "Host is shielded from web sandboxes",
                  "No direct route out"):
        assert results[title] == "OK", title
    assert webdoc.proxy.started and webdoc.proxy.stopped
    assert not webdoc.fake.running
    assert [p.name for p in webdoc.clones.iterdir() if p.is_dir()] == []


def test_web_test_sandbox_uses_the_dedicated_network(webdoc):
    webdoc.run_web()
    assert any("export http_proxy=http://192.168.50.1:3128" in script for script, _ in webdoc.fake.ran)


@pytest.mark.parametrize("seen,failing", [
    ({"port445": "open"}, "Host is shielded from web sandboxes"),
    ({"port135": "open"}, "Host is shielded from web sandboxes"),
    ({"private": "200"}, "Proxy refuses private addresses and other ports"),
    ({"hostself": "403"}, "Proxy refuses private addresses and other ports"),
    ({"badport": ""}, "Proxy refuses private addresses and other ports"),
    ({"public": "refused"}, "Web access through the proxy"),
    ({"public": "502"}, "Web access through the proxy"),
    ({"public": ""}, "Web access through the proxy"),
    ({"route": "yes"}, "No direct route out"),
    ({"port3128": "closed"}, "Web sandbox reaches the proxy"),
])
def test_each_web_problem_is_reported(webdoc, seen, failing):
    code, results = webdoc.run_web(**seen)
    assert code == 1 and results[failing] == "FAIL"
    assert not webdoc.fake.running and webdoc.proxy.stopped


def test_missing_probe_output_counts_as_failure(webdoc):
    webdoc.fake.next_result = (b"doctor-ok\nsudo-no\n", b"", 0)
    code, results = webdoc.run(proxy_factory=lambda config: webdoc.proxy)
    assert code == 1 and results["Web sandbox reaches the proxy"] == "FAIL"


def test_proxy_address_not_on_this_computer_is_reported(webdoc):
    webdoc.proxy = FakeProxy(OSError(10049, "address not valid in its context"))
    code, results = webdoc.run_web()
    assert code == 1 and results["Web proxy"] == "FAIL"
    assert sum(1 for cmd in webdoc.fake.commands() if cmd == "clone") == 1  # only the plain boot test


def test_proxy_already_served_by_a_running_server_is_fine(webdoc):
    webdoc.proxy = FakeProxy(OSError(10048, "address already in use"))
    code, results = webdoc.run_web()
    assert code == 0 and results["Web proxy"] == "OK"


def test_the_site_list_is_checked_and_a_listed_site_is_the_one_tried(webdoc):
    webdoc.settings["VMSANDBOX_WEB_ALLOW"] = "*.debian.org, pypi.org"
    code, results = webdoc.run_web()
    assert code == 0 and results["Site list"] == "OK" and results["Web access through the proxy"] == "OK"
    probe = next(script for script, _ in webdoc.fake.ran if "ask public" in script)
    assert "ask public http://pypi.org/ pypi.org" in probe          # not example.com, which is not listed
    assert "allowlist-check.invalid" in probe


def test_a_site_that_is_not_on_the_list_but_got_through_is_a_failure(webdoc):
    webdoc.settings["VMSANDBOX_WEB_ALLOW"] = "pypi.org"
    code, results = webdoc.run_web(unlisted="200")
    assert code == 1 and results["Site list"] == "FAIL"


def test_a_list_of_only_wildcards_skips_the_site_to_try(webdoc):
    webdoc.settings["VMSANDBOX_WEB_ALLOW"] = "*.debian.org"
    code, results = webdoc.run_web()
    assert code == 0 and results["Web access through the proxy"] == "SKIP" and results["Site list"] == "OK"


def test_without_a_list_there_is_no_site_list_check_and_example_com_is_tried(webdoc):
    code, results = webdoc.run_web()
    assert "Site list" not in results
    assert any("ask public http://example.com/ example.com" in script for script, _ in webdoc.fake.ran)


def test_the_doctors_proxy_gets_the_list(webdoc):
    webdoc.settings["VMSANDBOX_WEB_ALLOW"] = "pypi.org"
    seen = []
    webdoc.fake.next_result = (b"doctor-ok\nsudo-no\n" + "".join(f"{k}={v}\n" for k, v in GOOD_WEB.items()).encode(), b"", 0)
    webdoc.run(proxy_factory=lambda config: (seen.append(config.web_allow), webdoc.proxy)[1])
    assert seen == [("pypi.org",)]


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
