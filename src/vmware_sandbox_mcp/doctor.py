"""`vmware-sandbox-mcp doctor`: check the setup and say what to fix.

Uses the same Config, VmrunClient and SandboxManager as the server, so it is bound by
the same guard: the base VM is only read and cloned from, and the boot test only ever
touches a sandbox it created itself.
"""

from __future__ import annotations

import argparse
import re
import secrets
import socket
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .audit import AuditLog
from .config import Config, ConfigError, load_config
from .manager import SandboxError, SandboxManager
from .proxy import WebProxy, host_addresses
from .state import StateStore
from .vmrun import Runner, VmrunClient, VmrunError, subprocess_runner

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
_PROBE = "echo doctor-ok; if sudo -n true 2>/dev/null; then echo sudo-yes; else echo sudo-no; fi"

# Run inside a "web" sandbox. __H__/__P__ are the proxy's address and port (from settings).
_WEB_PROBE = r"""H=__H__; P=__P__
ask() { # name url host -> name=<http status>, or name=refused when the proxy itself said no
  reply=$(timeout 25 bash -c 'exec 3<>/dev/tcp/'"$H"'/'"$P"' && printf "GET %s HTTP/1.1\r\nHost: %s\r\n\r\n" "$0" "$1" >&3 && head -c 2000 <&3' "$2" "$3" 2>/dev/null | tr -d '\r')
  case "$reply" in
    *"vmware-sandbox-mcp proxy:"*) echo "$1=refused" ;;
    *) echo "$1=$(echo "$reply" | head -n1 | cut -d' ' -f2)" ;;
  esac
}
port() {
  if timeout 3 bash -c "exec 3<>/dev/tcp/$H/$1" 2>/dev/null; then echo "port$1=open"; else echo "port$1=closed"; fi
}
ask public http://__PUBLIC__/ __PUBLIC__
ask unlisted http://allowlist-check.invalid/ allowlist-check.invalid
ask private http://10.0.0.1/ 10.0.0.1
ask hostself "http://$H/" "$H"
ask badport http://example.com:22/ example.com
port 135; port 139; port 445; port "$P"
if ip -4 route show default | grep -q .; then echo route=yes; else echo route=no; fi
"""


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
        proxy_factory: Callable[[Config], WebProxy] | None = None,
        manager_options: dict | None = None,
    ) -> None:
        self._manager_options = manager_options or {}
        self._proxy_factory = proxy_factory or (
            lambda config: WebProxy(
                config.web_proxy_host or "",
                config.web_proxy_port,
                config.web_ports,
                AuditLog(config.audit_log),
                allowed_hosts=config.web_allow,
            )
        )
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
                for name, _ in config.images:
                    if name != config.snapshot:
                        self._check_image_boot(config, vmrun, name)
                if config.web_vnet:
                    self._check_web(config, vmrun)

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
        good = self._report(OK, "Snapshot", f"{config.snapshot!r} exists and is powered off")
        for name, _ in config.images:
            if name == config.snapshot:
                continue
            if name not in names:
                good = self._report(
                    FAIL,
                    f"Image {name!r}",
                    f"the base VM has no snapshot with that name (found: {', '.join(repr(n) for n in names) or 'none'})",
                    "take that snapshot, or remove the entry from VMSANDBOX_IMAGES.",
                ) and good
            elif snapshot_powered_on(config.base_vmx, name):
                good = self._report(
                    FAIL,
                    f"Image {name!r}",
                    "it was taken while the VM was running",
                    "shut the base VM down completely and take the snapshot again.",
                ) and good
            else:
                self._report(OK, f"Image {name!r}", "exists and is powered off")
        return good

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
        manager = self._manager(config, vmrun)
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

    def _manager(self, config: Config, vmrun: VmrunClient) -> SandboxManager:
        return SandboxManager(
            config,
            vmrun,
            StateStore(config.clones_dir),
            clock=self._clock,
            sleep=self._sleep,
            audit=AuditLog(config.audit_log, self._clock, redact=(config.guest_password,)),
            **self._manager_options,
        )

    def _check_web(self, config: Config, vmrun: VmrunClient) -> None:
        """network="web": the proxy works, and nothing but the proxy is reachable."""
        where = f"{config.web_proxy_host}:{config.web_proxy_port}"
        proxy = self._proxy_factory(config)
        try:
            proxy.start()
        except OSError as exc:
            if getattr(exc, "errno", None) in (98, 10048) or getattr(exc, "winerror", None) == 10048:
                proxy = None  # a running server instance already serves it
                self._report(OK, "Web proxy", f"{where} is already served by a running server")
            else:
                self._report(
                    FAIL,
                    "Web proxy",
                    f"cannot listen on {where} ({exc})",
                    f"VMSANDBOX_WEB_PROXY_HOST must be this computer's address on {config.web_vnet}.",
                )
                return
        else:
            self._report(OK, "Web proxy", f"listening on {where}")

        manager = self._manager(config, vmrun)
        self._out("       Creating a test sandbox with network 'web'...")
        try:
            try:
                sandbox_id = manager.create("web")
            except SandboxError as exc:
                self._report(FAIL, "Web sandbox", str(exc))
                return
            try:
                probe = (
                    _WEB_PROBE.replace("__H__", config.web_proxy_host or "")
                    .replace("__P__", str(config.web_proxy_port))
                    .replace("__PUBLIC__", self._public_host(config) or "example.com")
                )
                self._sleep(5)  # give the guest a moment to get its network address
                result = manager.run(sandbox_id, probe, 180)
                self._judge_web(dict(line.split("=", 1) for line in result.stdout.split() if "=" in line), config)
            except SandboxError as exc:
                self._report(FAIL, "Web sandbox", str(exc))
            finally:
                try:
                    manager.destroy(sandbox_id)
                except SandboxError as exc:
                    self._report(FAIL, "Cleanup", f"could not destroy test sandbox {sandbox_id}: {exc}")
        finally:
            if proxy is not None:
                proxy.stop()

    @staticmethod
    def _public_host(config: Config) -> str | None:
        """The site to try: example.com normally, or the first plainly named site on the list."""
        if config.web_allow is None:
            return "example.com"
        return next((entry for entry in config.web_allow if not entry.startswith("*.")), None)

    def _judge_web(self, seen: dict[str, str], config: Config) -> None:
        setup = "run scripts\\setup-web-network.ps1 as administrator (see docs/setup.md)."
        if seen.get(f"port{config.web_proxy_port}") != "open":
            self._report(
                FAIL,
                "Web sandbox reaches the proxy",
                "a sandbox on the web network cannot connect to the proxy",
                f"check that {config.web_vnet} has DHCP enabled, and {setup}",
            )
            return
        self._report(OK, "Web sandbox reaches the proxy")

        public_host = self._public_host(config)
        public = seen.get("public", "")
        if public_host is None:
            self._report(SKIP, "Web access through the proxy", "the site list has only wildcard entries, so no site was tried")
        elif public[:1] in ("2", "3"):
            self._report(OK, "Web access through the proxy", f"{public_host} answered {public}")
        else:
            self._report(
                FAIL,
                "Web access through the proxy",
                f"{public_host} answered {public or 'nothing'}",
                "check this computer's own internet connection"
                + (" and that the site is really on VMSANDBOX_WEB_ALLOW." if config.web_allow else "."),
            )

        if config.web_allow is not None:
            if seen.get("unlisted") == "refused":
                self._report(OK, "Site list", f"sites not on the list are refused ({len(config.web_allow)} listed)")
            else:
                self._report(
                    FAIL,
                    "Site list",
                    "a site that is not on VMSANDBOX_WEB_ALLOW was not refused",
                    "this is a bug in the proxy; do not rely on the list.",
                )

        refused = {name: seen.get(name, "") for name in ("private", "hostself", "badport")}
        if all(answer == "refused" for answer in refused.values()):
            self._report(OK, "Proxy refuses private addresses and other ports")
        else:
            self._report(FAIL, "Proxy refuses private addresses and other ports", f"got {refused}")

        open_ports = [port for port in ("135", "139", "445") if seen.get(f"port{port}") != "closed"]
        if open_ports:
            self._report(
                FAIL,
                "Host is shielded from web sandboxes",
                f"host ports {', '.join(open_ports)} are reachable from the sandbox",
                setup,
            )
        else:
            self._report(OK, "Host is shielded from web sandboxes", "file sharing and RPC ports are blocked")

        if seen.get("route") == "no":
            self._report(OK, "No direct route out", "the sandbox has no default gateway")
        else:
            self._report(
                FAIL,
                "No direct route out",
                "the sandbox has a default gateway, so it may bypass the proxy",
                f"{config.web_vnet} must be a host-only network without NAT.",
            )

    def _check_input(self, manager: SandboxManager, sandbox_id: str) -> None:
        try:
            manager.press_keys(sandbox_id, "shift")
        except SandboxError as exc:
            self._report(FAIL, "Keyboard and mouse", str(exc))
            return
        self._report(OK, "Keyboard and mouse", "the sandbox accepts input")
        port = manager.remote_display_port(sandbox_id)
        exposed = [str(address) for address in sorted(host_addresses(), key=str) if self._reachable(str(address), port)]
        if exposed:
            self._report(
                FAIL,
                "Remote display is local only",
                f"the sandbox's remote display is reachable at {', '.join(exposed)}",
                "do not use VMSANDBOX_REMOTE_INPUT on this computer and report this as a bug.",
            )
        else:
            self._report(OK, "Remote display is local only", "reachable from this computer only (127.0.0.1)")

    @staticmethod
    def _reachable(address: str, port: int | None) -> bool:
        if port is None or address.startswith("127.") or address == "::1":
            return False
        try:
            socket.create_connection((address, port), timeout=2).close()
        except OSError:
            return False
        return True

    def _check_image_boot(self, config: Config, vmrun: VmrunClient, name: str) -> None:
        manager = self._manager(config, vmrun)
        started = self._clock()
        try:
            sandbox_id = manager.create("none", image=name)
        except SandboxError as exc:
            self._report(FAIL, f"Image {name!r} boots", str(exc))
            return
        self._report(OK, f"Image {name!r} boots", f"ready in {self._clock() - started:.0f}s, login accepted")
        try:
            manager.destroy(sandbox_id)
        except SandboxError as exc:
            self._report(FAIL, "Cleanup", f"could not destroy test sandbox {sandbox_id}: {exc}")

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
        if config.remote_input:
            self._check_input(manager, sandbox_id)
        if "sudo-yes" in lines:
            self._report(OK, "Installing software", "passwordless sudo is available")
        else:
            self._report(
                WARN,
                "Installing software",
                f"user {config.guest_user!r} has no passwordless sudo, so sandboxes cannot install packages",
                "optional. Run scripts/prepare-base-vm.sh inside the base VM (see docs/setup.md), then take a new snapshot.",
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
