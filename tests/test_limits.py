import dataclasses
import json

import pytest

from vmware_sandbox_mcp.manager import MARKER, SandboxError, SandboxManager
from vmware_sandbox_mcp.state import StateStore

from conftest import BASE_VMX_TEXT


def folders(env):
    return sorted(p.name for p in env.clones.iterdir() if p.is_dir())


def restarted(env) -> SandboxManager:
    """A new server process looking at the same clones folder."""
    return SandboxManager(env.config, env.vmrun, StateStore(env.clones), clock=lambda: env.now[0], sleep=lambda s: None)


def make_folder(env, name, marker=True):
    folder = env.clones / name
    folder.mkdir()
    (folder / f"{name}.vmx").write_text(BASE_VMX_TEXT, encoding="utf-8")
    if marker:
        (folder / MARKER).write_text("x")
    return folder


# ---- cap ---------------------------------------------------------------

def test_cap_on_simultaneous_sandboxes(env):
    ids = [env.manager.create() for _ in range(3)]
    before = len(env.fake.calls)
    with pytest.raises(SandboxError, match="limit of 3"):
        env.manager.create()
    assert len(env.fake.calls) == before  # refused before touching VMware
    assert len(folders(env)) == 3

    env.manager.destroy(ids[0])
    env.manager.create()
    assert len(env.manager.list()) == 3


def test_cap_is_configurable(env):
    manager = SandboxManager(
        dataclasses.replace(env.config, max_sandboxes=1), env.vmrun, StateStore(env.clones),
        clock=lambda: env.now[0], sleep=lambda s: None,
    )
    manager.create()
    with pytest.raises(SandboxError, match="limit of 1"):
        manager.create()


# ---- maximum age -------------------------------------------------------

def test_expired_sandboxes_are_destroyed(env):
    old = env.manager.create()
    env.now[0] += 60 * 60
    young = env.manager.create()
    env.now[0] += 61 * 60  # old is 121 min, young is 61 min; limit is 120

    assert env.manager.reap_expired() == [old]
    assert [s["sandbox_id"] for s in env.manager.list()] == [young]
    assert folders(env) == [young]
    assert len(env.fake.running) == 1


def test_create_reaps_expired_first_so_the_cap_frees_up(env):
    for _ in range(3):
        env.manager.create()
    env.now[0] += 121 * 60
    fresh = env.manager.create()
    assert [s["sandbox_id"] for s in env.manager.list()] == [fresh]


def test_nothing_reaped_before_max_age(env):
    env.manager.create()
    env.now[0] += 119 * 60
    assert env.manager.reap_expired() == []


# ---- orphans at startup ------------------------------------------------

def test_untracked_marked_folder_is_removed(env):
    make_folder(env, "sbx-aaaaaaaa")
    assert restarted(env).cleanup_orphans() == ["sbx-aaaaaaaa"]
    assert folders(env) == []


def test_unknown_folders_are_left_alone(env):
    make_folder(env, "sbx-bbbbbbbb", marker=False)  # looks like ours but no marker
    make_folder(env, "My Other VM")                 # marker but not our naming
    (env.clones / "notes.txt").write_text("keep")
    assert restarted(env).cleanup_orphans() == []
    assert folders(env) == ["My Other VM", "sbx-bbbbbbbb"]
    assert (env.clones / "notes.txt").exists()
    assert env.fake.commands() == ["list"]  # read-only


def test_ready_sandbox_that_is_no_longer_running_is_removed(env):
    alive = env.manager.create()
    dead = env.manager.create()
    env.fake.running.discard(str(env.clones / dead / f"{dead}.vmx"))  # e.g. host rebooted

    assert restarted(env).cleanup_orphans() == [dead]
    assert [s["sandbox_id"] for s in env.manager.list()] == [alive]
    assert folders(env) == [alive]


def test_state_entry_without_folder_is_dropped(env):
    with StateStore(env.clones).edit() as sandboxes:
        sandboxes["sbx-cccccccc"] = {"network": "none", "created_at": env.now[0], "status": "ready"}
    assert restarted(env).cleanup_orphans() == ["sbx-cccccccc"]
    assert env.manager.list() == []


def test_sandbox_being_created_by_another_server_is_left_alone(env):
    make_folder(env, "sbx-dddddddd")
    with StateStore(env.clones).edit() as sandboxes:
        sandboxes["sbx-dddddddd"] = {"network": "none", "created_at": env.now[0] - 20, "status": "creating"}
    assert restarted(env).cleanup_orphans() == []
    assert folders(env) == ["sbx-dddddddd"]


def test_stuck_creating_sandbox_is_removed(env):
    make_folder(env, "sbx-eeeeeeee")
    with StateStore(env.clones).edit() as sandboxes:
        sandboxes["sbx-eeeeeeee"] = {"network": "none", "created_at": env.now[0] - 3600, "status": "creating"}
    assert restarted(env).cleanup_orphans() == ["sbx-eeeeeeee"]
    assert folders(env) == []


def test_tampered_state_cannot_direct_deletion_elsewhere(env):
    victim = env.other_vmx.parent
    (env.clones / "state.json").write_text(json.dumps({"sandboxes": {
        "../Virtual Machines/other vm": {"network": "none", "created_at": 0, "status": "ready"},
        "sbx-ffffffff": {"network": "none", "created_at": 0, "status": "ready"},
    }}))
    manager = restarted(env)
    manager.cleanup_orphans()
    manager.reap_expired()
    assert victim.is_dir() and env.other_vmx.is_file() and env.base_vmx.is_file()
    for argv in env.fake.calls:
        assert str(env.other_vmx) not in argv and str(env.base_vmx) not in argv


def test_cleanup_never_touches_base_or_neighbour(env):
    env.manager.create()
    env.fake.running.clear()
    make_folder(env, "sbx-12345678")
    restarted(env).cleanup_orphans()
    assert env.base_vmx.read_text(encoding="utf-8") == BASE_VMX_TEXT
    assert env.other_vmx.read_text(encoding="utf-8") == BASE_VMX_TEXT
