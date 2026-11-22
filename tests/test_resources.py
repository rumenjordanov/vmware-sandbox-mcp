"""Choosing a sandbox's memory and CPUs when it is created."""

from __future__ import annotations

import dataclasses

import pytest

import conftest
from vmware_sandbox_mcp.config import ConfigError, load_config
from vmware_sandbox_mcp.manager import SandboxError
from vmware_sandbox_mcp.vmx import harden_vmx_text, vmx_resources

NO_SIZE = 'displayName = "x"\nscsi0:0.fileName = "disk.vmdk"\nscsi0:0.present = "TRUE"\n'


def vmx_of(env, sandbox_id):
    text = (env.clones / sandbox_id / f"{sandbox_id}.vmx").read_text(encoding="utf-8")
    return dict(line.split(" = ", 1) for line in text.splitlines())


def listed(env):
    entry = env.manager.list()[0]
    return entry["memory_mb"], entry["cpus"]


def nothing_left(env):
    return env.manager.list() == [] and not env.fake.running and not any(p.is_dir() for p in env.clones.iterdir())


# ---- what the sandbox gets ---------------------------------------------------

def test_default_size_is_the_base_vms_and_is_listed(env):
    sid = env.manager.create()
    vmx = vmx_of(env, sid)
    assert (vmx["memsize"], vmx["numvcpus"], vmx["cpuid.coresPerSocket"]) == ('"2048"', '"2"', '"2"')
    assert listed(env) == (2048, 2)
    assert env.audit()[-1]["memory_mb"] is None and env.audit()[-1]["cpus"] is None


def test_a_smaller_size_is_applied_listed_and_audited(env):
    sid = env.manager.create(memory_mb=1024, cpus=1)
    vmx = vmx_of(env, sid)
    assert (vmx["memsize"], vmx["numvcpus"]) == ('"1024"', '"1"')
    assert "cpuid.coresPerSocket" not in vmx          # it could no longer divide the CPU count
    assert listed(env) == (1024, 1)
    entry = env.audit()[-1]
    assert (entry["event"], entry["memory_mb"], entry["cpus"], entry["ok"]) == ("create", 1024, 1, True)


def test_each_value_can_be_given_alone(env):
    only_memory = env.manager.create(memory_mb=512)
    vmx = vmx_of(env, only_memory)
    assert (vmx["memsize"], vmx["numvcpus"], vmx["cpuid.coresPerSocket"]) == ('"512"', '"2"', '"2"')
    only_cpus = env.manager.create(cpus=1)
    vmx = vmx_of(env, only_cpus)
    assert (vmx["memsize"], vmx["numvcpus"]) == ('"2048"', '"1"')


@pytest.mark.parametrize("network", ["none", "nat"])
def test_the_size_does_not_disturb_isolation_or_network(env, network):
    vmx = vmx_of(env, env.manager.create(network, memory_mb=1024))
    assert vmx["memsize"] == '"1024"'
    assert vmx["isolation.tools.hgfs.disable"] == '"TRUE"'
    assert vmx["ethernet0.present"] == ('"TRUE"' if network == "nat" else '"FALSE"')


def test_the_smallest_and_the_exact_maximum_are_accepted(env):
    env.manager.create(memory_mb=512, cpus=1)
    env.manager.create(memory_mb=2048, cpus=2)
    assert sorted((e["memory_mb"], e["cpus"]) for e in env.manager.list()) == [(512, 1), (2048, 2)]


# ---- limits ------------------------------------------------------------------

@pytest.mark.parametrize("request_,limit", [
    ({"memory_mb": 4096}, "at most 2048"),
    ({"memory_mb": 2052}, "at most 2048"),
    ({"cpus": 4}, "at most 2"),
    ({"memory_mb": 1024, "cpus": 3}, "at most 2"),
])
def test_asking_for_more_than_the_base_vm_is_refused_and_leaves_nothing_behind(env, request_, limit):
    with pytest.raises(SandboxError, match=limit):
        env.manager.create(**request_)
    assert nothing_left(env)


def test_the_users_limits_replace_the_default_limit(env):
    env.manager._config = dataclasses.replace(env.config, max_memory_mb=4096, max_cpus=4)
    env.manager.create(memory_mb=4096, cpus=4)
    assert listed(env) == (4096, 4)
    for request_, limit in (({"memory_mb": 4100}, "at most 4096"), ({"cpus": 5}, "at most 4")):
        with pytest.raises(SandboxError, match=limit):
            env.manager.create(**request_)
    assert len(env.manager.list()) == 1


def test_a_user_limit_below_the_base_size_still_lets_the_default_through(env):
    env.manager._config = dataclasses.replace(env.config, max_memory_mb=1024)
    env.manager.create()                                  # the default is the user's own base VM
    assert listed(env) == (2048, 2)
    with pytest.raises(SandboxError, match="at most 1024"):
        env.manager.create(memory_mb=1500)


@pytest.mark.parametrize("memory", [0, 511, 513, -4, 1_048_580, True, 1.5, "1024", [1024]])
def test_bad_memory_values_are_refused_before_anything_is_cloned(env, memory):
    with pytest.raises(SandboxError, match="memory_mb must"):
        env.manager.create(memory_mb=memory)
    assert env.fake.calls == []


@pytest.mark.parametrize("cpus", [0, -1, 33, True, 1.5, "2", [1]])
def test_bad_cpu_values_are_refused_before_anything_is_cloned(env, cpus):
    with pytest.raises(SandboxError, match="cpus must"):
        env.manager.create(cpus=cpus)
    assert env.fake.calls == []


def test_when_the_base_size_cannot_be_read_only_a_configured_limit_allows_a_change(env, monkeypatch):
    monkeypatch.setattr(conftest, "BASE_VMX_TEXT", NO_SIZE)
    env.manager.create()
    assert listed(env) == (None, None)
    with pytest.raises(SandboxError, match="could not be read"):
        env.manager.create(memory_mb=1024)
    env.manager._config = dataclasses.replace(env.config, max_memory_mb=2048)
    sid = env.manager.create(memory_mb=1024)
    assert vmx_of(env, sid)["memsize"] == '"1024"'


# ---- settings ----------------------------------------------------------------

@pytest.fixture
def settings(env, tmp_path):
    vmrun_exe = tmp_path / "vmrun.exe"
    vmrun_exe.write_text("", encoding="utf-8")
    return {
        "VMSANDBOX_BASE_VMX": str(env.base_vmx), "VMSANDBOX_SNAPSHOT": "Snapshot 1",
        "VMSANDBOX_CLONES_DIR": str(env.clones), "VMSANDBOX_TRANSFER_DIR": str(env.config.transfer_dir),
        "VMSANDBOX_GUEST_USER": "user", "VMSANDBOX_GUEST_PASSWORD": "s3cret-pw",
        "VMSANDBOX_VMRUN_PATH": str(vmrun_exe),
    }


def test_limits_are_unset_by_default_and_read_when_given(settings):
    config = load_config(settings)
    assert (config.max_memory_mb, config.max_cpus) == (None, None)
    settings.update(VMSANDBOX_MAX_MEMORY_MB="8192", VMSANDBOX_MAX_CPUS="8")
    config = load_config(settings)
    assert (config.max_memory_mb, config.max_cpus) == (8192, 8)


@pytest.mark.parametrize("name,value", [
    ("VMSANDBOX_MAX_MEMORY_MB", "abc"), ("VMSANDBOX_MAX_MEMORY_MB", "0"), ("VMSANDBOX_MAX_MEMORY_MB", "511"),
    ("VMSANDBOX_MAX_MEMORY_MB", "1048577"), ("VMSANDBOX_MAX_MEMORY_MB", "-1"), ("VMSANDBOX_MAX_MEMORY_MB", "2.5"),
    ("VMSANDBOX_MAX_CPUS", "0"), ("VMSANDBOX_MAX_CPUS", "33"), ("VMSANDBOX_MAX_CPUS", "x"),
])
def test_bad_limits_are_refused(settings, name, value):
    settings[name] = value
    with pytest.raises(ConfigError, match=name):
        load_config(settings)


# ---- the .vmx rewriting itself -----------------------------------------------

def test_vmx_rewrite_sets_each_key_once_and_drops_the_core_count_only_with_cpus():
    text = harden_vmx_text(conftest.BASE_VMX_TEXT, "none", memory_mb=1024, cpus=1)
    lines = [line.lower() for line in text.splitlines()]
    assert sum(line.startswith("memsize") for line in lines) == 1 and 'memsize = "1024"' in lines
    assert sum(line.startswith("numvcpus") for line in lines) == 1 and 'numvcpus = "1"' in lines
    assert not any("corespersocket" in line for line in lines)
    untouched = harden_vmx_text(conftest.BASE_VMX_TEXT, "none", memory_mb=1024)
    assert 'cpuid.coresPerSocket = "2"' in untouched and 'numvcpus = "2"' in untouched


def test_nothing_is_changed_when_nothing_is_asked():
    text = harden_vmx_text(conftest.BASE_VMX_TEXT, "none")
    assert 'memsize = "2048"' in text and 'numvcpus = "2"' in text


@pytest.mark.parametrize("memory", ['1024"\nethernet1.present = "TRUE', "1024\n", 1024.0, None.__class__])
def test_vmx_rewrite_cannot_be_given_text_in_place_of_a_number(memory):
    with pytest.raises(ValueError):
        harden_vmx_text(conftest.BASE_VMX_TEXT, "none", memory_mb=memory)


def test_reading_the_size_of_a_vmx():
    assert vmx_resources(conftest.BASE_VMX_TEXT) == (2048, 2)
    assert vmx_resources("") == (None, None)
    assert vmx_resources('memsize = "lots"\nnumvcpus = "4"\n') == (None, 4)
    assert vmx_resources('MEMSIZE = "4096"\n') == (4096, None)
