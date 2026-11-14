"""Folder copy: packing host folders, and unpacking archives that come from an untrusted sandbox."""

from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from vmware_sandbox_mcp import folders
from vmware_sandbox_mcp.folders import FolderError, pack_folder, unpack_folder
from vmware_sandbox_mcp.manager import SandboxError


def make_tar(path: Path, entries: list[tuple]) -> Path:
    """entries: (name, bytes) for a file, (name, None) for a folder, (name, 'sym'|'hard'|'dev'|'fifo', target)."""
    with tarfile.open(path, "w") as tar:
        for entry in entries:
            info = tarfile.TarInfo(entry[0])
            if len(entry) == 3:
                kind, info.linkname = entry[1], entry[2]
                info.type = {"sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE, "dev": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE}[kind]
                tar.addfile(info)
            elif entry[1] is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(entry[1])
                tar.addfile(info, io.BytesIO(entry[1]))
    return path


def tree(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


# ---- packing a host folder --------------------------------------------------

def test_pack_and_unpack_round_trip(tmp_path):
    src = tmp_path / "src"
    (src / "sub dir" / "deep").mkdir(parents=True)
    (src / "empty").mkdir()
    (src / "a.txt").write_bytes(b"alpha")
    (src / "sub dir" / "b c.bin").write_bytes(bytes(range(256)) * 40)
    (src / "sub dir" / "deep" / "ünï.txt").write_bytes(b"unicode")

    files, size = pack_folder(src, tmp_path / "f.tar")
    assert (files, size) == (3, 5 + 10240 + 7)
    assert unpack_folder(tmp_path / "f.tar", tmp_path / "out") == (3, 5 + 10240 + 7, 0)
    assert tree(tmp_path / "out") == tree(src)


def test_packed_archive_has_only_plain_files_and_folders_with_relative_names(tmp_path):
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f").write_bytes(b"x")
    pack_folder(src, tmp_path / "f.tar")
    with tarfile.open(tmp_path / "f.tar") as tar:
        members = tar.getmembers()
    assert [(m.name, m.isdir(), m.mode) for m in members] == [("d", True, 0o755), ("d/f", False, 0o644)]


def _symlink_or_skip(link: Path, target: Path, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except OSError:
        pytest.skip("creating symlinks needs a privilege this account does not have")


def test_pack_refuses_a_folder_containing_a_file_link(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    src = tmp_path / "src"
    src.mkdir()
    _symlink_or_skip(src / "innocent.txt", secret, False)
    with pytest.raises(FolderError, match="link"):
        pack_folder(src, tmp_path / "f.tar")


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_pack_refuses_a_folder_containing_a_junction(tmp_path):
    import subprocess

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret")
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(src / "sub" / "jump"), str(outside)], capture_output=True)
    if done.returncode != 0:
        pytest.skip("could not create a junction")
    with pytest.raises(FolderError, match="link"):
        pack_folder(src, tmp_path / "f.tar")


def test_pack_enforces_the_size_limits(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    for i in range(4):
        (src / f"{i}.txt").write_bytes(b"12345")
    monkeypatch.setattr(folders, "MAX_FOLDER_FILES", 3)
    with pytest.raises(FolderError, match="too large"):
        pack_folder(src, tmp_path / "a.tar")
    monkeypatch.setattr(folders, "MAX_FOLDER_FILES", 100)
    monkeypatch.setattr(folders, "MAX_FOLDER_BYTES", 19)
    with pytest.raises(FolderError, match="too large"):
        pack_folder(src, tmp_path / "b.tar")


# ---- unpacking an archive from a sandbox ------------------------------------

@pytest.mark.parametrize("name", [
    "../escape.txt", "a/../../escape.txt", "./../escape.txt", "/etc/absolute.txt", "//server/share/x.txt",
    "..\\escape.txt", "a\\..\\..\\escape.txt", "C:\\Windows\\x.txt", "C:/Windows/x.txt", "C:escape.txt",
    "file.txt:hidden-stream", "a/NUL", "a/con.txt", "COM1", "LPT9.log", "trailing-dot.", "trailing-space ",
    "bad|name", "bad?name", "bad*name", 'bad"name', "bad<name", "ctrl\x07char", "a/" + "x" * 300,
])
def test_unsafe_names_are_skipped_and_nothing_lands_outside(tmp_path, name):
    target = tmp_path / "transfer" / "out"
    make_tar(tmp_path / "evil.tar", [("ok.txt", b"fine"), (name, b"EVIL"), ("d/also-ok.txt", b"fine")])
    files, size, skipped = unpack_folder(tmp_path / "evil.tar", target)
    assert (files, size, skipped) == (2, 8, 1)
    assert tree(target) == {"ok.txt": b"fine", "d": None, "d/also-ok.txt": b"fine"}
    everything = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert sorted(p.name for p in everything) == ["also-ok.txt", "evil.tar", "ok.txt"]


@pytest.mark.parametrize("kind,target", [
    ("sym", "/etc/passwd"), ("sym", "..\\..\\secret.txt"), ("sym", "C:\\Windows\\System32\\drivers\\etc\\hosts"),
    ("hard", "ok.txt"), ("hard", "../../secret.txt"), ("dev", ""), ("fifo", ""),
])
def test_links_and_special_files_are_never_created(tmp_path, kind, target):
    out = tmp_path / "out"
    make_tar(tmp_path / "evil.tar", [("ok.txt", b"fine"), ("link", kind, target)])
    assert unpack_folder(tmp_path / "evil.tar", out) == (1, 4, 1)
    assert tree(out) == {"ok.txt": b"fine"}


def test_symlink_then_write_through_it_cannot_escape(tmp_path):
    """The classic tar trick: create 'x' as a link to outside, then write 'x/file'."""
    outside = tmp_path / "outside"
    outside.mkdir()
    out = tmp_path / "out"
    make_tar(tmp_path / "evil.tar", [("x", "sym", str(outside)), ("x/planted.txt", b"EVIL")])
    files, _, skipped = unpack_folder(tmp_path / "evil.tar", out)
    assert skipped == 1 and files == 1
    assert list(outside.iterdir()) == []
    assert (out / "x").is_dir() and not (out / "x").is_symlink()
    assert (out / "x" / "planted.txt").read_bytes() == b"EVIL"  # a plain file inside the target


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_existing_junction_inside_the_target_is_not_written_through(tmp_path):
    import subprocess

    outside = tmp_path / "outside"
    outside.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(out / "jump"), str(outside)], capture_output=True)
    if done.returncode != 0:
        pytest.skip("could not create a junction")
    make_tar(tmp_path / "evil.tar", [("jump/planted.txt", b"EVIL"), ("jump/deep/planted.txt", b"EVIL"), ("ok.txt", b"fine")])
    files, _, skipped = unpack_folder(tmp_path / "evil.tar", out)
    assert (files, skipped) == (1, 2)
    assert list(outside.iterdir()) == []


def test_unpack_enforces_the_size_limits(tmp_path, monkeypatch):
    make_tar(tmp_path / "big.tar", [(f"{i}.txt", b"12345") for i in range(4)])
    monkeypatch.setattr(folders, "MAX_FOLDER_FILES", 3)
    with pytest.raises(FolderError, match="too large"):
        unpack_folder(tmp_path / "big.tar", tmp_path / "a")
    monkeypatch.setattr(folders, "MAX_FOLDER_FILES", 100)
    monkeypatch.setattr(folders, "MAX_FOLDER_BYTES", 19)
    with pytest.raises(FolderError, match="too large"):
        unpack_folder(tmp_path / "big.tar", tmp_path / "b")


def test_existing_files_are_replaced_and_others_kept(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_bytes(b"mine")
    (out / "a.txt").write_bytes(b"old")
    make_tar(tmp_path / "f.tar", [("./", None), ("./a.txt", b"new"), ("./sub/", None), ("./sub/b.txt", b"b")])
    assert unpack_folder(tmp_path / "f.tar", out) == (2, 4, 0)
    assert tree(out) == {"a.txt": b"new", "keep.txt": b"mine", "sub": None, "sub/b.txt": b"b"}


def test_file_where_a_folder_is_expected_is_skipped_not_fatal(tmp_path):
    make_tar(tmp_path / "f.tar", [("a", b"file"), ("a/b.txt", b"under a file"), ("c.txt", b"ok")])
    files, _, skipped = unpack_folder(tmp_path / "f.tar", tmp_path / "out")
    assert (files, skipped) == (2, 1)


def test_garbage_instead_of_an_archive_is_an_error(tmp_path):
    (tmp_path / "junk.tar").write_bytes(b"this is not a tar file" * 100)
    with pytest.raises(tarfile.TarError):
        unpack_folder(tmp_path / "junk.tar", tmp_path / "out")


# ---- through the manager, against the fake vmrun ----------------------------

@pytest.fixture
def ready(env):
    env.transfer = env.config.transfer_dir
    env.sid = env.manager.create()
    env.fake.calls.clear()
    return env


def test_folder_copy_in_then_out_round_trip(ready):
    src = ready.transfer / "my project"
    (src / "src").mkdir(parents=True)
    (src / "README.md").write_bytes(b"# hi")
    (src / "src" / "main.py").write_bytes(b"print(1)")

    done = ready.manager.copy_in(ready.sid, "my project", "/home/debian/my project")
    assert (done.kind, done.files, done.bytes) == ("folder", 2, 12)
    assert "/home/debian/my project" in ready.fake.guest_dirs
    script, timeout = ready.fake.ran[-1]
    assert script.startswith("mkdir -p -- '/home/debian/my project' && tar -xf /tmp/.sbx-")
    assert not any(name.endswith(".dir.tar") for name in ready.fake.guest_files)  # temp archive removed

    back = ready.manager.copy_out(ready.sid, "/home/debian/my project", "results/copy")
    assert (back.kind, back.files, back.bytes, back.skipped) == ("folder", 2, 12, 0)
    assert back.path == ready.transfer / "results" / "copy"
    assert tree(back.path) == tree(src)
    assert not any(name.endswith(".dir.tar") for name in ready.fake.guest_files)


def test_guest_path_with_shell_characters_is_quoted(ready):
    (ready.transfer / "d").mkdir()
    (ready.transfer / "d" / "f").write_bytes(b"x")
    nasty = "/tmp/a'; touch /tmp/pwned; echo '$(id) `id`"
    ready.manager.copy_in(ready.sid, "d", nasty)
    assert nasty in ready.fake.guest_dirs  # the fake parses the command like a shell would
    ready.manager.copy_out(ready.sid, nasty, "back")
    assert (ready.transfer / "back" / "f").read_bytes() == b"x"


def test_malicious_archive_from_the_sandbox_stays_inside_the_transfer_folder(ready, tmp_path):
    evil = make_tar(tmp_path / "evil.tar", [
        ("report.txt", b"real result"),
        ("../../escape.txt", b"EVIL"),
        ("/abs.txt", b"EVIL"),
        ("link", "sym", str(ready.base_vmx)),
        ("..\\..\\win-escape.txt", b"EVIL"),
    ])
    ready.fake.guest_dirs["/home/debian/out"] = evil.read_bytes()
    before = {p for p in ready.transfer.parent.rglob("*") if ready.transfer not in p.parents and p != ready.transfer}
    done = ready.manager.copy_out(ready.sid, "/home/debian/out", "results")
    assert (done.files, done.skipped) == (1, 4)
    assert tree(ready.transfer) == {"results": None, "results/report.txt": b"real result"}
    after = {p for p in ready.transfer.parent.rglob("*") if ready.transfer not in p.parents and p != ready.transfer}
    assert {p.name for p in after - before} <= {"audit.jsonl"}
    entry = ready.audit()[-1]
    assert entry["event"] == "copy_out" and entry["kind"] == "folder" and entry["skipped"] == 4


def test_host_folder_with_a_link_is_refused_before_anything_is_sent(ready, tmp_path):
    src = ready.transfer / "proj"
    src.mkdir()
    _symlink_or_skip(src / "vmx", ready.base_vmx, False)
    with pytest.raises(SandboxError, match="link"):
        ready.manager.copy_in(ready.sid, "proj", "/tmp/proj")
    assert not [c for c in ready.fake.commands() if c.startswith("CopyFile")]


def test_folder_escapes_are_rejected_like_file_escapes(ready):
    ready.fake.guest_dirs["/tmp/d"] = make_tar(ready.transfer.parent / "t.tar", [("f", b"x")]).read_bytes()
    for host_path in ("../outside", "..", "sub/../../outside", str(ready.base_vmx.parent)):
        with pytest.raises(SandboxError):
            ready.manager.copy_out(ready.sid, "/tmp/d", host_path)
        with pytest.raises(SandboxError):
            ready.manager.copy_in(ready.sid, host_path, "/tmp/x")
    assert not [c for c in ready.fake.commands() if c.startswith("CopyFile")]


def test_folder_onto_an_existing_file_is_refused(ready):
    (ready.transfer / "taken.txt").write_bytes(b"mine")
    ready.fake.guest_dirs["/tmp/d"] = b""
    with pytest.raises(SandboxError, match="existing file"):
        ready.manager.copy_out(ready.sid, "/tmp/d", "taken.txt")
    assert (ready.transfer / "taken.txt").read_bytes() == b"mine"


def test_broken_archive_from_the_sandbox_is_a_clean_error(ready):
    ready.fake.guest_dirs["/tmp/d"] = b"not a tar at all" * 200
    with pytest.raises(SandboxError, match="Could not copy the folder"):
        ready.manager.copy_out(ready.sid, "/tmp/d", "out")


def test_guest_side_failure_is_reported_and_cleaned_up(ready):
    (ready.transfer / "d").mkdir()
    ready.fake.next_result = (b"", b"mkdir: cannot create directory: Permission denied\n", 1)
    ready.fake.tar_fails = True
    with pytest.raises(SandboxError, match="Permission denied"):
        ready.manager.copy_in(ready.sid, "d", "/root/forbidden")
    assert ready.fake.guest_dirs == {}
    assert not any(name.endswith(".dir.tar") for name in ready.fake.guest_files)


def test_plain_file_copy_is_unchanged(ready):
    (ready.transfer / "f.txt").write_bytes(b"12345")
    done = ready.manager.copy_in(ready.sid, "f.txt", "/tmp/f.txt")
    assert (done.kind, done.files, done.bytes) == ("file", 1, 5)
    out = ready.manager.copy_out(ready.sid, "/tmp/f.txt", "g.txt")
    assert (out.kind, out.bytes, out.path) == ("file", 5, ready.transfer / "g.txt")
    assert ready.fake.ran == []  # no guest command needed for single files
