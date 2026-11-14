"""`vmware-sandbox-mcp doctor`: check the setup and say what to fix.

Uses the same Config, VmrunClient and SandboxManager as the server, so it is bound by
the same guard: the base VM is only read and cloned from, and the boot test only ever
touches a sandbox it created itself.
"""

from __future__ import annotations

import argparse
import re
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .audit import AuditLog
from .config import Config, ConfigError, load_config
from .manager import SandboxError, SandboxManager
from .state import StateStore
from .vmrun import Runner, VmrunClient, VmrunError, subprocess_runner

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
_PROBE = "echo doctor-ok; if sudo -n true 2>/dev/null; then echo sudo-yes; else echo sudo-no; fi"


def snapshot_powered_on(base_vmx: Path, snapshot: str) -> bool | None:
    """Read the base VM's snapshot list file. None means "could not tell"."""
    try:
        text = base_vmx.with_suffix(".vmsd").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    entries: dict[str, dict[str, str]] = {}
    for match in re.finditer(r'^(snapshot\d+)\.(\w+)\s*=\s*"(.*)"\s*$', text, re.MULTILINE):
        entries.setdefault(match.group(1), {})[match.group(2)] = match.group(3)
    for entry in entries.values():
        if entry.get("displayName") == snapshot:
            if entry.get("type") == "1":
                return True
            filename = entry.get("filename", "")
            return bool(filename) and (base_vmx.parent / filename).with_suffix(".vmem").exists()
    return None


class Doctor:
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        boot: bool = True,
        runner: Runner = subprocess_runner,
        out: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._env, self._boot, self._runner = env, boot, runner
        self._out, self._clock, self._sleep = out, clock, sleep
        self.results: list[tuple[str, str]] = []

    def _report(self, status: str, title: str, detail: str = "", fix: str = "") -> bool:
        self.results.append((status, title))
        self._out(f"[{status:^4}] {title}" + (f": {detail}" if detail else ""))
        if fix:
            self._out(f"       Fix: {fix}")
        return status in (OK, WARN)

    def run(self) -> int:
        """Run every check. Returns the process exit code: 0 if nothing failed."""
        config = self._check_settings()
        if config is not None:
            vmrun = VmrunClient(
                config.vmrun_path,
                config.base_vmx,
                config.clones_dir,
                config.guest_user,
                config.guest_password,
                runner=self._runner,
            )
            ready = self._check_vmrun(vmrun)
            ready = ready and self._check_snapshot(config, vmrun)
            ready = self._check_folders(config) and ready
            if not self._boot:
                self._report(SKIP, "Sandbox boot test", "skipped because of --no-boot")
            elif not ready:
                self._report(SKIP, "Sandbox boot test", "skipped until the problems above are fixed")
            else:
                self._check_boot(config, vmrun)

        failed = sum(1 for status, _ in self.results if status == FAIL)
        self._out("")
        self._out("Everything needed is working." if not failed else f"{failed} problem(s) found.")
        return 1 if failed else 0

    # ---- individual checks -----------------------------------------------

    def _check_settings(self) -> Config | None:
        try:
            config = load_config(self._env)
        except ConfigError as exc:
            self._report(FAIL, "Settings", str(exc), "correct the setting named above.")
            return None
        self._report(OK, "Settings", f"base VM {config.base_vmx}")
        return config

    def _check_vmrun(self, vmrun: VmrunClient) -> bool:
        try:
            vmrun.list_running()
        except (VmrunError, OSError) as exc:
            return self._report(
                FAIL,
                "vmrun",
                str(exc),
                "check that VMware Workstation is installed, or set VMSANDBOX_VMRUN_PATH.",
            )
        return self._report(OK, "vmrun", "responds")

    def _check_snapshot(self, config: Config, vmrun: VmrunClient) -> bool:
        try:
            names = vmrun.list_snapshots()
        except VmrunError as exc:
            return self._report(FAIL, "Snapshot", str(exc))
        if config.snapshot not in names:
            found = ", ".join(repr(name) for name in names) or "none"
            return self._report(
                FAIL,
                "Snapshot",
                f"the base VM has no snapshot named {config.snapshot!r} (found: {found})",
                "set VMSANDBOX_SNAPSHOT to one of the names found, or take that snapshot.",
            )
        if snapshot_powered_on(config.base_vmx, config.snapshot):
            return self._report(
                FAIL,
                "Snapshot",
                f"{config.snapshot!r} was taken while the VM was running",
                "shut the base VM down completely and take a new snapshot.",
            )
        return self._report(OK, "Snapshot", f"{config.snapshot!r} exists and is powered off")

    def _check_folders(self, config: Config) -> bool:
        good = True
        for title, folder in (("Clones folder", config.clones_dir), ("Transfer folder", config.transfer_dir)):
            probe = folder / f".doctor-{secrets.token_hex(4)}"
            try:
                probe.write_text("x", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                good = self._report(FAIL, title, f"cannot write to {folder} ({exc})", "check its permissions.")
            else:
                self._report(OK, title, f"{folder} is writable")
        if config.audit_log is not None:
            try:
                with open(config.audit_log, "a", encoding="utf-8"):
                    pass
            except OSError as exc:
                good = self._report(
                    FAIL, "Audit log", f"cannot write to {config.audit_log} ({exc})", "check VMSANDBOX_AUDIT_LOG."
                )
            else:
                self._report(OK, "Audit log", f"{config.audit_log} is writable")
        return good

    def _check_boot(self, config: Config, vmrun: VmrunClient) -> None:
        manager = SandboxManager(
            config,
            vmrun,
            StateStore(config.clones_dir),
            clock=self._clock,
            sleep=self._sleep,
            audit=AuditLog(config.audit_log, self._clock),
        )
        self._out("       Creating a test sandbox (this can take a minute)...")
        started = self._clock()
        try:
            sandbox_id = manager.create("none")
        except SandboxError as exc:
            self._report(FAIL, "Sandbox boot test", str(exc))
            return
        self._report(OK, "Sandbox boot test", f"ready in {self._clock() - started:.0f}s, login accepted")
        try:
            self._check_guest(manager, sandbox_id, config)
        finally:
            try:
                manager.destroy(sandbox_id)
            except SandboxError as exc:
                self._report(FAIL, "Cleanup", f"could not destroy test sandbox {sandbox_id}: {exc}")
            else:
                self._report(OK, "Cleanup", "test sandbox destroyed")

    def _check_guest(self, manager: SandboxManager, sandbox_id: str, config: Config) -> None:
        try:
            result = manager.run(sandbox_id, _PROBE, 30)
        except SandboxError as exc:
            self._report(
                FAIL,
                "Running commands",
                str(exc),
                "make sure bash, timeout and tar exist in the base VM.",
            )
            return
        lines = result.stdout.split()
        if "doctor-ok" not in lines:
            self._report(FAIL, "Running commands", "the test command produced no output")
            return
        self._report(OK, "Running commands", "works")
        if "sudo-yes" in lines:
            self._report(OK, "Installing software", "passwordless sudo is available")
        else:
            self._report(
                WARN,
                "Installing software",
                f"user {config.guest_user!r} has no passwordless sudo, so sandboxes cannot install packages",
                "optional. See 'Base VM setup' in the README.",
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vmware-sandbox-mcp doctor",
        description="Check the vmware-sandbox-mcp setup and say what to fix.",
    )
    parser.add_argument(
        "--no-boot",
        action="store_true",
        help="only check settings, vmrun, the snapshot and the folders; do not create a test sandbox",
    )
    args = parser.parse_args(argv)
    return Doctor(boot=not args.no_boot).run()
