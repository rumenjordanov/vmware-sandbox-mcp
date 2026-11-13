"""Path containment helpers shared by every safety check."""

from __future__ import annotations

import os
from pathlib import Path


def resolve(path: str | os.PathLike[str]) -> Path:
    """Absolute path with '..', symlinks and junctions resolved."""
    return Path(os.path.realpath(os.fspath(path)))


def _key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(str(resolve(path)))


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return _key(a) == _key(b)


def is_within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """True if child is strictly inside parent (not parent itself)."""
    c, p = _key(child), _key(parent)
    if c == p:
        return False
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


class TransferPathError(ValueError):
    pass


def resolve_in_transfer(transfer_dir: str | os.PathLike[str], host_path: str) -> Path:
    """Map an agent-supplied host path to a real path strictly inside the transfer folder.

    Relative paths are taken relative to the transfer folder. Anything that ends up
    outside it after resolving '..', symlinks and junctions is rejected.
    """
    if not isinstance(host_path, str) or not host_path.strip():
        raise TransferPathError("host_path is empty.")
    if any(ord(ch) < 32 for ch in host_path):
        raise TransferPathError("host_path contains control characters.")
    root = resolve(transfer_dir)
    candidate = Path(host_path)
    if not candidate.is_absolute():
        # A ':' in a relative Windows path means a drive ("C:x") or an alternate data stream.
        if ":" in host_path:
            raise TransferPathError("host_path must not contain ':' unless it is a full path.")
        candidate = root / candidate
    resolved = resolve(candidate)
    if not is_within(resolved, root):
        raise TransferPathError(
            f"host_path must be inside the transfer folder ({root}); got {host_path!r}."
        )
    if ":" in str(resolved.relative_to(root)):
        raise TransferPathError("host_path must not contain ':' inside the transfer folder.")
    return resolved


def overlaps(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """True if the two directories are the same or one contains the other."""
    return same_path(a, b) or is_within(a, b) or is_within(b, a)
