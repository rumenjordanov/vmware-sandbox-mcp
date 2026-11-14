"""Folder transfer: a folder travels as one tar file, packed and unpacked by this module.

Both directions are written by hand instead of using tarfile's extractall / add, so
that exactly these rules hold:

  * Host -> sandbox (pack): only regular files and folders are included. If anything
    in the folder is a link or junction, the copy is refused, so a link can never
    pull files from outside the transfer folder into a sandbox.
  * Sandbox -> host (unpack): the archive comes from an untrusted machine. Only regular
    files and folders are created. Links, devices and entries whose names are absolute,
    contain "..", or are not valid Windows names are skipped. Every file is checked to
    land inside the target folder before it is written.
"""

from __future__ import annotations

import os
import re
import tarfile
from pathlib import Path

from .paths import is_within, resolve, same_path

MAX_FOLDER_FILES = 100_000
MAX_FOLDER_BYTES = 4 * 1024**3

_BAD_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)


class FolderError(Exception):
    """A problem the agent can understand and act on."""


def _is_link(entry: Path) -> bool:
    """True for symlinks and Windows junctions: anything that resolves away from where it sits.

    The caller walks down from an already-resolved root, so the entry's own path contains
    no links above it; any difference after resolving comes from the entry itself.
    """
    if entry.is_symlink():
        return True
    return os.path.normcase(os.path.realpath(entry)) != os.path.normcase(os.path.abspath(entry))


def pack_folder(source: Path, tar_path: Path) -> tuple[int, int]:
    """Pack a host folder into tar_path. Returns (files, bytes)."""
    root = resolve(source)
    files = total = 0
    with tarfile.open(tar_path, "w") as tar:
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(current)
            for name in sorted(dirnames) + sorted(filenames):
                entry = here / name
                if _is_link(entry):
                    raise FolderError(
                        f"The folder contains a link ({entry.relative_to(root)}); folders with links cannot be copied."
                    )
            for name in sorted(dirnames):
                info = tarfile.TarInfo((here / name).relative_to(root).as_posix())
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            for name in sorted(filenames):
                entry = here / name
                if not entry.is_file():
                    continue
                size = entry.stat().st_size
                files, total = files + 1, total + size
                if files > MAX_FOLDER_FILES or total > MAX_FOLDER_BYTES:
                    raise FolderError("The folder is too large to copy (limit: 100,000 files or 4 GB).")
                info = tarfile.TarInfo(entry.relative_to(root).as_posix())
                info.size, info.mode = size, 0o644
                with open(entry, "rb") as handle:
                    tar.addfile(info, handle)
    return files, total


def _safe_parts(name: str) -> list[str] | None:
    """Split an archive member name into path parts, or None if it must not be created."""
    if name.startswith("/") or "\\" in name:
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    for part in parts:
        if (
            part == ".."
            or len(part) > 255
            or _BAD_CHARS.search(part)
            or part.endswith((" ", "."))
            or _RESERVED.match(part)
        ):
            return None
    return parts


def unpack_folder(tar_path: Path, target: Path) -> tuple[int, int, int]:
    """Unpack an untrusted tar into target. Returns (files, bytes, skipped entries)."""
    root = resolve(target)
    root.mkdir(parents=True, exist_ok=True)
    files = total = skipped = 0
    with tarfile.open(tar_path, "r:") as tar:
        for member in tar:
            if not (member.isreg() or member.isdir()):
                skipped += 1  # links, devices, pipes
                continue
            parts = _safe_parts(member.name)
            if parts is None:
                skipped += 1
                continue
            if not parts:
                continue  # the folder itself ("." or "./")
            destination = root.joinpath(*parts)
            # Also catches a junction already sitting inside the target folder.
            if not is_within(destination, root) or not (
                same_path(destination.parent, root) or is_within(destination.parent, root)
            ):
                skipped += 1
                continue
            try:
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                files, total = files + 1, total + member.size
                if files > MAX_FOLDER_FILES or total > MAX_FOLDER_BYTES:
                    raise FolderError("The folder is too large to copy (limit: 100,000 files or 4 GB).")
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                with open(destination, "wb") as out:
                    while chunk := source.read(1024 * 1024):  # type: ignore[union-attr]
                        out.write(chunk)
            except OSError:
                skipped += 1  # for example a path too long for Windows, or a file/folder name clash
                if member.isreg():
                    files, total = files - 1, total - member.size
    return files, total, skipped
