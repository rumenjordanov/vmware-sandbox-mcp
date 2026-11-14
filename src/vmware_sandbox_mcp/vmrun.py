"""The only place vmrun is ever spawned.

Every call goes through VmrunClient._run, which enforces:
  * the command is on an allowlist;
  * the base VM may only be read (listSnapshots) or cloned from;
  * every other command targets a .vmx at <clones_dir>/<sandbox>/<file>.vmx;
  * a clone's destination is such a path too.
vmrun is invoked with an argument list, never through a shell.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .paths import resolve, same_path

# (returncode, combined output) <- (argv, timeout_seconds)
Runner = Callable[[Sequence[str], float], tuple[int, str]]

# command -> which VM it may target
_BASE, _CLONE, _NONE = "base", "clone", "none"
_COMMANDS: dict[str, str] = {
    "list": _NONE,
    "listSnapshots": _BASE,
    "clone": _BASE,
    "start": _CLONE,
    "stop": _CLONE,
    "deleteVM": _CLONE,
    "checkToolsState": _CLONE,
    "runProgramInGuest": _CLONE,
    "CopyFileFromHostToGuest": _CLONE,
    "CopyFileFromGuestToHost": _CLONE,
    "deleteFileInGuest": _CLONE,
    "directoryExistsInGuest": _CLONE,
    "captureScreen": _CLONE,
}
_GUEST_COMMANDS = {
    "captureScreen",
    "runProgramInGuest",
    "CopyFileFromHostToGuest",
    "CopyFileFromGuestToHost",
    "deleteFileInGuest",
    "directoryExistsInGuest",
}


class VmrunError(Exception):
    """vmrun ran and reported a failure."""


class VmrunTimeout(VmrunError):
    pass


class ForbiddenTargetError(VmrunError):
    """The call was refused before vmrun was started."""


def subprocess_runner(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            shell=False,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        raise VmrunTimeout(f"vmrun did not finish within {timeout:.0f}s") from None
    return done.returncode, done.stdout.decode("utf-8", errors="replace")


class VmrunClient:
    def __init__(
        self,
        vmrun_path: str | os.PathLike[str],
        base_vmx: str | os.PathLike[str],
        clones_dir: str | os.PathLike[str],
        guest_user: str = "",
        guest_password: str = "",
        runner: Runner = subprocess_runner,
    ) -> None:
        self._vmrun = str(vmrun_path)
        self._base_vmx = resolve(base_vmx)
        self._clones_dir = resolve(clones_dir)
        self._guest_user = guest_user
        self._guest_password = guest_password
        self._runner = runner

    # ---- the guard -------------------------------------------------------

    def _checked_base(self, vmx: str | os.PathLike[str]) -> str:
        if not same_path(vmx, self._base_vmx):
            raise ForbiddenTargetError(f"Refused: {vmx} is not the configured base VM.")
        return str(self._base_vmx)

    def _checked_clone(self, vmx: str | os.PathLike[str]) -> str:
        path = resolve(vmx)
        inside = same_path(path.parent.parent, self._clones_dir)
        if not inside or path.suffix.lower() != ".vmx":
            raise ForbiddenTargetError(
                f"Refused: {vmx} is not a sandbox .vmx directly inside {self._clones_dir}."
            )
        return str(path)

    def _run(
        self,
        command: str,
        vmx: str | os.PathLike[str] | None = None,
        *rest: str,
        timeout: float = 60,
        check: bool = True,
    ) -> tuple[int, str]:
        role = _COMMANDS.get(command)
        if role is None:
            raise ForbiddenTargetError(f"Refused: vmrun command {command!r} is not allowed.")

        argv = [self._vmrun, "-T", "ws"]
        if command in _GUEST_COMMANDS:
            argv += ["-gu", self._guest_user, "-gp", self._guest_password]
        argv.append(command)

        if role == _NONE:
            if vmx is not None or rest:
                raise ForbiddenTargetError(f"Refused: {command} takes no arguments.")
        elif role == _BASE:
            argv.append(self._checked_base(vmx or ""))
        else:
            argv.append(self._checked_clone(vmx or ""))

        rest_args = list(rest)
        if command == "clone":
            if not rest_args:
                raise ForbiddenTargetError("Refused: clone needs a destination.")
            rest_args[0] = self._checked_clone(rest_args[0])
        argv += rest_args

        code, output = self._runner(argv, timeout)
        output = output.strip()
        if check and code != 0:
            # Built from command + vmrun output only, so credentials never leak into errors.
            raise VmrunError(f"vmrun {command} failed (exit {code}): {output or 'no output'}")
        return code, output

    # ---- base VM: read and clone-from only -------------------------------

    def list_snapshots(self) -> list[str]:
        _, out = self._run("listSnapshots", self._base_vmx)
        return [line.strip() for line in out.splitlines()[1:] if line.strip()]

    def clone(self, dest_vmx: str | os.PathLike[str], snapshot: str, name: str) -> None:
        self._run(
            "clone",
            self._base_vmx,
            str(dest_vmx),
            "linked",
            f"-snapshot={snapshot}",
            f"-cloneName={name}",
            timeout=300,
        )

    # ---- sandbox clones --------------------------------------------------

    def start(self, vmx: str | os.PathLike[str]) -> None:
        self._run("start", vmx, "nogui", timeout=180)

    def stop(self, vmx: str | os.PathLike[str]) -> None:
        self._run("stop", vmx, "hard", timeout=90)

    def delete_vm(self, vmx: str | os.PathLike[str]) -> None:
        self._run("deleteVM", vmx, timeout=120)

    def tools_running(self, vmx: str | os.PathLike[str]) -> bool:
        _, out = self._run("checkToolsState", vmx, timeout=30, check=False)
        return out.strip().lower() == "running"

    def run_program(self, vmx: str | os.PathLike[str], program: str, *args: str, timeout: float = 60) -> int:
        """Run a program in the guest and return vmrun's exit code (0 = success)."""
        code, _ = self._run("runProgramInGuest", vmx, program, *args, timeout=timeout, check=False)
        return code

    def probe_guest(self, vmx: str | os.PathLike[str]) -> tuple[bool, str]:
        """Try a no-op program in the guest. Returns (worked, vmrun's message)."""
        code, out = self._run("runProgramInGuest", vmx, "/bin/true", timeout=60, check=False)
        return code == 0, out

    def copy_to_guest(self, vmx: str | os.PathLike[str], host_path: str | os.PathLike[str], guest_path: str) -> None:
        self._run("CopyFileFromHostToGuest", vmx, str(host_path), guest_path, timeout=300)

    def copy_from_guest(self, vmx: str | os.PathLike[str], guest_path: str, host_path: str | os.PathLike[str]) -> None:
        self._run("CopyFileFromGuestToHost", vmx, guest_path, str(host_path), timeout=300)

    def delete_guest_file(self, vmx: str | os.PathLike[str], guest_path: str) -> None:
        self._run("deleteFileInGuest", vmx, guest_path, timeout=30, check=False)

    def guest_dir_exists(self, vmx: str | os.PathLike[str], guest_path: str) -> bool:
        code, _ = self._run("directoryExistsInGuest", vmx, guest_path, timeout=30, check=False)
        return code == 0

    def capture_screen(self, vmx: str | os.PathLike[str], host_png: str | os.PathLike[str]) -> None:
        self._run("captureScreen", vmx, str(host_png), timeout=60)

    # ---- host-wide, read-only --------------------------------------------

    def list_running(self) -> list[Path]:
        _, out = self._run("list")
        return [Path(line.strip()) for line in out.splitlines()[1:] if line.strip()]
