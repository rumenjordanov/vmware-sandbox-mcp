"""Configuration from environment variables. Fails closed: no guessing."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .paths import is_within, overlaps, resolve, same_path

_VMRUN_CANDIDATES = (
    r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
    r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    vmrun_path: Path
    base_vmx: Path
    snapshot: str
    clones_dir: Path
    transfer_dir: Path
    guest_user: str
    guest_password: str
    max_sandboxes: int = 3
    max_age_minutes: int = 120
    boot_timeout_seconds: int = 180
    max_memory_mb: int | None = None  # the most an agent may ask for; None means the base VM's own size
    max_cpus: int | None = None
    audit_log: Path | None = None
    # network="web": private VMware network + filtering proxy. Off unless both are set.
    web_vnet: str | None = None
    web_proxy_host: str | None = None
    web_proxy_port: int = 3128
    web_ports: tuple[int, ...] = (80, 443)
    # Keyboard and mouse tools: gives every sandbox a password-protected remote display on 127.0.0.1.
    remote_input: bool = False


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Environment variable {name} is required.")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from None
    if value < 1:
        raise ConfigError(f"{name} must be at least 1.")
    return value


def _optional_int(env: Mapping[str, str], name: str, minimum: int, maximum: int) -> int | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be from {minimum} to {maximum}.")
    return value


def _find_vmrun(env: Mapping[str, str]) -> Path:
    explicit = env.get("VMSANDBOX_VMRUN_PATH", "").strip()
    candidates = [explicit] if explicit else [*_VMRUN_CANDIDATES, shutil.which("vmrun") or ""]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise ConfigError("vmrun not found. Set VMSANDBOX_VMRUN_PATH to the full path of vmrun.exe.")


def _switch(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    raise ConfigError(f"{name} must be on or off, got {raw!r}.")


def vnet_problem(vnet: str) -> str | None:
    """Why this VMware network must not be used for "web" sandboxes, or None if it is fine.

    Reads VMware's own (read-only) record of the network. A network with NAT would give
    sandboxes a direct route out, bypassing the proxy, so it is refused.
    """
    if sys.platform != "win32":
        return None
    import winreg

    base = rf"SOFTWARE\WOW6432Node\VMware, Inc.\VMnetLib\VMnetConfig\{vnet.lower()}"

    def enabled(subkey: str, value: str) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{subkey}") as key:
                return winreg.QueryValueEx(key, value)[0] == 1
        except OSError:
            return False

    if enabled("NAT", "UseNAT"):
        return f"{vnet} is a NAT network; sandboxes on it would bypass the proxy."
    if not enabled("Adapter", "UseAdapter"):
        return f"{vnet} is not set up as a host-only network with a host adapter."
    return None


def _web_settings(env: Mapping[str, str]) -> dict[str, object]:
    vnet = env.get("VMSANDBOX_WEB_VNET", "").strip()
    host = env.get("VMSANDBOX_WEB_PROXY_HOST", "").strip()
    if not vnet and not host:
        return {}
    if not vnet or not host:
        raise ConfigError("VMSANDBOX_WEB_VNET and VMSANDBOX_WEB_PROXY_HOST must be set together.")

    match = re.fullmatch(r"vmnet(\d{1,2})", vnet, re.IGNORECASE)
    number = int(match.group(1)) if match else -1
    if not 2 <= number <= 19 or number == 8:
        raise ConfigError(
            "VMSANDBOX_WEB_VNET must be a dedicated host-only network, VMnet2 to VMnet19. "
            "VMnet0 (bridged), VMnet1 (shared host-only) and VMnet8 (NAT) are refused."
        )
    vnet = f"VMnet{number}"
    problem = vnet_problem(vnet)
    if problem:
        raise ConfigError(f"VMSANDBOX_WEB_VNET: {problem}")

    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        raise ConfigError("VMSANDBOX_WEB_PROXY_HOST must be an IPv4 address, like 192.168.50.1.") from None
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
        raise ConfigError(
            "VMSANDBOX_WEB_PROXY_HOST must be this computer's private address on the sandbox network."
        )

    port = _positive_int(env, "VMSANDBOX_WEB_PROXY_PORT", 3128)
    if port > 65535:
        raise ConfigError("VMSANDBOX_WEB_PROXY_PORT must be at most 65535.")
    raw_ports = env.get("VMSANDBOX_WEB_PORTS", "").strip() or "80,443"
    try:
        ports = tuple(sorted({int(part) for part in raw_ports.split(",") if part.strip()}))
    except ValueError:
        ports = ()
    if not ports or any(not 1 <= p <= 65535 for p in ports):
        raise ConfigError("VMSANDBOX_WEB_PORTS must be a comma-separated list of port numbers, like 80,443.")
    return {"web_vnet": vnet, "web_proxy_host": str(address), "web_proxy_port": port, "web_ports": ports}


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    base_vmx = resolve(_required(env, "VMSANDBOX_BASE_VMX"))
    if base_vmx.suffix.lower() != ".vmx" or not base_vmx.is_file():
        raise ConfigError(f"VMSANDBOX_BASE_VMX is not an existing .vmx file: {base_vmx}")

    clones_dir = resolve(_required(env, "VMSANDBOX_CLONES_DIR"))
    transfer_dir = resolve(_required(env, "VMSANDBOX_TRANSFER_DIR"))

    if overlaps(clones_dir, base_vmx.parent):
        raise ConfigError("VMSANDBOX_CLONES_DIR must not overlap the base VM folder.")
    if overlaps(transfer_dir, base_vmx.parent) or overlaps(transfer_dir, clones_dir):
        raise ConfigError(
            "VMSANDBOX_TRANSFER_DIR must not overlap the base VM folder or the clones folder."
        )

    audit_raw = env.get("VMSANDBOX_AUDIT_LOG", "").strip()
    audit_log = resolve(audit_raw) if audit_raw else clones_dir / "audit.jsonl"
    if audit_log.is_dir():
        raise ConfigError(f"VMSANDBOX_AUDIT_LOG must be a file path, not a folder: {audit_log}")
    if same_path(audit_log, transfer_dir) or is_within(audit_log, transfer_dir):
        raise ConfigError(
            "VMSANDBOX_AUDIT_LOG must not be inside the transfer folder (the agent could read or change it)."
        )
    if is_within(audit_log, base_vmx.parent):
        raise ConfigError("VMSANDBOX_AUDIT_LOG must not be inside the base VM folder.")
    if is_within(audit_log, clones_dir) and not same_path(audit_log.parent, clones_dir):
        raise ConfigError("VMSANDBOX_AUDIT_LOG must not be inside a sandbox folder.")
    if is_within(audit_log, clones_dir) and audit_log.name.lower() in ("state.json", "state.lock"):
        raise ConfigError("VMSANDBOX_AUDIT_LOG must not be the server's own state file.")

    clones_dir.mkdir(parents=True, exist_ok=True)
    transfer_dir.mkdir(parents=True, exist_ok=True)
    audit_log.parent.mkdir(parents=True, exist_ok=True)

    return Config(
        vmrun_path=_find_vmrun(env),
        base_vmx=base_vmx,
        snapshot=_required(env, "VMSANDBOX_SNAPSHOT"),
        clones_dir=clones_dir,
        transfer_dir=transfer_dir,
        guest_user=_required(env, "VMSANDBOX_GUEST_USER"),
        guest_password=_required(env, "VMSANDBOX_GUEST_PASSWORD"),
        max_sandboxes=_positive_int(env, "VMSANDBOX_MAX_SANDBOXES", 3),
        max_age_minutes=_positive_int(env, "VMSANDBOX_MAX_AGE_MINUTES", 120),
        boot_timeout_seconds=_positive_int(env, "VMSANDBOX_BOOT_TIMEOUT_SECONDS", 180),
        max_memory_mb=_optional_int(env, "VMSANDBOX_MAX_MEMORY_MB", 512, 1_048_576),
        max_cpus=_optional_int(env, "VMSANDBOX_MAX_CPUS", 1, 32),
        audit_log=audit_log,
        remote_input=_switch(env, "VMSANDBOX_REMOTE_INPUT"),
        **_web_settings(env),  # type: ignore[arg-type]
    )
