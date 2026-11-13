import pytest

from vmware_sandbox_mcp.manager import MARKER, SandboxError

from conftest import BASE_VMX_TEXT


def test_create_clones_hardens_then_boots(env):
    sandbox_id = env.manager.create()
    vmx = env.clones / sandbox_id / f"{sandbox_id}.vmx"

    assert env.fake.commands() == ["clone", "start", "checkToolsState", "runProgramInGuest"]
    text = vmx.read_text(encoding="utf-8")
    assert 'isolation.tools.hgfs.disable = "TRUE"' in text
    assert 'ethernet0.present = "FALSE"' in text  # network defaults to none
    assert (vmx.parent / MARKER).is_file()
    assert env.manager.list() == [
        {"sandbox_id": sandbox_id, "network": "none", "status": "ready", "age_seconds": 0}
    ]


def test_create_with_nat(env):
    sandbox_id = env.manager.create("nat")
    text = (env.clones / sandbox_id / f"{sandbox_id}.vmx").read_text(encoding="utf-8")
    assert 'ethernet0.connectionType = "nat"' in text
    assert env.manager.list()[0]["network"] == "nat"


def test_create_rejects_unknown_network(env):
    with pytest.raises(SandboxError):
        env.manager.create("bridged")
    assert env.fake.calls == []


def test_destroy_stops_deletes_and_forgets(env):
    sandbox_id = env.manager.create()
    env.manager.destroy(sandbox_id)
    assert env.fake.commands()[-2:] == ["stop", "deleteVM"]
    assert not (env.clones / sandbox_id).exists()
    assert env.manager.list() == []


def test_destroy_unknown_or_malformed_id(env):
    for bad in ("sbx-deadbeef", "../Virtual Machines/other vm", "", "sbx-XYZ"):
        with pytest.raises(SandboxError):
            env.manager.destroy(bad)
    assert env.fake.calls == []


def test_failed_boot_is_cleaned_up(env):
    env.fake.tools_ready = False
    with pytest.raises(SandboxError, match="did not become ready"):
        env.manager.create()
    assert env.manager.list() == []
    assert [p for p in env.clones.iterdir() if p.is_dir()] == []
    assert env.fake.running == set()


def test_failed_clone_is_cleaned_up(env):
    env.fake.fail.add("clone")
    with pytest.raises(SandboxError, match="Could not create"):
        env.manager.create()
    assert env.manager.list() == []
    assert [p for p in env.clones.iterdir() if p.is_dir()] == []


def test_unmarked_folder_is_never_deleted(env):
    sandbox_id = env.manager.create()
    (env.clones / sandbox_id / MARKER).unlink()
    with pytest.raises(SandboxError, match="Refused to delete"):
        env.manager._remove_dir(sandbox_id)
    assert (env.clones / sandbox_id).is_dir()


def test_base_and_neighbour_untouched_by_full_lifecycle(env):
    sandbox_id = env.manager.create("nat")
    env.manager.destroy(sandbox_id)
    assert env.base_vmx.read_text(encoding="utf-8") == BASE_VMX_TEXT
    assert env.other_vmx.read_text(encoding="utf-8") == BASE_VMX_TEXT
    for argv in env.fake.calls:
        assert str(env.other_vmx) not in argv
        if str(env.base_vmx) in argv:
            assert "clone" in argv
