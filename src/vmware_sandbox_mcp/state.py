"""Sandbox registry: state.json in the clones folder, guarded by a lock file.

The lock makes it safe for two server processes (say, two MCP clients) to share
one clones folder without treating each other's sandboxes as orphans.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_STALE_LOCK_SECONDS = 30


class StateStore:
    def __init__(self, clones_dir: str | os.PathLike[str]) -> None:
        self._file = Path(clones_dir) / "state.json"
        self._lock = Path(clones_dir) / "state.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        deadline = time.monotonic() + 15
        while True:
            try:
                os.close(os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                try:
                    if time.time() - self._lock.stat().st_mtime > _STALE_LOCK_SECONDS:
                        self._lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Could not lock {self._lock}") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            self._lock.unlink(missing_ok=True)

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data.get("sandboxes", {}) if isinstance(data, dict) else {}

    def _write(self, sandboxes: dict[str, dict[str, Any]]) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sandboxes": sandboxes}, indent=2), encoding="utf-8")
        os.replace(tmp, self._file)

    @contextmanager
    def edit(self) -> Iterator[dict[str, dict[str, Any]]]:
        """Read-modify-write under the lock. Mutate the yielded dict."""
        with self._locked():
            sandboxes = self._read()
            yield sandboxes
            self._write(sandboxes)

    def all(self) -> dict[str, dict[str, Any]]:
        with self._locked():
            return self._read()
