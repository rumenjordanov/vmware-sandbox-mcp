"""Checkpoints: save a sandbox's disk, roll back to it."""

from __future__ import annotations

import pytest

from vmware_sandbox_mcp.manager import MAX_CHECKPOINTS, SandboxError

STEPS = ("stop", "snapshot", "revertToSnapshot", "deleteSnapshot", "start")


@pytest.fixture
def sb(env):
    env.sid = env.manager.create()
    env.vmx = str(env.clones / env.sid / f"{env.sid}.vmx")
    env.fake.calls.clear()
    return env


def steps(env):
    """The power and snapshot steps vmrun was asked for, in order."""
    return [c for c in env.fake.commands() if c in STEPS]


def stop_arg(env):
    return next(argv[-1] for argv in env.fake.calls if "stop" in argv)


def test_saving_stops_cleanly_snapshots_while_powered_off_and_starts_again(sb):
    done = sb.manager.checkpoint(sb.sid, "before-install")
    assert steps(sb) == ["stop", "snapshot", "start"]
    assert stop_arg(sb) == "soft"                      # a clean shutdown, so the saved disk is consistent
    assert sb.fake.snapshot_taken_while_running == [False]
    assert (done.name, done.replaced, done.checkpoints) == ("before-install", False, ["before-install"])
    assert sb.vmx in sb.fake.running                    # and it is running again
    entry = sb.manager.list()[0]
    assert (entry["status"], entry["checkpoints"]) == ("ready", ["before-install"])
    assert sb.manager.run(sb.sid, "true").exit_code == 0   # and usable


def test_restoring_brings_back_files_and_stops_programs(sb):
    sb.fake.guest_files["/tmp/a.txt"] = b"original"
    sb.manager.checkpoint(sb.sid, "start-point")
    sb.fake.guest_files["/tmp/a.txt"] = b"changed"
    sb.fake.guest_files["/tmp/b.txt"] = b"new file"
    job = sb.manager.start(sb.sid, "sleep 600")
    sb.fake.calls.clear()

    sb.manager.restore(sb.sid, "start-point")

    assert steps(sb) == ["stop", "revertToSnapshot", "start"]
    assert stop_arg(sb) == "hard"                       # what is running is being thrown away anyway
    assert sb.fake.guest_files["/tmp/a.txt"] == b"original"
    assert "/tmp/b.txt" not in sb.fake.guest_files
    assert sb.vmx in sb.fake.running
    with pytest.raises(SandboxError, match="No background job"):
        sb.manager.check(sb.sid, job)                   # the program did not survive


def test_a_checkpoint_survives_being_restored_and_can_be_restored_again(sb):
    sb.fake.guest_files["/tmp/a.txt"] = b"v1"
    sb.manager.checkpoint(sb.sid, "one")
    for attempt in range(3):
        sb.fake.guest_files["/tmp/a.txt"] = f"junk {attempt}".encode()
        assert sb.manager.restore(sb.sid, "one") == ["one"]
        assert sb.fake.guest_files["/tmp/a.txt"] == b"v1"


def test_saving_under_an_existing_name_replaces_that_checkpoint(sb):
    sb.fake.guest_files["/tmp/a.txt"] = b"v1"
    sb.manager.checkpoint(sb.sid, "good")
    sb.fake.guest_files["/tmp/a.txt"] = b"v2"
    sb.fake.calls.clear()
    done = sb.manager.checkpoint(sb.sid, "good")
    assert steps(sb) == ["stop", "deleteSnapshot", "snapshot", "start"]
    assert (done.replaced, done.checkpoints) == (True, ["good"])
    sb.fake.guest_files["/tmp/a.txt"] = b"v3"
    sb.manager.restore(sb.sid, "good")
    assert sb.fake.guest_files["/tmp/a.txt"] == b"v2"   # the newer save


def test_a_sandbox_holds_a_limited_number_of_checkpoints(sb):
    names = [f"cp{i}" for i in range(MAX_CHECKPOINTS)]
    for name in names:
        sb.manager.checkpoint(sb.sid, name)
    sb.fake.calls.clear()
    with pytest.raises(SandboxError, match=f"already holds {MAX_CHECKPOINTS}"):
        sb.manager.checkpoint(sb.sid, "one-too-many")
    assert steps(sb) == []                              # refused before anything was touched
    assert sb.manager.list()[0]["status"] == "ready"
    assert sb.manager.checkpoint(sb.sid, "cp0").replaced is True   # replacing is still allowed
    assert sb.manager.list()[0]["checkpoints"] == names[1:] + ["cp0"]   # listed oldest first; cp0 is newest now


@pytest.mark.parametrize("name", [
    "", " ", "-x", "a b", "a" * 41, "x/y", "x\n", "é", "..", "a;b", None, 5, ["a"],
])
def test_bad_checkpoint_names_are_refused_before_anything_happens(sb, name):
    with pytest.raises(SandboxError, match="checkpoint name"):
        sb.manager.checkpoint(sb.sid, name)
    with pytest.raises(SandboxError, match="checkpoint name"):
        sb.manager.restore(sb.sid, name)
    assert sb.fake.calls == []


def test_restoring_an_unknown_checkpoint_says_what_exists_and_touches_nothing(sb):
    sb.manager.checkpoint(sb.sid, "alpha")
    sb.manager.checkpoint(sb.sid, "beta")
    sb.fake.calls.clear()
    with pytest.raises(SandboxError, match=r"No checkpoint named 'gamma'.*alpha, beta"):
        sb.manager.restore(sb.sid, "gamma")
    assert steps(sb) == [] and sb.manager.list()[0]["status"] == "ready"


def test_the_sandbox_is_busy_while_it_is_saved(sb):
    seen = {}

    def during_snapshot():
        seen["status"] = sb.manager.list()[0]["status"]
        for operation in (
            lambda: sb.manager.run(sb.sid, "id"),
            lambda: sb.manager.checkpoint(sb.sid, "second"),
            lambda: sb.manager.restore(sb.sid, "first"),
            lambda: sb.manager.screenshot(sb.sid),
        ):
            with pytest.raises(SandboxError, match="not ready"):
                operation()

    sb.fake.hooks["snapshot"] = during_snapshot
    sb.manager.checkpoint(sb.sid, "first")
    assert seen["status"] == "busy"
    assert sb.manager.list()[0]["status"] == "ready"


def test_a_failed_save_leaves_a_working_sandbox_and_no_checkpoint(sb):
    sb.fake.fail.add("snapshot")
    with pytest.raises(SandboxError, match="Could not save the checkpoint.*usable again"):
        sb.manager.checkpoint(sb.sid, "x")
    sb.fake.fail.discard("snapshot")
    assert steps(sb) == ["stop", "snapshot", "start"]   # it was started again
    entry = sb.manager.list()[0]
    assert (entry["status"], entry["checkpoints"]) == ("ready", [])
    assert sb.manager.run(sb.sid, "true").exit_code == 0


def test_a_sandbox_that_cannot_be_started_again_is_removed(sb):
    sb.fake.fail.add("start")
    with pytest.raises(SandboxError, match="could not be brought back, so it was removed"):
        sb.manager.checkpoint(sb.sid, "x")
    assert sb.manager.list() == []
    assert not (sb.clones / sb.sid).exists()


def test_a_failed_restore_leaves_a_working_sandbox_with_its_checkpoints(sb):
    sb.manager.checkpoint(sb.sid, "keep")
    sb.fake.fail.add("revertToSnapshot")
    with pytest.raises(SandboxError, match="Could not restore the checkpoint.*may not be in the saved state"):
        sb.manager.restore(sb.sid, "keep")
    sb.fake.fail.discard("revertToSnapshot")
    entry = sb.manager.list()[0]
    assert (entry["status"], entry["checkpoints"]) == ("ready", ["keep"])
    assert sb.manager.run(sb.sid, "true").exit_code == 0


def test_a_guest_that_never_shuts_down_does_not_get_a_checkpoint_or_lose_the_sandbox(sb):
    sb.fake.ignore_stop = True
    with pytest.raises(SandboxError, match="did not power off in time"):
        sb.manager.checkpoint(sb.sid, "x")
    sb.fake.ignore_stop = False
    assert "snapshot" not in sb.fake.commands()
    entry = sb.manager.list()[0]
    assert (entry["status"], entry["checkpoints"]) == ("ready", [])
    assert sb.manager.run(sb.sid, "true").exit_code == 0


def test_other_sandboxes_are_not_touched(sb):
    other = sb.manager.create()
    other_vmx = str(sb.clones / other / f"{other}.vmx")
    sb.fake.calls.clear()
    sb.manager.checkpoint(sb.sid, "mine")
    sb.manager.restore(sb.sid, "mine")
    for argv in sb.fake.calls:
        if any(step in argv for step in STEPS):
            assert other_vmx not in argv
    states = {e["sandbox_id"]: (e["status"], e["checkpoints"]) for e in sb.manager.list()}
    assert states == {sb.sid: ("ready", ["mine"]), other: ("ready", [])}
    assert other_vmx in sb.fake.running


def test_destroying_a_sandbox_removes_its_checkpoints_too(sb):
    sb.manager.checkpoint(sb.sid, "a")
    sb.manager.checkpoint(sb.sid, "b")
    sb.manager.destroy(sb.sid)
    assert sb.manager.list() == [] and not sb.fake.running
    assert not (sb.clones / sb.sid).exists()


def test_a_busy_sandbox_is_not_mistaken_for_an_orphan_until_it_is_stuck(sb):
    with sb.manager._store.edit() as sandboxes:
        sandboxes[sb.sid]["status"] = "busy"
        sandboxes[sb.sid]["busy_since"] = sb.now[0]
    sb.fake.running.clear()                             # powered off for the snapshot, as it is meanwhile
    sb.now[0] += 600                                    # older than a creation could take
    assert sb.manager.cleanup_orphans() == []
    assert [e["sandbox_id"] for e in sb.manager.list()] == [sb.sid]
    sb.now[0] += sb.config.boot_timeout_seconds + 1200  # but not for ever
    assert sb.manager.cleanup_orphans() == [sb.sid]


def test_checkpoint_and_restore_are_audited(sb):
    sb.manager.checkpoint(sb.sid, "one")
    sb.manager.checkpoint(sb.sid, "one")
    sb.manager.restore(sb.sid, "one")
    with pytest.raises(SandboxError):
        sb.manager.restore(sb.sid, "missing")
    first, second, restored, refused = sb.audit()[-4:]
    assert (first["event"], first["name"], first["replaced"], first["checkpoints"]) == ("checkpoint", "one", False, ["one"])
    assert second["replaced"] is True
    assert (restored["event"], restored["name"], restored["ok"]) == ("restore", "one", True)
    assert (refused["event"], refused["ok"]) == ("restore", False) and "No checkpoint named" in refused["error"]


def test_checkpoints_need_a_known_ready_sandbox(sb):
    for operation in (
        lambda: sb.manager.checkpoint("sbx-deadbeef", "x"),
        lambda: sb.manager.restore("sbx-deadbeef", "x"),
        lambda: sb.manager.checkpoint("not-an-id", "x"),
    ):
        with pytest.raises(SandboxError):
            operation()
    assert sb.fake.calls == []
