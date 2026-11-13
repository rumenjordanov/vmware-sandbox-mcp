import pytest

from vmware_sandbox_mcp.manager import SandboxError
from vmware_sandbox_mcp.vmrun import ForbiddenTargetError


def test_screenshot_returns_png_bytes(env):
    sandbox_id = env.manager.create()
    assert env.manager.screenshot(sandbox_id) == env.fake.screen
    assert env.fake.commands()[-1] == "captureScreen"


def test_screenshot_file_is_written_outside_vm_folders(env):
    sandbox_id = env.manager.create()
    env.manager.screenshot(sandbox_id)
    host_png = env.fake.calls[-1][-1]
    assert str(env.base_vmx.parent) not in host_png and str(env.clones) not in host_png


def test_non_png_result_is_an_error(env):
    sandbox_id = env.manager.create()
    env.fake.screen = b"not an image"
    with pytest.raises(SandboxError, match="valid screenshot"):
        env.manager.screenshot(sandbox_id)


def test_vmrun_failure_is_reported(env):
    sandbox_id = env.manager.create()
    env.fake.fail.add("captureScreen")
    with pytest.raises(SandboxError, match="Could not take a screenshot"):
        env.manager.screenshot(sandbox_id)


def test_screenshot_of_unknown_sandbox(env):
    with pytest.raises(SandboxError):
        env.manager.screenshot("sbx-deadbeef")
    assert env.fake.calls == []


def test_screenshot_of_base_or_other_vm_refused(env, tmp_path):
    for vmx in (env.base_vmx, env.other_vmx):
        with pytest.raises(ForbiddenTargetError):
            env.vmrun.capture_screen(vmx, tmp_path / "x.png")
    assert env.fake.calls == []
