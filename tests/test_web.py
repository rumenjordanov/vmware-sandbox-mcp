"""network="web": settings, the clone's network adapter, and the proxy variables for commands."""

from __future__ import annotations

import dataclasses
import sys

import pytest

from vmware_sandbox_mcp import config as config_module
from vmware_sandbox_mcp import guest_run
from vmware_sandbox_mcp.config import ConfigError, load_config, vnet_problem
from vmware_sandbox_mcp.manager import SandboxError
from vmware_sandbox_mcp.vmx import harden_vmx_text

from conftest import BASE_VMX_TEXT


@pytest.fixture
def settings(env, tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "vnet_problem", lambda vnet: None)
    vmrun_exe = tmp_path / "vmrun.exe"
    vmrun_exe.write_text("", encoding="utf-8")
    return {
        "VMSANDBOX_BASE_VMX": str(env.base_vmx),
        "VMSANDBOX_SNAPSHOT": "Snapshot 1",
        "VMSANDBOX_CLONES_DIR": str(env.clones),
        "VMSANDBOX_TRANSFER_DIR": str(env.config.transfer_dir),
        "VMSANDBOX_GUEST_USER": "user",
        "VMSANDBOX_GUEST_PASSWORD": "s3cret-pw",
        "VMSANDBOX_VMRUN_PATH": str(vmrun_exe),
        "VMSANDBOX_WEB_VNET": "vmnet9",
        "VMSANDBOX_WEB_PROXY_HOST": "192.168.50.1",
    }


@pytest.fixture
def web(env):
    """The standard test environment with web mode switched on."""
    env.manager._config = dataclasses.replace(
        env.config, web_vnet="VMnet9", web_proxy_host="192.168.50.1", web_proxy_port=3128
    )
    return env


# ---- settings --------------------------------------------------------------

def test_web_mode_is_off_by_default(env, settings):
    del settings["VMSANDBOX_WEB_VNET"], settings["VMSANDBOX_WEB_PROXY_HOST"]
    config = load_config(settings)
    assert config.web_vnet is None and config.web_proxy_host is None


def test_web_settings_are_read_and_normalised(settings):
    settings["VMSANDBOX_WEB_PROXY_PORT"] = "8888"
    settings["VMSANDBOX_WEB_PORTS"] = "443, 80,8443"
    config = load_config(settings)
    assert (config.web_vnet, config.web_proxy_host, config.web_proxy_port, config.web_ports) == (
        "VMnet9", "192.168.50.1", 8888, (80, 443, 8443))


@pytest.mark.parametrize("missing", ["VMSANDBOX_WEB_VNET", "VMSANDBOX_WEB_PROXY_HOST"])
def test_both_web_settings_are_needed(settings, missing):
    del settings[missing]
    with pytest.raises(ConfigError, match="set together"):
        load_config(settings)


@pytest.mark.parametrize("vnet", ["VMnet0", "VMnet1", "VMnet8", "vmnet8", "VMnet20", "nat", "bridged", "VMnet9x", "eth0"])
def test_shared_bridged_and_nat_networks_are_refused(settings, vnet):
    settings["VMSANDBOX_WEB_VNET"] = vnet
    with pytest.raises(ConfigError, match="VMSANDBOX_WEB_VNET"):
        load_config(settings)


def test_network_that_vmware_records_as_nat_is_refused(settings, monkeypatch):
    monkeypatch.setattr(config_module, "vnet_problem", lambda vnet: f"{vnet} is a NAT network")
    with pytest.raises(ConfigError, match="NAT network"):
        load_config(settings)


@pytest.mark.parametrize("host", ["", "example.com", "8.8.8.8", "127.0.0.1", "0.0.0.0", "169.254.1.1", "::1", "192.168.50"])
def test_proxy_host_must_be_a_private_ipv4_address(settings, host):
    settings["VMSANDBOX_WEB_PROXY_HOST"] = host
    with pytest.raises(ConfigError):
        load_config(settings)


@pytest.mark.parametrize("name,value", [
    ("VMSANDBOX_WEB_PROXY_PORT", "0"), ("VMSANDBOX_WEB_PROXY_PORT", "70000"), ("VMSANDBOX_WEB_PROXY_PORT", "abc"),
    ("VMSANDBOX_WEB_PORTS", "80,https"), ("VMSANDBOX_WEB_PORTS", "0"), ("VMSANDBOX_WEB_PORTS", "80,99999"),
    ("VMSANDBOX_WEB_PORTS", ","),
])
def test_bad_port_settings_are_refused(settings, name, value):
    settings[name] = value
    with pytest.raises(ConfigError):
        load_config(settings)


@pytest.mark.skipif(sys.platform != "win32", reason="reads VMware's registry records")
def test_vnet_problem_fails_closed_for_an_unknown_network():
    assert "host-only" in vnet_problem("VMnet-does-not-exist")


# ---- the clone's network adapter -------------------------------------------

def _settings_of(text: str) -> dict[str, str]:
    return dict(line.split(" = ", 1) for line in text.splitlines())


def test_web_clone_is_attached_only_to_the_dedicated_network():
    result = _settings_of(harden_vmx_text(BASE_VMX_TEXT, "web", "VMnet9"))
    assert result["ethernet0.connectionType"] == '"custom"'
    assert result["ethernet0.vnet"] == '"VMnet9"'
    assert result["ethernet0.present"] == '"TRUE"'
    assert [key for key in result if key.lower().startswith("ethernet")] == [
        "ethernet0.present", "ethernet0.connectionType", "ethernet0.vnet",
        "ethernet0.virtualDev", "ethernet0.addressType",
    ]
    assert "nat" not in harden_vmx_text(BASE_VMX_TEXT, "web", "VMnet9").lower()
    assert result["isolation.tools.hgfs.disable"] == '"TRUE"'  # isolation still applied


@pytest.mark.parametrize("vnet", [None, "", "VMnet0", "VMnet1", "VMnet8", "VMnet20", "nat", "VMnet9\n", 'VMnet9"\nethernet1.present = "TRUE'])
def test_web_clone_refuses_any_other_network(vnet):
    with pytest.raises(ValueError):
        harden_vmx_text(BASE_VMX_TEXT, "web", vnet)


def test_other_modes_ignore_the_web_network():
    assert "vnet" not in harden_vmx_text(BASE_VMX_TEXT, "none", "VMnet9")
    assert "VMnet9" not in harden_vmx_text(BASE_VMX_TEXT, "nat", "VMnet9")


# ---- manager ---------------------------------------------------------------

def test_web_is_refused_when_not_set_up_and_nothing_is_created(env):
    with pytest.raises(SandboxError, match="not set up"):
        env.manager.create("web")
    assert "clone" not in env.fake.commands()
    assert env.manager.list() == []


def test_web_sandbox_gets_the_dedicated_network(web):
    sandbox_id = web.manager.create("web")
    vmx = (web.clones / sandbox_id / f"{sandbox_id}.vmx").read_text(encoding="utf-8")
    assert 'ethernet0.vnet = "VMnet9"' in vmx and 'ethernet0.connectionType = "custom"' in vmx
    assert web.manager.list()[0]["network"] == "web"


def test_commands_in_a_web_sandbox_are_pointed_at_the_proxy(web):
    sandbox_id = web.manager.create("web")
    web.manager.run(sandbox_id, "curl https://example.com")
    script, _ = web.fake.ran[-1]
    lines = script.splitlines()
    assert lines[-1] == "curl https://example.com"
    assert "export http_proxy=http://192.168.50.1:3128" in lines
    assert "export https_proxy=http://192.168.50.1:3128" in lines
    assert "export HTTPS_PROXY=http://192.168.50.1:3128" in lines
    assert web.audit()[-1]["command"] == "curl https://example.com"  # the log keeps the agent's own text


@pytest.mark.parametrize("network", ["none", "nat"])
def test_other_sandboxes_get_no_proxy_variables(web, network):
    sandbox_id = web.manager.create(network)
    web.manager.run(sandbox_id, "env")
    assert web.fake.ran[-1][0] == "env\n"


def test_exported_values_are_shell_quoted():
    script = guest_run.build_script("true", {"X": "a b'c;$(id)"}).decode()
    assert "export X='a b'\"'\"'c;$(id)'\n" in script
