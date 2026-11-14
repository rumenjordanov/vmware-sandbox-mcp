"""MCP server (stdio). Tool descriptions are written for an agent that knows nothing about VMware."""

from __future__ import annotations

import dataclasses
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .audit import AuditLog
from .config import ConfigError, load_config
from .manager import SandboxError, SandboxManager
from .proxy import WebProxy
from .state import StateStore
from .vmrun import VmrunClient

T = TypeVar("T")

mcp = MCPServer(
    "vmware-sandbox",
    version=__version__,
    instructions=(
        "Disposable Linux computers (sandboxes). Create one, use it, then destroy it. "
        "Nothing done inside a sandbox affects the user's real computer."
    ),
)

_manager: SandboxManager | None = None


def build_manager() -> SandboxManager:
    config = load_config()
    vmrun = VmrunClient(
        config.vmrun_path,
        config.base_vmx,
        config.clones_dir,
        config.guest_user,
        config.guest_password,
    )
    return SandboxManager(config, vmrun, StateStore(config.clones_dir), audit=AuditLog(config.audit_log))


async def _call(fn: Callable[..., T], *args: Any) -> T:
    """Run blocking sandbox work off the event loop; turn known failures into tool errors."""
    if _manager is None:
        raise ToolError("The sandbox server is not configured.")
    try:
        return await anyio.to_thread.run_sync(fn, *args)
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def sandbox_create(network: Literal["none", "web", "nat"] = "none") -> str:
    """Create a fresh, disposable Linux computer (a "sandbox") and wait until it is ready to use.

    Use this before running untrusted code, installing packages, or trying anything you
    would not want to do on the user's real computer. Every sandbox starts from the same
    clean state and is completely separate from the user's machine and from other sandboxes.

    network: "none" (default) gives the sandbox no network at all. Choose another mode only
    when the task needs to download something:
    - "web": internet for web downloads only (HTTP and HTTPS), and nothing else: the user's
      own computer and home network stay unreachable. Prefer this. Standard tools (curl,
      wget, pip, git over https) work as usual; with sudo use `sudo -E`, for example
      `sudo -E apt-get -y install <package>`. ping and non-web protocols do not work.
      It may not be set up on this computer; the error will say so.
    - "nat": full internet access. It can also reach the user's own computer and home
      network, so never use it for code you do not trust.

    Takes up to a few minutes. Returns the sandbox_id to pass to the other sandbox tools.
    Destroy the sandbox with sandbox_destroy when you are finished.
    """
    return await _call(lambda: _manager.create(network))  # type: ignore[union-attr]


@mcp.tool()
async def sandbox_run(sandbox_id: str, command: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """Run a shell command inside a sandbox and return what it printed.

    Use this to execute code, install packages, inspect files, or do anything else on the
    sandbox computer. The command is run by bash, so pipes, redirects, && and multi-line
    scripts all work. Each call starts in the home directory with a fresh shell: files
    persist between calls, but `cd` and environment variables do not. The command gets no
    keyboard input, so use non-interactive flags (for example `apt-get -y`).

    timeout_seconds: how long the command may run (1 to 3600, default 60). When the limit
    is reached the command is killed and timed_out is true.

    Returns stdout, stderr, exit_code (0 means success) and timed_out. Very long output is
    cut at 1 MB; stdout_truncated / stderr_truncated say when that happened.
    """
    result = await _call(lambda: _manager.run(sandbox_id, command, timeout_seconds))  # type: ignore[union-attr]
    return dataclasses.asdict(result)


@mcp.tool()
async def sandbox_start(sandbox_id: str, command: str) -> str:
    """Start a command in a sandbox that keeps running in the background, and return at once.

    Use this for things that do not finish on their own or take very long: a web server,
    a database, a watcher, a long build or download. sandbox_run would wait for them and
    time out. After starting, use sandbox_run for other commands in the same sandbox (for
    example to talk to the server you started), sandbox_check to see its output and
    whether it is still running, and sandbox_stop to end it.

    The command is run by bash, starting in the home directory, with no keyboard input.
    A sandbox can have at most 8 background commands running at once. Background commands
    end when the sandbox is destroyed.

    Returns a job_id to pass to sandbox_check and sandbox_stop.
    """
    return await _call(lambda: _manager.start(sandbox_id, command))  # type: ignore[union-attr]


@mcp.tool()
async def sandbox_check(sandbox_id: str, job_id: str) -> dict[str, Any]:
    """Look at a background command started with sandbox_start: is it still running, and what has it printed?

    Returns status ("running", "exited" when it ended by itself, or "stopped" when it was
    killed), exit_code (only when status is "exited"; 0 means success), and stdout and
    stderr collected so far. If the output is longer than about 1 MB only the newest part
    is returned, and stdout_truncated / stderr_truncated say so. Calling this does not
    affect the command; call it again later to see newer output.
    """
    status = await _call(lambda: _manager.check(sandbox_id, job_id))  # type: ignore[union-attr]
    return dataclasses.asdict(status)


@mcp.tool()
async def sandbox_stop(sandbox_id: str, job_id: str) -> str:
    """Stop a background command started with sandbox_start, together with any programs it started.

    Use this when you no longer need the command, or to free one of the 8 background
    slots. Its output stays available through sandbox_check afterwards.
    Returns a short confirmation, which also says if the command had already ended.
    """
    outcome = await _call(lambda: _manager.stop(sandbox_id, job_id))  # type: ignore[union-attr]
    if outcome == "finished":
        return f"Background command {job_id} had already ended."
    return f"Background command {job_id} stopped."


@mcp.tool()
async def sandbox_copy_in(sandbox_id: str, host_path: str, guest_path: str) -> str:
    """Copy a file or a whole folder from the user's computer into a sandbox.

    Use this to give the sandbox something to work on (a script, a dataset, a project
    folder). Only things inside the user's designated transfer folder can be copied;
    nothing else on the user's computer is reachable.

    host_path: the file or folder to copy, relative to the transfer folder (for example
    "data/input.csv" or "myproject"). Paths that leave the transfer folder are rejected.
    guest_path: full destination path inside the sandbox. For a file, the path of the
    file itself (for example "/home/debian/input.csv"); its folder must already exist.
    For a folder, the path the folder should have (for example "/home/debian/myproject");
    it is created if missing, and the folder's contents are placed inside it.

    Folders are copied with everything in them. Files arrive without the executable
    flag (use chmod +x if needed). A folder that contains links is refused.
    Returns a short confirmation with the number of files and the size.
    """
    done = await _call(lambda: _manager.copy_in(sandbox_id, host_path, guest_path))  # type: ignore[union-attr]
    if done.kind == "folder":
        return f"Copied folder ({done.files} files, {done.bytes} bytes) into the sandbox at {guest_path}."
    return f"Copied {done.bytes} bytes into the sandbox at {guest_path}."


@mcp.tool()
async def sandbox_copy_out(sandbox_id: str, guest_path: str, host_path: str) -> str:
    """Copy a file or a whole folder from a sandbox to the user's computer.

    Use this to save results (a report, a built program, a log, an output folder) before
    destroying the sandbox, since everything inside a sandbox is lost when it is destroyed.
    Things can only be written inside the user's designated transfer folder.

    guest_path: full path of the file or folder inside the sandbox (for example
    "/home/debian/out.txt" or "/home/debian/build").
    host_path: where to save it, relative to the transfer folder (for example
    "results/out.txt" or "results/build"). Missing folders are created; existing files
    with the same names are replaced. Paths that leave the transfer folder are rejected.

    When copying a folder, its contents are placed inside host_path. Links and files
    whose names are not valid on the user's computer are left out; the reply says how many.
    Returns the full path where it was saved, the number of files and the size.
    """
    done = await _call(lambda: _manager.copy_out(sandbox_id, guest_path, host_path))  # type: ignore[union-attr]
    if done.kind == "folder":
        note = f" {done.skipped} entries were left out (links or unsupported names)." if done.skipped else ""
        return f"Saved folder ({done.files} files, {done.bytes} bytes) to {done.path}.{note}"
    return f"Saved {done.bytes} bytes to {done.path}."


@mcp.tool()
async def sandbox_screenshot(sandbox_id: str) -> Image:
    """Take a picture of what is currently on a sandbox's screen.

    Use this to see things that have no text output: a graphical program, a boot or error
    screen, or to check what the sandbox is displaying when a command seems stuck. For the
    output of commands, sandbox_run is faster and more precise.

    Returns a PNG image of the sandbox's display at this moment.
    """
    data = await _call(lambda: _manager.screenshot(sandbox_id))  # type: ignore[union-attr]
    return Image(data=data, format="png")


@mcp.tool()
async def sandbox_destroy(sandbox_id: str) -> str:
    """Shut down a sandbox and permanently delete it and everything inside it.

    Use this as soon as you no longer need a sandbox; only a few can exist at once.
    Copy out any files you want to keep first, because this cannot be undone.
    Returns a short confirmation.
    """
    await _call(lambda: _manager.destroy(sandbox_id))  # type: ignore[union-attr]
    return f"Sandbox {sandbox_id} destroyed."


@mcp.tool()
async def sandbox_list() -> list[dict[str, Any]]:
    """List the sandboxes that currently exist.

    Use this to find a sandbox_id you lost track of, or to check whether old sandboxes
    should be destroyed. Returns one entry per sandbox with its sandbox_id, network mode
    ("none" or "nat"), status, and age in seconds. Returns an empty list if there are none.
    """
    return await _call(lambda: _manager.list())  # type: ignore[union-attr]


def _reaper_loop(manager: SandboxManager, interval_seconds: float = 60) -> None:
    """Background thread: destroys sandboxes that outlive the maximum age."""
    while True:
        time.sleep(interval_seconds)
        try:
            manager.reap_expired()
        except Exception:
            logging.getLogger("vmware_sandbox_mcp").exception("reaper pass failed")


def main() -> None:
    global _manager
    args = sys.argv[1:]
    if args[:1] == ["doctor"]:
        from .doctor import main as doctor_main

        raise SystemExit(doctor_main(args[1:]))
    if args:
        print("usage: vmware-sandbox-mcp [doctor [--no-boot]]", file=sys.stderr)
        raise SystemExit(2)
    try:
        _manager = build_manager()
    except ConfigError as exc:
        print(f"vmware-sandbox-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s: %(message)s")
    _manager.cleanup_orphans()
    _manager.reap_expired()
    threading.Thread(target=_reaper_loop, args=(_manager,), daemon=True, name="sandbox-reaper").start()
    config = _manager.config
    if config.web_vnet and config.web_proxy_host:
        WebProxy(
            config.web_proxy_host, config.web_proxy_port, config.web_ports, AuditLog(config.audit_log)
        ).start_with_retry()
    mcp.run()


if __name__ == "__main__":
    main()
