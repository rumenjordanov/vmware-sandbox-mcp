import pytest

from vmware_sandbox_mcp.vmx import harden_vmx, harden_vmx_text

from conftest import BASE_VMX_TEXT


def settings(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, _, value = line.partition(" = ")
        out[key] = value.strip('"')
    return out


def test_isolation_settings_applied():
    s = settings(harden_vmx_text(BASE_VMX_TEXT, "none"))
    assert s["isolation.tools.hgfs.disable"] == "TRUE"
    assert s["isolation.tools.copy.disable"] == "TRUE"
    assert s["isolation.tools.paste.disable"] == "TRUE"
    assert s["isolation.tools.dnd.disable"] == "TRUE"
    assert s["sharedFolder.maxNum"] == "0"
    assert not any(k.startswith("sharedFolder0") for k in s)


def test_cdrom_usb_sound_removed_disk_kept():
    s = settings(harden_vmx_text(BASE_VMX_TEXT, "none"))
    assert not any(k.startswith("sata0:1") for k in s)
    assert not any(k.startswith("usb:") for k in s)
    assert s["usb.present"] == "FALSE" and s["ehci.present"] == "FALSE"
    assert s["sound.present"] == "FALSE" and "sound.fileName" not in s
    assert s["scsi0:0.fileName"] == "disk.vmdk"


def test_network_none_has_no_adapter():
    s = settings(harden_vmx_text(BASE_VMX_TEXT, "none"))
    assert s["ethernet0.present"] == "FALSE"
    assert "ethernet0.connectionType" not in s


def test_network_nat():
    s = settings(harden_vmx_text(BASE_VMX_TEXT, "nat"))
    assert s["ethernet0.present"] == "TRUE"
    assert s["ethernet0.connectionType"] == "nat"
    assert "ethernet0.generatedAddress" not in s  # no MAC shared with the base VM


def test_unknown_network_rejected():
    with pytest.raises(ValueError):
        harden_vmx_text(BASE_VMX_TEXT, "bridged")


def test_refuses_to_edit_base_or_other_vmx(env):
    for vmx in (env.base_vmx, env.other_vmx):
        with pytest.raises(PermissionError):
            harden_vmx(vmx, env.clones, "none")
        assert vmx.read_text(encoding="utf-8") == BASE_VMX_TEXT
