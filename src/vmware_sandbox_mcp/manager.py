"""Sandbox lifecycle. Talks to VMware only through VmrunClient."""

from __future__ import annotations

import dataclasses
import logging
import re
import secrets
import shlex
import shutil
import socket
import string
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .audit import AuditLog
from .config import Config
from .folders import FolderError, pack_folder, unpack_folder
from . import input as remote_input
from . import jobs
from .input import InputError, RfbClient
from .jobs import JobError, JobStatus
from .paths import TransferPathError, resolve_in_transfer, same_path
from .state import StateStore
from .guest_run import RunResult, build_script, parse_result
from .vmrun import VmrunClient, VmrunError, VmrunTimeout
from .vmx import NETWORK_MODES, check_resources, harden_vmx, read_vmx_resources

log = logging.getLogger("vmware_sandbox_mcp")
T = TypeVar("T")

MARKER = ".vmware-sandbox-mcp"
MAX_RUN_TIMEOUT_SECONDS = 3600
_ID = re.compile(r"^sbx-[0-9a-f]{8}\Z")


FOLDER_TIMEOUT_SECONDS = 600
TOOLS_STATUS_PATIENCE_SECONDS = 30
MAX_CHECKPOINTS = 5
_CHECKPOINT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z")
JOB_STEP_TIMEOUT_SECONDS = 60


class SandboxError(Exception):
    """A problem the agent can understand and act on."""


@dataclasses.dataclass(frozen=True)
class CheckpointResult:
    name: str
    replaced: bool
    checkpoints: list[str]


@dataclasses.dataclass(frozen=True)
class Transfer:
    kind: str  # "file" or "folder"
    files: int
    bytes: int
    path: Path | None = None  # where it was saved on the host (copy_out only)
    skipped: int = 0  # entries of a folder from the sandbox that were not created on the host


class SandboxManager:
    def __init__(
        self,
        config: Config,
        vmrun: VmrunClient,
        store: StateStore,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        audit: AuditLog | None = None,
        input_connect: Callable[[int, str], RfbClient] = RfbClient,
        port_picker: Callable[[set[int]], int] | None = None,
    ) -> None:
        self._audit = audit or AuditLog(None)
        self._input_connect = input_connect
        self._port_picker = port_picker or self._free_port
        self._config = config
        self._vmrun = vmrun
        self._store = store
        self._clock = clock
        self._sleep = sleep

    @property
    def config(self) -> Config:
        return self._config

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
        started = self._clock()
        deadline = started + self._config.boot_timeout_seconds
        rejected = 0
        while self._clock() < deadline:
            # VMware sometimes keeps saying the tools are "installed", not "running", for a guest that
            # works and answers every command. So after a while, ask the guest itself instead of
            # trusting that status: a command that runs is the real proof.
            if self._vmrun.tools_running(vmx) or self._clock() - started >= TOOLS_STATUS_PATIENCE_SECONDS:
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

    def create(self, network: str = "none", memory_mb: int | None = None, cpus: int | None = None) -> str:
        return self._audited(
            "create",
            {"network": network, "memory_mb": memory_mb, "cpus": cpus},
            lambda: self._create(network, memory_mb, cpus),
            lambda sid: {"sandbox_id": sid},
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

    def start(self, sandbox_id: str, command: str) -> str:
        """Start a command that keeps running in the background. Returns its job id."""
        return self._audited(
            "start",
            {"sandbox_id": sandbox_id, "command": command},
            lambda: self._start(sandbox_id, command),
            lambda job_id: {"job_id": job_id},
        )

    def check(self, sandbox_id: str, job_id: str) -> JobStatus:
        """Status and newest output of a background command."""
        return self._audited(
            "check",
            {"sandbox_id": sandbox_id, "job_id": job_id},
            lambda: self._check(sandbox_id, job_id),
            lambda s: {"status": s.status, "exit_code": s.exit_code},
        )

    def stop(self, sandbox_id: str, job_id: str) -> str:
        """Stop a background command. Returns "stopped" or "finished" (it had already ended)."""
        return self._audited(
            "stop",
            {"sandbox_id": sandbox_id, "job_id": job_id},
            lambda: self._stop(sandbox_id, job_id),
            lambda outcome: {"outcome": outcome},
        )

    def checkpoint(self, sandbox_id: str, name: str) -> CheckpointResult:
        """Save the sandbox's disk under a name (replacing a checkpoint of that name)."""
        return self._audited(
            "checkpoint",
            {"sandbox_id": sandbox_id, "name": name},
            lambda: self._checkpoint(sandbox_id, name),
            lambda r: {"replaced": r.replaced, "checkpoints": r.checkpoints},
        )

    def restore(self, sandbox_id: str, name: str) -> list[str]:
        """Roll the sandbox's disk back to a checkpoint. Returns the checkpoint names it still has."""
        return self._audited(
            "restore",
            {"sandbox_id": sandbox_id, "name": name},
            lambda: self._restore(sandbox_id, name),
            lambda names: {"checkpoints": names},
        )

    def click(self, sandbox_id: str, x: int, y: int, button: str = "left", double: bool = False) -> None:
        """Click on the sandbox's screen at a pixel position."""
        self._audited(
            "click",
            {"sandbox_id": sandbox_id, "x": x, "y": y, "button": button, "double": double},
            lambda: self._with_input(sandbox_id, lambda c: remote_input.click(c, x, y, button, double, self._sleep)),
        )

    def scroll(self, sandbox_id: str, x: int, y: int, direction: str = "down", amount: int = 3) -> None:
        """Turn the mouse wheel at a pixel position of the sandbox's screen."""
        self._audited(
            "scroll",
            {"sandbox_id": sandbox_id, "x": x, "y": y, "direction": direction, "amount": amount},
            lambda: self._with_input(
                sandbox_id, lambda c: remote_input.scroll(c, x, y, direction, amount, self._sleep)
            ),
        )

    def drag(
        self, sandbox_id: str, from_x: int, from_y: int, to_x: int, to_y: int, button: str = "left"
    ) -> None:
        """Press a mouse button at one position, move to another, release."""
        self._audited(
            "drag",
            {"sandbox_id": sandbox_id, "from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y, "button": button},
            lambda: self._with_input(
                sandbox_id, lambda c: remote_input.drag(c, from_x, from_y, to_x, to_y, button, self._sleep)
            ),
        )

    def type_text(self, sandbox_id: str, text: str) -> int:
        """Type text on the sandbox's keyboard. Returns the number of keys typed."""
        return self._audited(
            "type",
            {"sandbox_id": sandbox_id, "text": text},
            lambda: self._with_input(sandbox_id, lambda c: remote_input.type_text(c, text, self._sleep)),
            lambda count: {"keys": count},
        )

    def press_keys(self, sandbox_id: str, keys: str) -> None:
        """Press one key or a shortcut such as 'ctrl+alt+t'."""
        self._audited(
            "key",
            {"sandbox_id": sandbox_id, "keys": keys},
            lambda: self._with_input(sandbox_id, lambda c: remote_input.press_keys(c, keys, self._sleep)),
        )

    def remote_display_port(self, sandbox_id: str) -> int | None:
        """The local port of a sandbox's remote display, or None if it has none."""
        return (self._require(sandbox_id).get("input") or {}).get("port")

    def copy_in(self, sandbox_id: str, host_path: str, guest_path: str) -> Transfer:
        """Copy a file or a folder from the transfer folder into the sandbox."""
        return self._audited(
            "copy_in",
            {"sandbox_id": sandbox_id, "host_path": host_path, "guest_path": guest_path},
            lambda: self._copy_in(sandbox_id, host_path, guest_path),
            lambda t: {"kind": t.kind, "files": t.files, "bytes": t.bytes},
        )

    def copy_out(self, sandbox_id: str, guest_path: str, host_path: str) -> Transfer:
        """Copy a file or a folder from the sandbox into the transfer folder."""
        return self._audited(
            "copy_out",
            {"sandbox_id": sandbox_id, "guest_path": guest_path, "host_path": host_path},
            lambda: self._copy_out(sandbox_id, guest_path, host_path),
            lambda t: {
                "kind": t.kind, "files": t.files, "bytes": t.bytes, "saved_to": str(t.path), "skipped": t.skipped
            },
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

    def _resolve_resources(
        self, vmx: Path, memory_mb: int | None, cpus: int | None
    ) -> tuple[int | None, int | None]:
        """Check a size request against the limits and return the size the sandbox will have.

        The limit is what the user configured, or else the size of the base VM, so by default an
        agent can ask for less than the base VM but never for more. The clone's own .vmx gives
        the base size, because that is what it was just created with.
        """
        base_memory, base_cpus = read_vmx_resources(vmx, self._config.clones_dir)
        for label, wanted, base, configured in (
            ("memory_mb", memory_mb, base_memory, self._config.max_memory_mb),
            ("cpus", cpus, base_cpus, self._config.max_cpus),
        ):
            if wanted is None:
                continue
            limit = configured if configured is not None else base
            if limit is None:
                raise SandboxError(
                    f"{label} cannot be changed: the size of the base VM could not be read and the user "
                    "has not set a maximum."
                )
            if wanted > limit:
                raise SandboxError(
                    f"{label} can be at most {limit} on this computer, and {wanted} was asked for. "
                    "Ask for less, or leave it out for the default size."
                )
        return (memory_mb if memory_mb is not None else base_memory, cpus if cpus is not None else base_cpus)

    def _create(self, network: str, memory_mb: int | None = None, cpus: int | None = None) -> str:
        if network not in NETWORK_MODES:
            raise SandboxError(f"network must be 'none', 'web' or 'nat', got {network!r}.")
        try:
            check_resources(memory_mb, cpus)
        except ValueError as exc:
            raise SandboxError(str(exc)) from None
        if network == "web" and not self._config.web_vnet:
            raise SandboxError(
                "network 'web' is not set up on this computer. Use 'none', or 'nat' if the task needs internet."
            )

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
            display = None
            if self._config.remote_input:
                taken = {(r.get("input") or {}).get("port") for r in sandboxes.values()}
                password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
                display = (self._port_picker(taken), password)
                # Kept in the state file in the clones folder, which the agent cannot read.
                sandboxes[sandbox_id]["input"] = {"port": display[0], "password": display[1]}
        try:
            folder.mkdir()
            (folder / MARKER).write_text("created by vmware-sandbox-mcp\n", encoding="utf-8")
            self._vmrun.clone(vmx, self._config.snapshot, sandbox_id)
            size = self._resolve_resources(vmx, memory_mb, cpus)
            harden_vmx(vmx, self._config.clones_dir, network, self._config.web_vnet, display, memory_mb, cpus)
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
                sandboxes[sandbox_id]["memory_mb"], sandboxes[sandbox_id]["cpus"] = size
        return sandbox_id

    def _destroy(self, sandbox_id: str) -> None:
        self._require(sandbox_id)
        self._teardown(sandbox_id)

    def _run(self, sandbox_id: str, command: str, timeout_seconds: int) -> RunResult:
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= MAX_RUN_TIMEOUT_SECONDS:
            raise SandboxError(f"timeout_seconds must be between 1 and {MAX_RUN_TIMEOUT_SECONDS}.")
        if not command.strip():
            raise SandboxError("command is empty.")
        record = self._require(sandbox_id)
        if record.get("status") != "ready":
            raise SandboxError(f"Sandbox {sandbox_id} is not ready.")
        return self._guest_exec(self._vmx(sandbox_id), command, timeout_seconds, self._proxy_env(record))

    def _guest_exec(self, vmx: Path, command: str, timeout_seconds: int, env: dict[str, str] | None = None) -> RunResult:
        """Run bash text in the guest. The text travels as file content, never on a command line."""
        token = secrets.token_hex(8)
        guest_script, guest_result = f"/tmp/.sbx-{token}.sh", f"/tmp/.sbx-{token}.tar"
        started = self._clock()
        # The wrapper deletes its own script, so on success only the result file is left.
        leftovers = [guest_script, guest_result]
        with tempfile.TemporaryDirectory(prefix="vmsandbox-run-") as tmp:
            host_script, host_result = Path(tmp) / "script.sh", Path(tmp) / "result.tar"
            host_script.write_bytes(build_script(command, env))
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

    # ---- checkpoints -----------------------------------------------------
    #
    # A checkpoint is a snapshot of the sandbox's disk taken while it is powered off. Saving the
    # memory too is possible, but on the test machine it took over three minutes and 2 GB per
    # checkpoint; a clean shutdown, a snapshot and a boot take well under a minute. So running
    # programs are not saved: the sandbox restarts when a checkpoint is saved or restored.

    @staticmethod
    def _checkpoint_name(name: str) -> str:
        if not isinstance(name, str) or not _CHECKPOINT_NAME.match(name):
            raise SandboxError(
                "A checkpoint name is 1 to 40 letters, digits, '-' or '_', starting with a letter or digit."
            )
        return name

    def _begin_busy(self, sandbox_id: str, check: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Mark a sandbox busy, so nothing else uses it meanwhile, once `check` has approved the request."""
        self._require(sandbox_id)
        with self._store.edit() as sandboxes:
            record = sandboxes.get(sandbox_id)
            if record is None:
                raise SandboxError(f"No sandbox with id {sandbox_id}. Use sandbox_list to see active ones.")
            if record.get("status") != "ready":
                raise SandboxError(f"Sandbox {sandbox_id} is not ready.")
            check(record)
            record["status"] = "busy"
            record["busy_since"] = self._clock()
            return dict(record)

    def _end_busy(self, sandbox_id: str, checkpoints: list[dict[str, Any]] | None = None) -> None:
        with self._store.edit() as sandboxes:
            record = sandboxes.get(sandbox_id)
            if record is not None:
                record["status"] = "ready"
                record.pop("busy_since", None)
                if checkpoints is not None:
                    record["checkpoints"] = checkpoints

    def _is_running(self, vmx: Path) -> bool:
        try:
            return any(same_path(vmx, path) for path in self._vmrun.list_running())
        except VmrunError:
            return False

    def _wait_stopped(self, vmx: Path, seconds: float = 120) -> None:
        deadline = self._clock() + seconds
        while self._is_running(vmx):
            if self._clock() >= deadline:
                raise SandboxError("The sandbox did not power off in time.")
            self._sleep(2)

    def _synced_checkpoints(self, vmx: Path, known: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The checkpoints VMware says exist, keeping the saved times of those already known."""
        times = {c["name"]: c.get("created_at") for c in known}
        return [
            {"name": name, "created_at": times.get(name) or self._clock()}
            for name in self._vmrun.list_checkpoints(vmx)
        ]

    def _bring_back(self, sandbox_id: str, vmx: Path, known: list[dict[str, Any]]) -> bool:
        """After a failed save or restore: get the sandbox running and usable again, or remove it."""
        try:
            if not self._is_running(vmx):
                self._vmrun.start(vmx)
            self._wait_ready(vmx)
            checkpoints = self._synced_checkpoints(vmx, known)
        except Exception:
            self._try_teardown(sandbox_id, "unusable after a failed checkpoint operation")
            return False
        self._end_busy(sandbox_id, checkpoints)
        return True

    def _checkpoint(self, sandbox_id: str, name: str) -> CheckpointResult:
        name = self._checkpoint_name(name)
        self._require(sandbox_id)
        vmx = self._vmx(sandbox_id)
        replacing = {"yes": False}

        def check(record: dict[str, Any]) -> None:
            names = [c["name"] for c in record.get("checkpoints", [])]
            replacing["yes"] = name in names
            if not replacing["yes"] and len(names) >= MAX_CHECKPOINTS:
                raise SandboxError(
                    f"This sandbox already holds {MAX_CHECKPOINTS} checkpoints ({', '.join(names)}). "
                    "Save under one of these names to replace it, or destroy the sandbox."
                )

        known = self._begin_busy(sandbox_id, check).get("checkpoints", [])
        try:
            self._vmrun.stop(vmx, soft=True)
            self._wait_stopped(vmx)
            if replacing["yes"]:
                self._vmrun.delete_checkpoint(vmx, name)
            self._vmrun.take_checkpoint(vmx, name)
            self._vmrun.start(vmx)
            self._wait_ready(vmx)
            checkpoints = self._synced_checkpoints(vmx, known)
        except Exception as exc:
            if self._bring_back(sandbox_id, vmx, known):
                raise SandboxError(
                    f"Could not save the checkpoint: {exc}. The sandbox is usable again, but programs "
                    "that were running may have been stopped."
                ) from None
            raise SandboxError(
                f"Could not save the checkpoint ({exc}), and the sandbox could not be brought back, "
                "so it was removed."
            ) from None
        self._end_busy(sandbox_id, checkpoints)
        return CheckpointResult(name, replacing["yes"], [c["name"] for c in checkpoints])

    def _restore(self, sandbox_id: str, name: str) -> list[str]:
        name = self._checkpoint_name(name)
        self._require(sandbox_id)
        vmx = self._vmx(sandbox_id)

        def check(record: dict[str, Any]) -> None:
            names = [c["name"] for c in record.get("checkpoints", [])]
            if name not in names:
                raise SandboxError(
                    f"No checkpoint named {name!r} in this sandbox (it has: {', '.join(names) or 'none'})."
                )

        known = self._begin_busy(sandbox_id, check).get("checkpoints", [])
        try:
            self._vmrun.stop(vmx)  # hard: whatever state it is in is being thrown away
            self._wait_stopped(vmx)
            self._vmrun.restore_checkpoint(vmx, name)
            self._vmrun.start(vmx)
            self._wait_ready(vmx)
        except Exception as exc:
            if self._bring_back(sandbox_id, vmx, known):
                raise SandboxError(
                    f"Could not restore the checkpoint: {exc}. The sandbox is usable again, but it may not "
                    "be in the saved state."
                ) from None
            raise SandboxError(
                f"Could not restore the checkpoint ({exc}), and the sandbox could not be brought back, "
                "so it was removed."
            ) from None
        self._end_busy(sandbox_id)
        return [c["name"] for c in known]

    # ---- keyboard and mouse ----------------------------------------------

    @staticmethod
    def _free_port(taken: set[int]) -> int:
        for _ in range(300):
            port = 15900 + secrets.randbelow(1000)
            if port in taken:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
        raise SandboxError("No free local port for the sandbox's keyboard and mouse.")

    def _with_input(self, sandbox_id: str, action: Callable[[RfbClient], T]) -> T:
        record = self._require(sandbox_id)
        if record.get("status") != "ready":
            raise SandboxError(f"Sandbox {sandbox_id} is not ready.")
        display = record.get("input")
        if not display:
            raise SandboxError(
                "Keyboard and mouse input is not available for this sandbox. The user has to switch it on "
                "(VMSANDBOX_REMOTE_INPUT) and the sandbox has to be created after that."
            )
        try:
            client = self._input_connect(display["port"], display["password"])
        except InputError as exc:
            raise SandboxError(str(exc)) from None
        try:
            return action(client)
        except InputError as exc:
            raise SandboxError(str(exc)) from None
        except OSError as exc:
            raise SandboxError(f"Lost the connection to the sandbox's keyboard and mouse ({exc}).") from None
        finally:
            client.close()

    # ---- background commands ---------------------------------------------

    def _start(self, sandbox_id: str, command: str) -> str:
        if not isinstance(command, str) or not command.strip():
            raise SandboxError("command is empty.")
        record = self._require(sandbox_id)
        if record.get("status") != "ready":
            raise SandboxError(f"Sandbox {sandbox_id} is not ready.")
        vmx = self._vmx(sandbox_id)
        job_id = jobs.new_job_id()
        guest_command = jobs.command_path(job_id)
        with tempfile.TemporaryDirectory(prefix="vmsandbox-job-") as tmp:
            host_command = Path(tmp) / "cmd.sh"
            host_command.write_bytes((command.replace("\r\n", "\n") + "\n").encode("utf-8"))
            try:
                self._vmrun.copy_to_guest(vmx, host_command, guest_command)
            except (VmrunError, OSError) as exc:
                raise SandboxError(f"Could not start the command in the sandbox: {exc}") from exc
        outcome = ""
        try:
            outcome = jobs.first_word(
                self._guest_exec(vmx, jobs.start_script(job_id), JOB_STEP_TIMEOUT_SECONDS, self._proxy_env(record))
            )
        finally:
            if outcome != "started":
                self._vmrun.delete_guest_file(vmx, guest_command)
        if outcome == "limit":
            raise SandboxError(
                f"This sandbox already has {jobs.MAX_JOBS_PER_SANDBOX} background commands running. "
                "Stop one with sandbox_stop first."
            )
        if outcome != "started":
            raise SandboxError("The sandbox could not start the command.")
        return job_id

    def _job_step(self, sandbox_id: str, job_id: str, script: Callable[[str], str]) -> RunResult:
        vmx = self._ready_vmx(sandbox_id)
        try:
            text = script(job_id)  # validates the job id before it is put into a script
        except JobError as exc:
            raise SandboxError(str(exc)) from None
        return self._guest_exec(vmx, text, JOB_STEP_TIMEOUT_SECONDS)

    def _check(self, sandbox_id: str, job_id: str) -> JobStatus:
        result = self._job_step(sandbox_id, job_id, jobs.check_script)
        try:
            return jobs.parse_check(job_id, result)
        except JobError as exc:
            raise SandboxError(str(exc)) from None

    def _stop(self, sandbox_id: str, job_id: str) -> str:
        outcome = jobs.first_word(self._job_step(sandbox_id, job_id, jobs.stop_script))
        if outcome == "missing":
            raise SandboxError(f"No background job {job_id} in this sandbox.")
        if outcome not in ("stopped", "finished"):
            raise SandboxError("The sandbox could not stop the command.")
        return outcome

    def _proxy_env(self, record: dict[str, Any]) -> dict[str, str]:
        """Point programs in a "web" sandbox at the filtering proxy; it is their only way out."""
        if record.get("network") != "web" or not self._config.web_proxy_host:
            return {}
        url = f"http://{self._config.web_proxy_host}:{self._config.web_proxy_port}"
        names = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
        return {**{name: url for name in names}, "no_proxy": "localhost,127.0.0.1", "NO_PROXY": "localhost,127.0.0.1"}

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
            else:  # "creating" or "busy": another server may be mid-operation, so only reap stuck ones
                if record.get("status") == "busy":
                    dead = now - record.get("busy_since", 0) > self._config.boot_timeout_seconds + 1200
                else:
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

    def _guest_folder_step(self, vmx: Path, command: str, what: str) -> None:
        """Run one server-written tar command in the guest (the only agent text in it is a quoted path)."""
        result = self._guest_exec(vmx, command, FOLDER_TIMEOUT_SECONDS)
        if result.exit_code != 0:
            detail = result.stderr.strip().splitlines()[-1:] or ["no details"]
            raise SandboxError(f"Could not {what}: {detail[0]}")

    def _copy_in(self, sandbox_id: str, host_path: str, guest_path: str) -> Transfer:
        vmx = self._ready_vmx(sandbox_id)
        source = self._host_path(host_path)
        target = self._guest_path(guest_path)
        if source.is_dir():
            return self._copy_folder_in(vmx, source, target)
        if not source.is_file():
            raise SandboxError(f"Nothing at {host_path!r} in the transfer folder.")
        try:
            self._vmrun.copy_to_guest(vmx, source, target)
        except VmrunError as exc:
            raise SandboxError(f"Could not copy the file into the sandbox: {exc}") from exc
        return Transfer("file", 1, source.stat().st_size)

    def _copy_folder_in(self, vmx: Path, source: Path, target: str) -> Transfer:
        guest_tar = f"/tmp/.sbx-{secrets.token_hex(8)}.dir.tar"
        with tempfile.TemporaryDirectory(prefix="vmsandbox-dir-") as tmp:
            host_tar = Path(tmp) / "folder.tar"
            try:
                files, size = pack_folder(source, host_tar)
                self._vmrun.copy_to_guest(vmx, host_tar, guest_tar)
            except FolderError as exc:
                raise SandboxError(str(exc)) from None
            except (VmrunError, OSError) as exc:
                raise SandboxError(f"Could not copy the folder into the sandbox: {exc}") from exc
        quoted = shlex.quote(target)
        try:
            self._guest_folder_step(
                vmx,
                f"mkdir -p -- {quoted} && tar -xf {guest_tar} -C {quoted}",
                "unpack the folder in the sandbox",
            )
        finally:
            self._vmrun.delete_guest_file(vmx, guest_tar)
        return Transfer("folder", files, size)

    def _copy_out(self, sandbox_id: str, guest_path: str, host_path: str) -> Transfer:
        vmx = self._ready_vmx(sandbox_id)
        source = self._guest_path(guest_path)
        target = self._host_path(host_path)
        if self._vmrun.guest_dir_exists(vmx, source):
            return self._copy_folder_out(vmx, source, target, host_path)
        if target.is_dir():
            raise SandboxError(f"{host_path!r} is a folder; give a file name inside it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._vmrun.copy_from_guest(vmx, source, target)
        except VmrunError as exc:
            raise SandboxError(f"Could not copy the file out of the sandbox: {exc}") from exc
        return Transfer("file", 1, target.stat().st_size, target)

    def _copy_folder_out(self, vmx: Path, source: str, target: Path, host_path: str) -> Transfer:
        if target.exists() and not target.is_dir():
            raise SandboxError(f"{host_path!r} is an existing file, but the sandbox path is a folder.")
        guest_tar = f"/tmp/.sbx-{secrets.token_hex(8)}.dir.tar"
        try:
            self._guest_folder_step(
                vmx,
                # --hard-dereference: store hard-linked files as ordinary files, not as links.
                f"tar --hard-dereference -cf {guest_tar} -C {shlex.quote(source)} .",
                "pack the folder in the sandbox",
            )
            with tempfile.TemporaryDirectory(prefix="vmsandbox-dir-") as tmp:
                host_tar = Path(tmp) / "folder.tar"
                try:
                    self._vmrun.copy_from_guest(vmx, guest_tar, host_tar)
                    files, size, skipped = unpack_folder(host_tar, target)
                except FolderError as exc:
                    raise SandboxError(str(exc)) from None
                except (VmrunError, OSError, tarfile.TarError) as exc:
                    raise SandboxError(f"Could not copy the folder out of the sandbox: {exc}") from exc
        finally:
            self._vmrun.delete_guest_file(vmx, guest_tar)
        return Transfer("folder", files, size, target, skipped)

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
                "memory_mb": record.get("memory_mb"),
                "cpus": record.get("cpus"),
                "checkpoints": [c["name"] for c in record.get("checkpoints", [])],
            }
            for sandbox_id, record in sorted(self._store.all().items())
        ]
