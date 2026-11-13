"""A fake vmrun *process*. The real VmrunClient (and its path guard) stays in the loop."""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

import pytest

from vmware_sandbox_mcp.config import Config
from vmware_sandbox_mcp.manager import SandboxManager
from vmware_sandbox_mcp.state import StateStore
from vmware_sandbox_mcp import guest_run
from vmware_sandbox_mcp.vmrun import VmrunClient, VmrunTimeout


def make_result_tar(stdout: bytes, stderr: bytes, exit_code: int, limit: int = guest_run.OUTPUT_LIMIT_BYTES) -> bytes:
    """What the in-guest wrapper would produce."""
    members = {
        "stdout": stdout[:limit],
        "stderr": stderr[:limit],
        "stdout.size": f"{len(stdout)}\n".encode(),
        "stderr.size": f"{len(stderr)}\n".encode(),
        "exit": f"{exit_code}\n".encode(),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()

BASE_VMX_TEXT = """.encoding = "UTF-8"
displayName = "Debian 13.x 64-bit"
memsize = "2048"
scsi0:0.fileName = "disk.vmdk"
scsi0:0.present = "TRUE"
sata0:1.deviceType = "cdrom-image"
sata0:1.fileName = "C:\\ISOs\\debian.iso"
sata0:1.present = "TRUE"
usb.present = "TRUE"
ehci.present = "TRUE"
usb:0.present = "TRUE"
sound.present = "TRUE"
sound.fileName = "-1"
ethernet0.connectionType = "nat"
ethernet0.present = "TRUE"
ethernet0.generatedAddress = "00:0c:29:aa:bb:cc"
sharedFolder0.present = "TRUE"
sharedFolder0.hostPath = "C:\\Users"
isolation.tools.hgfs.disable = "FALSE"
"""


class FakeVmrunProcess:
    """Stands in for vmrun.exe: records argv and imitates its effects on disk."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.running: set[str] = set()
        self.tools_ready = True
        self.fail: set[str] = set()
        self.guest_files: dict[str, bytes] = {}
        self.ran: list[tuple[str, int]] = []
        self.next_result: tuple[bytes, bytes, int] = (b"", b"", 0)
        self.hang = False
        self.screen = b"\x89PNG\r\n\x1a\nfake-image"

    def commands(self) -> list[str]:
        return [self._split(argv)[0] for argv in self.calls]

    @staticmethod
    def _split(argv: list[str]) -> tuple[str, list[str]]:
        rest = list(argv[3:])  # after: vmrun -T ws
        if rest[:1] == ["-gu"]:
            rest = rest[4:]
        return rest[0], rest[1:]

    def __call__(self, argv, timeout):
        argv = list(argv)
        self.calls.append(argv)
        command, args = self._split(argv)
        if command in self.fail:
            return 255, f"Error: simulated {command} failure"
        if command == "list":
            return 0, f"Total running VMs: {len(self.running)}\n" + "\n".join(sorted(self.running))
        if command == "listSnapshots":
            return 0, "Total snapshots: 1\nSnapshot 1"
        if command == "clone":
            dest = Path(args[1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(BASE_VMX_TEXT, encoding="utf-8")
            return 0, ""
        if command == "start":
            self.running.add(args[0])
            return 0, ""
        if command == "stop":
            if args[0] not in self.running:
                return 255, "Error: The virtual machine is not powered on"
            self.running.discard(args[0])
            return 0, ""
        if command == "deleteVM":
            shutil.rmtree(Path(args[0]).parent, ignore_errors=True)
            return 0, ""
        if command == "checkToolsState":
            return 0, "running" if self.tools_ready else "installed"
        if command == "runProgramInGuest":
            if args[1] == "/bin/true":
                return 0, ""
            if self.hang:
                raise VmrunTimeout("simulated hang")
            _, _, script, out, secs = args
            text = self.guest_files[script].decode("utf-8")
            self.ran.append((text[len(guest_run._WRAPPER):], int(secs)))
            stdout, stderr, exit_code = self.next_result
            self.guest_files[out] = make_result_tar(stdout, stderr, exit_code)
            del self.guest_files[script]  # the wrapper deletes itself
            return 0, ""
        if command == "CopyFileFromHostToGuest":
            self.guest_files[args[2]] = Path(args[1]).read_bytes()
            return 0, ""
        if command == "CopyFileFromGuestToHost":
            if args[1] not in self.guest_files:
                return 255, "Error: A file was not found"
            Path(args[2]).write_bytes(self.guest_files[args[1]])
            return 0, ""
        if command == "captureScreen":
            Path(args[1]).write_bytes(self.screen)
            return 0, ""
        if command == "deleteFileInGuest":
            return (0, "") if self.guest_files.pop(args[1], None) is not None else (255, "Error: not found")
        return 255, f"Error: fake does not implement {command}"


@pytest.fixture
def env(tmp_path: Path):
    """Mirrors the real layout, including a space in the path and a neighbouring VM."""
    base_dir = tmp_path / "Virtual Machines" / "sandbox-base"
    other_dir = tmp_path / "Virtual Machines" / "other vm"
    clones = tmp_path / "vmsandbox-clones"
    transfer = tmp_path / "transfer"
    for folder in (base_dir, other_dir, clones, transfer):
        folder.mkdir(parents=True)
    base_vmx = base_dir / "Debian 13.x 64-bit.vmx"
    base_vmx.write_text(BASE_VMX_TEXT, encoding="utf-8")
    other_vmx = other_dir / "other.vmx"
    other_vmx.write_text(BASE_VMX_TEXT, encoding="utf-8")

    fake = FakeVmrunProcess()
    config = Config(
        vmrun_path=Path("vmrun"),
        base_vmx=base_vmx,
        snapshot="Snapshot 1",
        clones_dir=clones,
        transfer_dir=transfer,
        guest_user="user",
        guest_password="s3cret-pw",
        boot_timeout_seconds=10,
    )
    vmrun = VmrunClient("vmrun", base_vmx, clones, "user", "s3cret-pw", runner=fake)

    class Env:
        pass

    e = Env()
    e.fake, e.config, e.vmrun = fake, config, vmrun
    e.base_vmx, e.other_vmx, e.clones = base_vmx, other_vmx, clones
    e.now = [1000.0]
    e.manager = SandboxManager(
        config,
        vmrun,
        StateStore(clones),
        clock=lambda: e.now[0],
        sleep=lambda s: e.now.__setitem__(0, e.now[0] + s),
    )
    return e
