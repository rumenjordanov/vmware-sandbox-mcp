"""Choosing which base image (snapshot of the base VM) a sandbox starts from."""

from __future__ import annotations

import dataclasses

import pytest

from vmware_sandbox_mcp.config import ConfigError, load_config
from vmware_sandbox_mcp.manager import SandboxError
from vmware_sandbox_mcp.vmrun import ForbiddenTargetError


@pytest.fixture
def two_images(env):
    """The base VM has three snapshots; the user lists two of them as choices."""
    env.fake.base_snapshots[:] = ["Snapshot 1", "clean", "dev"]
    env.manager._config = dataclasses.replace(
        env.config, images=(("clean", "Debian with sudo and everyday tools"), ("dev", "Also compilers and Node"))
    )
    return env


# ---- the choices -----------------------------------------------------------------

def test_without_a_list_there_is_just_the_default(env):
    assert env.manager.images() == [{"name": "Snapshot 1", "description": "The default image.", "default": True}]


def test_the_default_comes_first_then_the_listed_images(two_images):
    assert two_images.manager.images() == [
        {"name": "Snapshot 1", "description": "The default image.", "default": True},
        {"name": "clean", "description": "Debian with sudo and everyday tools", "default": False},
        {"name": "dev", "description": "Also compilers and Node", "default": False},
    ]


def test_the_default_can_be_described_by_listing_it(env):
    env.manager._config = dataclasses.replace(env.config, images=(("Snapshot 1", "Plain Debian"), ("clean", "")))
    assert env.manager.images() == [
        {"name": "Snapshot 1", "description": "Plain Debian", "default": True},
        {"name": "clean", "description": "No description.", "default": False},
    ]


# ---- creating from an image ---------------------------------------------------------

def test_the_default_is_used_when_no_image_is_given(two_images):
    sid = two_images.manager.create()
    assert two_images.fake.clone_snapshots == ["Snapshot 1"]
    assert two_images.manager.list()[0]["image"] == "Snapshot 1"
    assert two_images.manager.create(image="").__class__ is str          # an empty name means the default
    assert two_images.fake.clone_snapshots == ["Snapshot 1", "Snapshot 1"]
    assert two_images.audit()[0]["image"] is None


@pytest.mark.parametrize("image", ["clean", "dev", "Snapshot 1"])
def test_a_listed_image_is_cloned_and_shown(two_images, image):
    sid = two_images.manager.create(image=image)
    assert two_images.fake.clone_snapshots == [image]
    entry = two_images.manager.list()[0]
    assert (entry["sandbox_id"], entry["image"]) == (sid, image)
    assert two_images.audit()[-1]["image"] == image


@pytest.mark.parametrize("image", ["DEV", "dev ", " dev", "unlisted", "Snapshot 2", "../dev", "dev\nclean", 5, ["dev"], True])
def test_anything_not_listed_is_refused_before_anything_is_cloned(two_images, image):
    two_images.fake.base_snapshots.append("unlisted")           # it exists in VMware, but the user did not offer it
    with pytest.raises(SandboxError, match="There is no image named"):
        two_images.manager.create(image=image)
    assert two_images.fake.calls == [] and two_images.manager.list() == []


def test_the_refusal_names_the_real_choices(two_images):
    with pytest.raises(SandboxError, match=r"'Snapshot 1', 'clean', 'dev'"):
        two_images.manager.create(image="nope")


def test_an_image_that_does_not_exist_in_vmware_fails_cleanly(two_images):
    two_images.fake.base_snapshots.remove("dev")                # listed by the user, but gone from the base VM
    with pytest.raises(SandboxError, match="does not exist"):
        two_images.manager.create(image="dev")
    assert two_images.manager.list() == [] and not two_images.fake.running


def test_the_image_choice_combines_with_the_other_options(two_images):
    sid = two_images.manager.create("nat", memory_mb=1024, cpus=1, image="clean")
    entry = two_images.manager.list()[0]
    assert (entry["image"], entry["memory_mb"], entry["cpus"]) == ("clean", 1024, 1)


def test_the_base_vm_is_still_only_cloned_from(two_images):
    two_images.manager.create(image="dev")
    base = str(two_images.base_vmx)
    for argv in two_images.fake.calls:
        if base in argv:
            assert argv[3] in ("clone", "listSnapshots")


def test_the_vmrun_wrapper_refuses_odd_snapshot_names_itself(env):
    dest = env.clones / "sbx-0a1b2c3d" / "sbx-0a1b2c3d.vmx"
    for bad in ("", "a\nb", "a\x00b", None, 5):
        with pytest.raises(ForbiddenTargetError):
            env.vmrun.clone(dest, bad, "sbx-0a1b2c3d")
    assert env.fake.calls == []


# ---- settings -----------------------------------------------------------------------

@pytest.fixture
def settings(env, tmp_path):
    vmrun_exe = tmp_path / "vmrun.exe"
    vmrun_exe.write_text("", encoding="utf-8")
    return {
        "VMSANDBOX_BASE_VMX": str(env.base_vmx), "VMSANDBOX_SNAPSHOT": "clean",
        "VMSANDBOX_CLONES_DIR": str(env.clones), "VMSANDBOX_TRANSFER_DIR": str(env.config.transfer_dir),
        "VMSANDBOX_GUEST_USER": "user", "VMSANDBOX_GUEST_PASSWORD": "s3cret-pw",
        "VMSANDBOX_VMRUN_PATH": str(vmrun_exe),
    }


def test_no_list_means_no_extra_images(settings):
    assert load_config(settings).images == ()


def test_the_list_is_read_with_and_without_descriptions(settings):
    settings["VMSANDBOX_IMAGES"] = " clean = Debian 13, with sudo ; Snapshot 1 ; dev tools=Has gcc=and more;; "
    assert load_config(settings).images == (
        ("clean", "Debian 13, with sudo"), ("Snapshot 1", ""), ("dev tools", "Has gcc=and more"))


def test_the_first_entry_wins_for_a_repeated_name(settings):
    settings["VMSANDBOX_IMAGES"] = "a=first;a=second;b"
    assert load_config(settings).images == (("a", "first"), ("b", ""))


@pytest.mark.parametrize("value", [
    "=no name", "ok;=x", "a\x01b", "x" * 81, "name=" + "d" * 201, "name=bad\x07text",
    ";".join(f"image{i}" for i in range(21)),
])
def test_bad_lists_are_refused_naming_the_setting(settings, value):
    settings["VMSANDBOX_IMAGES"] = value
    with pytest.raises(ConfigError, match="VMSANDBOX_IMAGES"):
        load_config(settings)
