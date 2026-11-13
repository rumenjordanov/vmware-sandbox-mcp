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
from .config import ConfigError, load_config
from .manager import SandboxError, SandboxManager
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
    return SandboxManager(config, vmrun, StateStore(config.clones_dir))


async def _call(fn: Callable[..., T], *args: Any) -> T:
    """Run blocking sandbox work off the event loop; turn known failures into tool errors."""
    if _manager is None:
        raise ToolError("The sandbox server is not configured.")
    try:
        return await anyio.to_thread.run_sync(fn, *args)
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def sandbox_create(network: Literal["none", "nat"] = "none") -> str:
    """Create a fresh, disposable Linux computer (a "sandbox") and wait until it is ready to use.

    Use this before running untrusted code, installing packages, or trying anything you
    would not want to do on the user's real computer. Every sandbox starts from the same
    clean state and is completely separate from the user's machine and from other sandboxes.

    network: "none" (default) gives the sandbox no network at all. "nat" gives it internet
    access; only choose it when the task needs to download something.

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
async def sandbox_copy_in(sandbox_id: str, host_path: str, guest_path: str) -> str:
    """Copy one file from the user's computer into a sandbox.

    Use this to give the sandbox a file to work on (a script, a dataset, an archive).
    Only files inside the user's designated transfer folder can be copied; nothing else
    on the user's computer is reachable.

    host_path: the file to copy, relative to the transfer folder (for example
    "data/input.csv"). Paths that leave the transfer folder are rejected.
    guest_path: full destination path of the file inside the sandbox (for example
    "/home/debian/input.csv"). The destination folder must already exist.

    Copies a single file, not a folder; pack folders into an archive first.
    Returns a short confirmation with the file size.
    """
    size = await _call(lambda: _manager.copy_in(sandbox_id, host_path, guest_path))  # type: ignore[union-attr]
    return f"Copied {size} bytes into the sandbox at {guest_path}."


@mcp.tool()
async def sandbox_copy_out(sandbox_id: str, guest_path: str, host_path: str) -> str:
    """Copy one file from a sandbox to the user's computer.

    Use this to save a result (a report, a built program, a log) before destroying the
    sandbox, since everything inside a sandbox is lost when it is destroyed. Files can only
    be written inside the user's designated transfer folder.

    guest_path: full path of the file inside the sandbox (for example "/home/debian/out.txt").
    host_path: where to save it, relative to the transfer folder (for example
    "results/out.txt"). Missing folders are created; an existing file with that name is
    replaced. Paths that leave the transfer folder are rejected.

    Copies a single file, not a folder; pack folders into an archive first (for example with tar).
    Returns the full path of the saved file on the user's computer and its size.
    """
    path, size = await _call(lambda: _manager.copy_out(sandbox_id, guest_path, host_path))  # type: ignore[union-attr]
    return f"Saved {size} bytes to {path}."


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
    try:
        _manager = build_manager()
    except ConfigError as exc:
        print(f"vmware-sandbox-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s: %(message)s")
    _manager.cleanup_orphans()
    _manager.reap_expired()
    threading.Thread(target=_reaper_loop, args=(_manager,), daemon=True, name="sandbox-reaper").start()
    mcp.run()


if __name__ == "__main__":
    main()
