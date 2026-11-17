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
    for command in ("addSharedFolder", "reset", "suspend", "pause", "writeVariable", "upgradevm", "installTools"):
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


# ---- checkpoints: snapshots of a sandbox's own disk, never of the base VM ----------------------

CHECKPOINT_METHODS = ["take_checkpoint", "restore_checkpoint", "delete_checkpoint"]


@pytest.mark.parametrize("method", CHECKPOINT_METHODS)
def test_checkpoint_commands_refuse_the_base_vm_and_every_other_vm(env, method):
    for vmx in (env.base_vmx, env.other_vmx, env.clones / "loose.vmx", env.clones / "a" / "b" / "deep.vmx"):
        with pytest.raises(ForbiddenTargetError):
            getattr(env.vmrun, method)(vmx, "cp-1")
    assert env.fake.calls == []


@pytest.mark.parametrize("method", CHECKPOINT_METHODS)
def test_checkpoint_commands_work_on_a_sandbox_and_pass_one_argument_each(env, method):
    if method != "take_checkpoint":
        env.vmrun.take_checkpoint(clone_vmx(env), "cp-1")  # something to restore or delete
        env.fake.calls.clear()
    getattr(env.vmrun, method)(clone_vmx(env), "cp-1" if method != "take_checkpoint" else "cp-1")
    command = {"take_checkpoint": "snapshot", "restore_checkpoint": "revertToSnapshot",
               "delete_checkpoint": "deleteSnapshot"}[method]
    assert env.fake.calls[0] == ["vmrun", "-T", "ws", command, str(clone_vmx(env)), "cp-1"]


def test_listing_snapshots_is_allowed_for_the_base_vm_and_sandboxes_only(env):
    env.vmrun.list_checkpoints(clone_vmx(env))
    env.vmrun.list_snapshots()
    assert env.fake.commands() == ["listSnapshots", "listSnapshots"]
    env.fake.calls.clear()
    for vmx in (env.other_vmx, env.clones / "loose.vmx"):
        with pytest.raises(ForbiddenTargetError):
            env.vmrun.list_checkpoints(vmx)
    assert env.fake.calls == []


@pytest.mark.parametrize("name", [
    "", " ", "-rf", "--help", "a b", "a;b", "a/b", "a\\b", "x" * 41, "x\n", "é", "a\x00b", "a\"b", None, 5, ["a"],
])
def test_checkpoint_names_cannot_look_like_options_or_hold_odd_characters(env, name):
    for method in CHECKPOINT_METHODS:
        with pytest.raises(ForbiddenTargetError):
            getattr(env.vmrun, method)(clone_vmx(env), name)
    assert env.fake.calls == []


def test_soft_stop_is_a_stop_of_a_sandbox_only(env):
    env.vmrun.clone(clone_vmx(env), "Snapshot 1", "sbx-0a1b2c3d")
    env.vmrun.start(clone_vmx(env))
    env.vmrun.stop(clone_vmx(env), soft=True)
    assert env.fake.calls[-1][3:] == ["stop", str(clone_vmx(env)), "soft"]
    with pytest.raises(ForbiddenTargetError):
        env.vmrun.stop(env.base_vmx, soft=True)
