import os
import sys

import pytest

from vmware_sandbox_mcp.manager import SandboxError
from vmware_sandbox_mcp.paths import TransferPathError, resolve_in_transfer


@pytest.fixture
def ready(env):
    env.transfer = env.config.transfer_dir
    env.sid = env.manager.create()
    env.fake.calls.clear()
    return env


def copy_calls(env):
    return [c for c in env.fake.commands() if c.startswith("CopyFile")]


def test_copy_in_and_out_roundtrip(ready):
    (ready.transfer / "in dir").mkdir()
    (ready.transfer / "in dir" / "a b.txt").write_bytes(b"payload")
    assert ready.manager.copy_in(ready.sid, "in dir/a b.txt", "/home/debian/a b.txt") == 7
    assert ready.fake.guest_files["/home/debian/a b.txt"] == b"payload"

    path, size = ready.manager.copy_out(ready.sid, "/home/debian/a b.txt", "out/new/b.txt")
    assert path == ready.transfer / "out" / "new" / "b.txt"
    assert path.read_bytes() == b"payload" and size == 7


def test_absolute_host_path_inside_transfer_is_accepted(ready):
    (ready.transfer / "f.txt").write_text("x")
    ready.manager.copy_in(ready.sid, str(ready.transfer / "f.txt"), "/tmp/f.txt")
    assert "/tmp/f.txt" in ready.fake.guest_files


ESCAPES = [
    "../secret.txt",
    "..\\secret.txt",
    "sub/../../secret.txt",
    "sub/../../Virtual Machines/sandbox-base/Debian 13.x 64-bit.vmx",
    "",
    "   ",
    "bad\x00name",
    "file.txt:stream",
]


@pytest.mark.parametrize("host_path", ESCAPES)
def test_copy_in_rejects_paths_outside_transfer(ready, host_path):
    (ready.transfer.parent / "secret.txt").write_text("secret")
    with pytest.raises(SandboxError):
        ready.manager.copy_in(ready.sid, host_path, "/tmp/x")
    assert copy_calls(ready) == []


@pytest.mark.parametrize("host_path", ESCAPES)
def test_copy_out_rejects_paths_outside_transfer(ready, host_path):
    ready.fake.guest_files["/tmp/x"] = b"data"
    with pytest.raises(SandboxError):
        ready.manager.copy_out(ready.sid, "/tmp/x", host_path)
    assert copy_calls(ready) == []
    assert not (ready.transfer.parent / "secret.txt").exists()


def test_absolute_paths_outside_transfer_rejected(ready):
    outside = ready.transfer.parent / "outside.txt"
    outside.write_text("x")
    for target in (str(outside), str(ready.base_vmx), str(ready.clones / "state.json")):
        with pytest.raises(SandboxError):
            ready.manager.copy_in(ready.sid, target, "/tmp/x")
        with pytest.raises(SandboxError):
            ready.manager.copy_out(ready.sid, "/tmp/x", target)
    assert copy_calls(ready) == []


def test_transfer_folder_itself_is_not_a_file_target(ready):
    with pytest.raises(SandboxError):
        ready.manager.copy_out(ready.sid, "/tmp/x", ".")
    assert copy_calls(ready) == []


def test_link_pointing_outside_transfer_is_rejected(ready):
    outside = ready.transfer.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = ready.transfer / "link"
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(outside), str(link))
        else:
            os.symlink(outside, link)
    except OSError:
        pytest.skip("cannot create links here")
    with pytest.raises(SandboxError):
        ready.manager.copy_in(ready.sid, "link/secret.txt", "/tmp/x")
    with pytest.raises(SandboxError):
        ready.manager.copy_out(ready.sid, "/tmp/x", "link/new.txt")
    assert copy_calls(ready) == []


def test_copy_in_requires_existing_regular_file(ready):
    (ready.transfer / "folder").mkdir()
    for host_path in ("missing.txt", "folder"):
        with pytest.raises(SandboxError):
            ready.manager.copy_in(ready.sid, host_path, "/tmp/x")
    assert copy_calls(ready) == []


@pytest.mark.parametrize("guest_path", ["relative.txt", "", "/tmp/dir/", "/tmp/a\nb"])
def test_guest_path_must_be_absolute_file_path(ready, guest_path):
    (ready.transfer / "f.txt").write_text("x")
    with pytest.raises(SandboxError):
        ready.manager.copy_in(ready.sid, "f.txt", guest_path)
    with pytest.raises(SandboxError):
        ready.manager.copy_out(ready.sid, guest_path, "out.txt")
    assert copy_calls(ready) == []


def test_missing_guest_file_gives_error(ready):
    with pytest.raises(SandboxError, match="Could not copy"):
        ready.manager.copy_out(ready.sid, "/tmp/nope", "out.txt")


def test_copy_needs_a_known_sandbox(ready):
    (ready.transfer / "f.txt").write_text("x")
    with pytest.raises(SandboxError):
        ready.manager.copy_in("sbx-deadbeef", "f.txt", "/tmp/x")
    assert copy_calls(ready) == []


def test_resolver_returns_path_inside_root(tmp_path):
    assert resolve_in_transfer(tmp_path, "a/b.txt") == tmp_path.resolve() / "a" / "b.txt"
    with pytest.raises(TransferPathError):
        resolve_in_transfer(tmp_path, "..")
