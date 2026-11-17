"""scripts/prepare-base-vm.sh: the checks that need no Linux. The full run is scripts/test_prepare_base_vm.py."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prepare-base-vm.sh"


def _find_bash():
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash") or "", "/bin/bash"):
        # skip the Windows "bash.exe" that starts WSL: it cannot read a script from here
        if candidate and os.path.exists(candidate) and "system32" not in candidate.lower():
            return candidate
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash on this machine")


def run(*args):
    return subprocess.run(
        [BASH, "-s", "--", *args], input=SCRIPT.read_bytes(), capture_output=True, timeout=60
    )


def test_script_uses_unix_line_endings():
    assert b"\r" not in SCRIPT.read_bytes()
    assert SCRIPT.read_bytes().startswith(b"#!/usr/bin/env bash\n")


def test_gitattributes_keeps_shell_scripts_on_unix_line_endings():
    attributes = (SCRIPT.parent.parent / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes


@needs_bash
def test_script_is_valid_bash():
    done = subprocess.run([BASH, "-n"], input=SCRIPT.read_bytes(), capture_output=True)
    assert done.returncode == 0, done.stderr


@needs_bash
def test_help_works_and_changes_nothing():
    done = run("--help")
    assert done.returncode == 0
    assert b"--check" in done.stdout and b"ACCOUNT" in done.stdout


@needs_bash
@pytest.mark.parametrize("args", [
    [], ["--check"], ["--check", "root"], ["--check", "Bad;Name"], ["--check", "-bad"], ["--check", "a b"],
    ["--check", "UPPER"], ["--check", "1abc"], ["--bogus", "debian"], ["--only"], ["--check", "debian", "second"],
])
def test_bad_arguments_are_refused_with_exit_code_2(args):
    done = run(*args)
    assert done.returncode == 2, (args, done.stderr)
