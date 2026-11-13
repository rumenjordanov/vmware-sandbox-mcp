"""Rewrite a clone's .vmx before its first boot: isolation, devices, network."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .paths import resolve, same_path

NETWORK_MODES = ("none", "nat")

# Whole device families removed from every clone.
_DROP_PREFIXES = ("ethernet", "sharedfolder", "usb", "ehci", "sound", "serial", "parallel", "floppy")

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

_LINE = re.compile(r'^\s*([^=\s#]+)\s*=\s*"(.*)"\s*$')


def harden_vmx_text(text: str, network: str) -> str:
    if network not in NETWORK_MODES:
        raise ValueError(f"network must be one of {NETWORK_MODES}, got {network!r}")

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

    settings = {**_ISOLATION, **(_NAT if network == "nat" else _NO_NETWORK)}
    overridden = {key.lower() for key in settings}

    kept: list[str] = []
    for key, value in parsed:
        low = key.lower()
        if low in overridden or low.startswith(_DROP_PREFIXES):
            continue
        if low.rsplit(".", 1)[0] in cdroms:
            continue
        kept.append(f'{key} = "{value}"')
    kept += [f'{key} = "{value}"' for key, value in settings.items()]
    return "\n".join(kept) + "\n"


def harden_vmx(vmx: str | os.PathLike[str], clones_dir: str | os.PathLike[str], network: str) -> None:
    path = resolve(vmx)
    if not same_path(path.parent.parent, clones_dir) or path.suffix.lower() != ".vmx":
        raise PermissionError(f"Refused to edit {vmx}: not a sandbox .vmx inside {clones_dir}.")
    text = Path(path).read_text(encoding="utf-8")
    Path(path).write_text(harden_vmx_text(text, network), encoding="utf-8")
