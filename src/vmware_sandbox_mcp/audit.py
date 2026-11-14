"""Append-only audit log: one JSON object per line, one line per operation.

Records what was asked for and how it ended. Command text is recorded in full; command
output is not (only its size). Guest credentials are never passed to this module.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("vmware_sandbox_mcp")


class AuditLog:
    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = json.dumps({"time": stamp, "event": event, **fields}, ensure_ascii=False, default=str)
        try:
            # One short write in append mode per entry, so servers sharing the file do not interleave.
            with self._lock, open(self._path, "a", encoding="utf-8", errors="backslashreplace", newline="\n") as file:
                file.write(line + "\n")
        except OSError as exc:
            # A full disk must not make sandboxes impossible to destroy; say so loudly instead.
            log.warning("Could not write to the audit log %s: %s", self._path, exc)
