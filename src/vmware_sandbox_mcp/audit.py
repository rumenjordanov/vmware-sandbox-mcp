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
    def __init__(
        self,
        path: Path | None,
        clock: Callable[[], float] = time.time,
        redact: tuple[str, ...] = (),
    ) -> None:
        """redact: secrets (the guest password) that are blanked out wherever they would be written,
        for example when someone types the password into the sandbox's login screen."""
        self._path = path
        self._redact = sorted({secret for secret in redact if secret}, key=len, reverse=True)
        self._clock = clock
        self._lock = threading.Lock()

    def _scrub(self, value: Any) -> Any:
        """Blank the secrets inside text values only, so numbers and structure are never touched."""
        if isinstance(value, str):
            for secret in self._redact:
                value = value.replace(secret, "[guest password]")
            return value
        if isinstance(value, dict):
            return {key: self._scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(item) for item in value]
        return value

    def record(self, event: str, **fields: Any) -> None:
        if self._path is None:
            return
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {"time": stamp, "event": event, **self._scrub(fields)}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            # One short write in append mode per entry, so servers sharing the file do not interleave.
            with self._lock, open(self._path, "a", encoding="utf-8", errors="backslashreplace", newline="\n") as file:
                file.write(line + "\n")
        except OSError as exc:
            # A full disk must not make sandboxes impossible to destroy; say so loudly instead.
            log.warning("Could not write to the audit log %s: %s", self._path, exc)
