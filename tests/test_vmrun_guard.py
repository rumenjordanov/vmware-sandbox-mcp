"""The hard rule: only the base VM (read / clone-from) and clones in the clones folder."""

import pytest

from vmware_sandbox_mcp.vmrun import ForbiddenTargetError, VmrunError


def clone_vmx(env, name="sbx-0a1b2c3d"):
    return env.clones / name / f"{name}.vmx"


@pytest.mark.parametrize("method", ["start", "stop", "delete_vm", "tools_running"])
def test_base_vm_is_never_started_stopped_or_deleted(env, method):
    with pytest.raises(ForbiddenTargetError):
        getattr(env.vmrun, method)(env.base_vmx)
    assert env.fake.calls == []


@pytest.mark.parametrize("method", ["start", "stop", "delete_vm", "tools_running"])
def test_other_vms_are_refused(env, method):
    with pytest.raises(ForbiddenTargetError):
        getattr(env.vmrun, method)(env.other_vmx)
    assert env.fake.calls == []


def test_guest_commands_refused_on_base_and_other(env):
    for vmx in (env.base_vmx, env.other_vmx):
        with pytest.raises(ForbiddenTargetError):
            env.vmrun.run_program(vmx, "/bin/true")
    assert env.fake.calls == []


def test_traversal_out_of_clones_folder_is_refused(env):
    sneaky = env.clones / "sbx-0a1b2c3d" / ".." / ".." / "Virtual Machines" / "other vm" / "other.vmx"
    with pytest.raises(ForbiddenTargetError):
        env.vmrun.start(sneaky)
    assert env.fake.calls == []


def test_wrong_depth_and_wrong_extension_are_refused(env):
    for bad in (
        env.clones / "loose.vmx",
        env.clones / "a" / "b" / "deep.vmx",
        env.clones / "sbx-0a1b2c3d" / "disk.vmdk",
    ):
        with pytest.raises(ForbiddenTargetError):
            env.vmrun.start(bad)
    assert env.fake.calls == []


def test_clone_destination_must_be_in_clones_folder(env):
    for dest in (env.other_vmx.parent / "evil.vmx", env.base_vmx.parent / "evil.vmx"):
        with pytest.raises(ForbiddenTargetError):
            env.vmrun.clone(dest, "Snapshot 1", "x")
    assert env.fake.calls == []


def test_clone_source_is_always_the_base(env):
    env.vmrun.clone(clone_vmx(env), "Snapshot 1", "sbx-0a1b2c3d")
    argv = env.fake.calls[0]
    assert argv[3:] == [
        "clone",
        str(env.base_vmx),
        str(clone_vmx(env)),
        "linked",
        "-snapshot=Snapshot 1",
        "-cloneName=sbx-0a1b2c3d",
    ]
    with pytest.raises(ForbiddenTargetError):
        env.vmrun._run("clone", env.other_vmx, str(clone_vmx(env, "sbx-11111111")), "linked")


def test_unknown_commands_are_refused(env):
    for command in ("snapshot", "revertToSnapshot", "deleteSnapshot", "addSharedFolder", "reset"):
        with pytest.raises(ForbiddenTargetError):
            env.vmrun._run(command, clone_vmx(env))
    assert env.fake.calls == []


def test_paths_with_spaces_are_single_arguments(env):
    env.vmrun.list_snapshots()
    argv = env.fake.calls[0]
    assert argv == ["vmrun", "-T", "ws", "listSnapshots", str(env.base_vmx)]
    assert " " in argv[-1]


def test_allowed_clone_operations_pass(env):
    vmx = clone_vmx(env)
    env.vmrun.clone(vmx, "Snapshot 1", "sbx-0a1b2c3d")
    env.vmrun.start(vmx)
    assert env.vmrun.tools_running(vmx)
    assert env.vmrun.run_program(vmx, "/bin/true") == 0
    env.vmrun.stop(vmx)
    env.vmrun.delete_vm(vmx)
    assert env.fake.commands() == [
        "clone", "start", "checkToolsState", "runProgramInGuest", "stop", "deleteVM",
    ]


def test_errors_never_contain_the_guest_password(env):
    env.fake.fail.add("start")
    with pytest.raises(VmrunError) as info:
        env.vmrun.start(clone_vmx(env))
    assert "s3cret-pw" not in str(info.value)
