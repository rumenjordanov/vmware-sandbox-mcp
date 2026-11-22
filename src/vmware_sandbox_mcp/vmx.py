"""Rewrite a clone's .vmx before its first boot: isolation, devices, network."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .paths import resolve, same_path

NETWORK_MODES = ("none", "nat", "web")

# Whole device families removed from every clone.
_DROP_PREFIXES = (
    "ethernet", "sharedfolder", "usb", "ehci", "sound", "serial", "parallel", "floppy", "remotedisplay",
)

_ISOLATION = {
    # shared folders off
    "isolation.tools.hgfs.disable": "TRUE",
    "sharedFolder.maxNum": "0",
    # clipboard and drag-and-drop off
    "isolation.tools.copy.disable": "TRUE",
    "isolation.tools.paste.disable": "TRUE",
    "isolation.tools.dnd.disable": "TRUE",
    "isolation.tools.setGUIOptions.enable": "FALSE",
    # removed devices stay removed
    "usb.present": "FALSE",
    "ehci.present": "FALSE",
    "sound.present": "FALSE",
    "floppy0.present": "FALSE",
    # a headless VM must never block on a dialog
    "msg.autoAnswer": "TRUE",
}

_NAT = {
    "ethernet0.present": "TRUE",
    "ethernet0.connectionType": "nat",
    "ethernet0.virtualDev": "e1000",
    "ethernet0.addressType": "generated",
}
_NO_NETWORK = {"ethernet0.present": "FALSE"}
_VNET = re.compile(r"^VMnet([2-7]|9|1[0-9])\Z")  # never bridged (0), shared host-only (1) or NAT (8)


def _web(vnet: str | None) -> dict[str, str]:
    if vnet is None or not _VNET.match(vnet):
        raise ValueError(f"network 'web' needs a dedicated host-only VMware network, got {vnet!r}")
    return {
        "ethernet0.present": "TRUE",
        "ethernet0.connectionType": "custom",
        "ethernet0.vnet": vnet,
        "ethernet0.virtualDev": "e1000",
        "ethernet0.addressType": "generated",
    }

_LINE = re.compile(r'^\s*([^=\s#]+)\s*=\s*"(.*)"\s*$')


_DISPLAY_PASSWORD = re.compile(r"^[A-Za-z0-9]{8}\Z")

MIN_MEMORY_MB, MAX_MEMORY_MB, MAX_CPUS = 512, 1_048_576, 32


def check_resources(memory_mb: object, cpus: object) -> None:
    """Raise ValueError unless the requested size is a plain number VMware can use."""
    if memory_mb is not None and (
        isinstance(memory_mb, bool)
        or not isinstance(memory_mb, int)
        or not MIN_MEMORY_MB <= memory_mb <= MAX_MEMORY_MB
        or memory_mb % 4
    ):
        raise ValueError(
            f"memory_mb must be a whole number of megabytes, a multiple of 4, from {MIN_MEMORY_MB} "
            f"to {MAX_MEMORY_MB}; got {memory_mb!r}."
        )
    if cpus is not None and (isinstance(cpus, bool) or not isinstance(cpus, int) or not 1 <= cpus <= MAX_CPUS):
        raise ValueError(f"cpus must be a whole number from 1 to {MAX_CPUS}; got {cpus!r}.")


def vmx_resources(text: str) -> tuple[int | None, int | None]:
    """(memory in MB, number of CPUs) as a .vmx file states them; None where it does not."""
    values = {key.lower(): value for key, value in (m.groups() for m in map(_LINE.match, text.splitlines()) if m)}

    def number(key: str) -> int | None:
        try:
            return int(values[key])
        except (KeyError, ValueError):
            return None

    return number("memsize"), number("numvcpus")


def _remote_display(display: tuple[int, str] | None) -> dict[str, str]:
    """Remote display (keyboard and mouse) settings. Off unless a port and password are given.

    Any remote display configured in the base VM is dropped first, so a clone never
    inherits an open or password-less display.
    """
    if display is None:
        return {"RemoteDisplay.vnc.enabled": "FALSE"}
    port, password = display
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError(f"bad remote display port {port!r}")
    if not isinstance(password, str) or not _DISPLAY_PASSWORD.match(password):
        raise ValueError("the remote display password must be 8 letters or digits")
    return {
        "RemoteDisplay.vnc.enabled": "TRUE",
        "RemoteDisplay.vnc.ip": "127.0.0.1",  # this computer only, never the network
        "RemoteDisplay.vnc.port": str(port),
        "RemoteDisplay.vnc.password": password,
    }


def harden_vmx_text(
    text: str,
    network: str,
    vnet: str | None = None,
    display: tuple[int, str] | None = None,
    memory_mb: int | None = None,
    cpus: int | None = None,
) -> str:
    if network not in NETWORK_MODES:
        raise ValueError(f"network must be one of {NETWORK_MODES}, got {network!r}")
    check_resources(memory_mb, cpus)

    parsed: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            parsed.append((match.group(1), match.group(2)))

    # CD/DVD drives (they point at ISO files or drives on the host), e.g. "sata0:1".
    cdroms = {
        key.rsplit(".", 1)[0].lower()
        for key, value in parsed
        if key.lower().endswith(".devicetype") and value.lower().startswith("cdrom")
    }

    adapter = _NAT if network == "nat" else _web(vnet) if network == "web" else _NO_NETWORK
    settings = {**_ISOLATION, **adapter, **_remote_display(display)}
    if memory_mb is not None:
        settings["memsize"] = str(memory_mb)
    if cpus is not None:
        settings["numvcpus"] = str(cpus)
    overridden = {key.lower() for key in settings}

    kept: list[str] = []
    for key, value in parsed:
        low = key.lower()
        if low in overridden or low.startswith(_DROP_PREFIXES):
            continue
        if cpus is not None and low == "cpuid.corespersocket":
            continue  # a core count that no longer divides the new CPU count would stop the VM starting
        if low.rsplit(".", 1)[0] in cdroms:
            continue
        kept.append(f'{key} = "{value}"')
    kept += [f'{key} = "{value}"' for key, value in settings.items()]
    return "\n".join(kept) + "\n"


def harden_vmx(
    vmx: str | os.PathLike[str],
    clones_dir: str | os.PathLike[str],
    network: str,
    vnet: str | None = None,
    display: tuple[int, str] | None = None,
    memory_mb: int | None = None,
    cpus: int | None = None,
) -> None:
    path = resolve(vmx)
    if not same_path(path.parent.parent, clones_dir) or path.suffix.lower() != ".vmx":
        raise PermissionError(f"Refused to edit {vmx}: not a sandbox .vmx inside {clones_dir}.")
    text = Path(path).read_text(encoding="utf-8")
    Path(path).write_text(harden_vmx_text(text, network, vnet, display, memory_mb, cpus), encoding="utf-8")


def read_vmx_resources(vmx: str | os.PathLike[str], clones_dir: str | os.PathLike[str]) -> tuple[int | None, int | None]:
    """The size a sandbox's .vmx states (it carries the base VM's size right after cloning)."""
    path = resolve(vmx)
    if not same_path(path.parent.parent, clones_dir) or path.suffix.lower() != ".vmx":
        raise PermissionError(f"Refused to read {vmx}: not a sandbox .vmx inside {clones_dir}.")
    return vmx_resources(Path(path).read_text(encoding="utf-8"))
