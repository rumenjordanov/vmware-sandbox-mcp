"""Sandbox lifecycle. Talks to VMware only through VmrunClient."""

from __future__ import annotations

import dataclasses
import logging
import re
import secrets
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .config import Config
from .paths import TransferPathError, resolve_in_transfer, same_path
from .state import StateStore
from .guest_run import RunResult, build_script, parse_result
from .vmrun import VmrunClient, VmrunError, VmrunTimeout
from .vmx import NETWORK_MODES, harden_vmx

log = logging.getLogger("vmware_sandbox_mcp")

MARKER = ".vmware-sandbox-mcp"
MAX_RUN_TIMEOUT_SECONDS = 3600
_ID = re.compile(r"^sbx-[0-9a-f]{8}$")


class SandboxError(Exception):
    """A problem the agent can understand and act on."""


class SandboxManager:
    def __init__(
        self,
        config: Config,
        vmrun: VmrunClient,
        store: StateStore,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        audit: AuditLog | None = None,
    ) -> None:
        self._audit = audit or AuditLog(None)
        self._config = config
        self._vmrun = vmrun
        self._store = store
        self._clock = clock
        self._sleep = sleep

    # ---- helpers ---------------------------------------------------------

    def _dir(self, sandbox_id: str) -> Path:
        return self._config.clones_dir / sandbox_id

    def _vmx(self, sandbox_id: str) -> Path:
        return self._dir(sandbox_id) / f"{sandbox_id}.vmx"

    def _require(self, sandbox_id: str) -> dict[str, Any]:
        if not _ID.match(sandbox_id or ""):
            raise SandboxError(f"{sandbox_id!r} is not a valid sandbox id.")
        record = self._store.all().get(sandbox_id)
        if record is None:
            raise SandboxError(f"No sandbox with id {sandbox_id}. Use sandbox_list to see active ones.")
        return record

    def _remove_dir(self, sandbox_id: str) -> None:
        """Delete a sandbox folder, but only one this server demonstrably created."""
        folder = self._dir(sandbox_id)
        if not folder.exists():
            return
        ours = (
            _ID.match(folder.name)
            and same_path(folder.parent, self._config.clones_dir)
            and not folder.is_symlink()
            and (folder / MARKER).is_file()
        )
        if not ours:
            raise SandboxError(f"Refused to delete {folder}: it is not marked as a sandbox.")
        for attempt in range(5):
            shutil.rmtree(folder, ignore_errors=True)
            if not folder.exists():
                return
            self._sleep(1 + attempt)
        raise SandboxError(f"Could not fully delete {folder}; files are still in use.")

    def _teardown(self, sandbox_id: str) -> None:
        vmx = self._vmx(sandbox_id)
        if vmx.is_file():
            for step in (self._vmrun.stop, self._vmrun.delete_vm):
                try:
                    step(vmx)
                except VmrunError:
                    pass  # already off / already gone; the folder removal below is the backstop
        self._remove_dir(sandbox_id)
        with self._store.edit() as sandboxes:
            sandboxes.pop(sandbox_id, None)

    def _wait_ready(self, vmx: Path) -> None:
        deadline = self._clock() + self._config.boot_timeout_seconds
        rejected = 0
        while self._clock() < deadline:
            if self._vmrun.tools_running(vmx):
                worked, message = self._vmrun.probe_guest(vmx)
                if worked:
                    return
                # A wrong login never starts working, so do not wait out the whole boot timeout.
                rejected = rejected + 1 if "invalid user name or password" in message.lower() else 0
                if rejected >= 3:
                    raise SandboxError(
                        "The sandbox rejected the guest login. Check VMSANDBOX_GUEST_USER and "
                        "VMSANDBOX_GUEST_PASSWORD against the account in the base VM."
                    )
            self._sleep(2)
        raise SandboxError(
            f"The sandbox did not become ready within {self._config.boot_timeout_seconds}s."
        )

    # ---- public operations: each one leaves exactly one audit entry -------

    def _audited(
        self,
        event: str,
        fields: dict[str, Any],
        work: Callable[[], Any],
        outcome: Callable[[Any], dict[str, Any]] = lambda result: {},
    ) -> Any:
        started = self._clock()
        try:
            result = work()
        except Exception as exc:
            self._audit.record(
                event, **fields, ok=False, seconds=round(self._clock() - started, 1), error=str(exc)
            )
            raise
        self._audit.record(
            event, **fields, ok=True, seconds=round(self._clock() - started, 1), **outcome(result)
        )
        return result

    def create(self, network: str = "none") -> str:
        return self._audited(
            "create", {"network": network}, lambda: self._create(network), lambda sid: {"sandbox_id": sid}
        )

    def destroy(self, sandbox_id: str) -> None:
        self._audited("destroy", {"sandbox_id": sandbox_id}, lambda: self._destroy(sandbox_id))

    def run(self, sandbox_id: str, command: str, timeout_seconds: int = 60) -> RunResult:
        return self._audited(
            "run",
            {"sandbox_id": sandbox_id, "command": command, "timeout_seconds": timeout_seconds},
            lambda: self._run(sandbox_id, command, timeout_seconds),
            lambda r: {
                "exit_code": r.exit_code,
                "timed_out": r.timed_out,
                "stdout_bytes": len(r.stdout.encode("utf-8", "replace")),
                "stderr_bytes": len(r.stderr.encode("utf-8", "replace")),
            },
        )

    def copy_in(self, sandbox_id: str, host_path: str, guest_path: str) -> int:
        """Copy one file from the transfer folder into the sandbox. Returns its size."""
        return self._audited(
            "copy_in",
            {"sandbox_id": sandbox_id, "host_path": host_path, "guest_path": guest_path},
            lambda: self._copy_in(sandbox_id, host_path, guest_path),
            lambda size: {"bytes": size},
        )

    def copy_out(self, sandbox_id: str, guest_path: str, host_path: str) -> tuple[Path, int]:
        """Copy one file from the sandbox into the transfer folder. Returns (path, size)."""
        return self._audited(
            "copy_out",
            {"sandbox_id": sandbox_id, "guest_path": guest_path, "host_path": host_path},
            lambda: self._copy_out(sandbox_id, guest_path, host_path),
            lambda saved: {"saved_to": str(saved[0]), "bytes": saved[1]},
        )

    def screenshot(self, sandbox_id: str) -> bytes:
        """PNG image of the sandbox's screen."""
        return self._audited(
            "screenshot",
            {"sandbox_id": sandbox_id},
            lambda: self._screenshot(sandbox_id),
            lambda png: {"bytes": len(png)},
        )

    # ---- implementations -------------------------------------------------

    def _create(self, network: str) -> str:
        if network not in NETWORK_MODES:
            raise SandboxError(f"network must be 'none' or 'nat', got {network!r}.")

        self.reap_expired()  # free slots held by sandboxes past their maximum age
        sandbox_id = f"sbx-{secrets.token_hex(4)}"
        folder, vmx = self._dir(sandbox_id), self._vmx(sandbox_id)
        with self._store.edit() as sandboxes:
            # Checked and reserved under the lock, so parallel creates cannot exceed the cap.
            if len(sandboxes) >= self._config.max_sandboxes:
                raise SandboxError(
                    f"The limit of {self._config.max_sandboxes} simultaneous sandboxes is reached. "
                    "Destroy one with sandbox_destroy first (see sandbox_list)."
                )
            sandboxes[sandbox_id] = {
                "network": network,
                "created_at": self._clock(),
                "status": "creating",
            }
        try:
            folder.mkdir()
            (folder / MARKER).write_text("created by vmware-sandbox-mcp\n", encoding="utf-8")
            self._vmrun.clone(vmx, self._config.snapshot, sandbox_id)
            harden_vmx(vmx, self._config.clones_dir, network)
            self._vmrun.start(vmx)
            self._wait_ready(vmx)
        except Exception as exc:
            try:
                self._teardown(sandbox_id)
            except Exception:
                pass
            if isinstance(exc, SandboxError):
                raise
            raise SandboxError(f"Could not create the sandbox: {exc}") from exc

        with self._store.edit() as sandboxes:
            if sandbox_id in sandboxes:
                sandboxes[sandbox_id]["status"] = "ready"
        return sandbox_id

    def _destroy(self, sandbox_id: str) -> None:
        self._require(sandbox_id)
        self._teardown(sandbox_id)

    def _run(self, sandbox_id: str, command: str, timeout_seconds: int) -> RunResult:
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= MAX_RUN_TIMEOUT_SECONDS:
            raise SandboxError(f"timeout_seconds must be between 1 and {MAX_RUN_TIMEOUT_SECONDS}.")
        if not command.strip():
            raise SandboxError("command is empty.")
        if self._require(sandbox_id).get("status") != "ready":
            raise SandboxError(f"Sandbox {sandbox_id} is not ready.")

        vmx = self._vmx(sandbox_id)
        token = secrets.token_hex(8)
        guest_script, guest_result = f"/tmp/.sbx-{token}.sh", f"/tmp/.sbx-{token}.tar"
        started = self._clock()
        # The wrapper deletes its own script, so on success only the result file is left.
        leftovers = [guest_script, guest_result]
        with tempfile.TemporaryDirectory(prefix="vmsandbox-run-") as tmp:
            host_script, host_result = Path(tmp) / "script.sh", Path(tmp) / "result.tar"
            host_script.write_bytes(build_script(command))
            try:
                self._vmrun.copy_to_guest(vmx, host_script, guest_script)
                code = self._vmrun.run_program(
                    vmx,
                    "/bin/bash",
                    guest_script,
                    guest_result,
                    str(timeout_seconds),
                    timeout=timeout_seconds + 30,
                )
                if code != 0:
                    raise SandboxError("The sandbox could not run the command (internal runner failed).")
                leftovers.remove(guest_script)
                self._vmrun.copy_from_guest(vmx, guest_result, host_result)
                result = parse_result(host_result.read_bytes())
            except VmrunTimeout:
                raise SandboxError(
                    f"The sandbox stopped responding while running the command "
                    f"(limit {timeout_seconds}s). Consider destroying it and creating a new one."
                ) from None
            except (VmrunError, OSError, ValueError, tarfile.TarError) as exc:
                raise SandboxError(f"Could not run the command in the sandbox: {exc}") from exc
            finally:
                for leftover in leftovers:
                    try:
                        self._vmrun.delete_guest_file(vmx, leftover)
                    except VmrunError:
                        pass
        elapsed = self._clock() - started
        timed_out = result.exit_code == 124 or (result.exit_code == 137 and elapsed >= timeout_seconds)
        return dataclasses.replace(result, timed_out=timed_out)

    # ---- limits and cleanup ----------------------------------------------

    def reap_expired(self) -> list[str]:
        """Destroy sandboxes older than the configured maximum age."""
        cutoff = self._clock() - self._config.max_age_minutes * 60
        expired = [
            sandbox_id
            for sandbox_id, record in self._store.all().items()
            if record.get("created_at", 0) < cutoff
        ]
        return [sandbox_id for sandbox_id in expired if self._try_teardown(sandbox_id, "expired")]

    def cleanup_orphans(self) -> list[str]:
        """Run once at startup. Removes what a crashed server or a host reboot left behind.

        Only ever touches folders named like a sandbox id that carry this server's marker
        file. Anything else found in the clones folder is reported and left alone.
        """
        with self._store.edit() as sandboxes:
            for key in [key for key in sandboxes if not _ID.match(key)]:
                log.warning("Dropping invalid entry from the state file: %r", key)
                del sandboxes[key]
            records = dict(sandboxes)
        try:
            running = self._vmrun.list_running()
        except VmrunError:
            running = None  # cannot tell; skip the "not running" check
        now = self._clock()
        stuck_after = self._config.boot_timeout_seconds + 300
        removed: list[str] = []

        for sandbox_id, record in records.items():
            vmx = self._vmx(sandbox_id)
            if record.get("status") == "ready":
                dead = not vmx.is_file() or (
                    running is not None and not any(same_path(vmx, path) for path in running)
                )
            else:  # "creating": another server may be mid-create, so only reap stuck ones
                dead = now - record.get("created_at", 0) > stuck_after
            if dead and self._try_teardown(sandbox_id, "orphaned (not running)"):
                removed.append(sandbox_id)

        for entry in sorted(self._config.clones_dir.iterdir()):
            if not entry.is_dir() or entry.name in records:
                continue
            if _ID.match(entry.name) and (entry / MARKER).is_file() and not entry.is_symlink():
                if self._try_teardown(entry.name, "orphaned (untracked folder)"):
                    removed.append(entry.name)
            else:
                log.warning("Leaving unknown folder in the clones directory untouched: %s", entry)
        return removed

    def _try_teardown(self, sandbox_id: str, reason: str) -> bool:
        if not _ID.match(sandbox_id):
            return False
        try:
            self._teardown(sandbox_id)
        except Exception as exc:
            log.warning("Could not remove %s sandbox %s: %s", reason, sandbox_id, exc)
            self._audit.record("auto_destroy", sandbox_id=sandbox_id, reason=reason, ok=False, error=str(exc))
            return False
        log.info("Removed %s sandbox %s", reason, sandbox_id)
        self._audit.record("auto_destroy", sandbox_id=sandbox_id, reason=reason, ok=True)
        return True

    # ---- file transfer ---------------------------------------------------

    def _ready_vmx(self, sandbox_id: str) -> Path:
        if self._require(sandbox_id).get("status") != "ready":
            raise SandboxError(f"Sandbox {sandbox_id} is not ready.")
        return self._vmx(sandbox_id)

    def _host_path(self, host_path: str) -> Path:
        try:
            return resolve_in_transfer(self._config.transfer_dir, host_path)
        except TransferPathError as exc:
            raise SandboxError(str(exc)) from None

    @staticmethod
    def _guest_path(guest_path: str) -> str:
        bad = (
            not isinstance(guest_path, str)
            or not guest_path.startswith("/")
            or guest_path.endswith("/")
            or any(ord(ch) < 32 for ch in guest_path)
        )
        if bad:
            raise SandboxError("guest_path must be a full file path in the sandbox, like /home/debian/data.csv.")
        return guest_path

    def _copy_in(self, sandbox_id: str, host_path: str, guest_path: str) -> int:
        vmx = self._ready_vmx(sandbox_id)
        source = self._host_path(host_path)
        target = self._guest_path(guest_path)
        if not source.is_file():
            raise SandboxError(f"No file at {host_path!r} in the transfer folder (folders cannot be copied).")
        try:
            self._vmrun.copy_to_guest(vmx, source, target)
        except VmrunError as exc:
            raise SandboxError(f"Could not copy the file into the sandbox: {exc}") from exc
        return source.stat().st_size

    def _copy_out(self, sandbox_id: str, guest_path: str, host_path: str) -> tuple[Path, int]:
        vmx = self._ready_vmx(sandbox_id)
        source = self._guest_path(guest_path)
        target = self._host_path(host_path)
        if target.is_dir():
            raise SandboxError(f"{host_path!r} is a folder; give a file name inside it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._vmrun.copy_from_guest(vmx, source, target)
        except VmrunError as exc:
            raise SandboxError(f"Could not copy the file out of the sandbox: {exc}") from exc
        return target, target.stat().st_size

    def _screenshot(self, sandbox_id: str) -> bytes:
        vmx = self._ready_vmx(sandbox_id)
        with tempfile.TemporaryDirectory(prefix="vmsandbox-shot-") as tmp:
            png = Path(tmp) / "screen.png"
            try:
                self._vmrun.capture_screen(vmx, png)
                data = png.read_bytes()
            except (VmrunError, OSError) as exc:
                raise SandboxError(f"Could not take a screenshot of the sandbox: {exc}") from exc
        if not data.startswith(b"\x89PNG"):
            raise SandboxError("The sandbox did not return a valid screenshot.")
        return data

    def list(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [
            {
                "sandbox_id": sandbox_id,
                "network": record.get("network"),
                "status": record.get("status"),
                "age_seconds": int(now - record.get("created_at", now)),
            }
            for sandbox_id, record in sorted(self._store.all().items())
        ]
