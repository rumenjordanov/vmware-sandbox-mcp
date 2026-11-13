"""Configuration from environment variables. Fails closed: no guessing."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .paths import overlaps, resolve

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


def _find_vmrun(env: Mapping[str, str]) -> Path:
    explicit = env.get("VMSANDBOX_VMRUN_PATH", "").strip()
    candidates = [explicit] if explicit else [*_VMRUN_CANDIDATES, shutil.which("vmrun") or ""]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise ConfigError("vmrun not found. Set VMSANDBOX_VMRUN_PATH to the full path of vmrun.exe.")


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

    clones_dir.mkdir(parents=True, exist_ok=True)
    transfer_dir.mkdir(parents=True, exist_ok=True)

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
    )
