"""The MCP server process itself: it must start and describe its tools even when it is not configured."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

EXPECTED_TOOLS = sorted([
    "sandbox_create", "sandbox_run", "sandbox_start", "sandbox_check", "sandbox_stop",
    "sandbox_copy_in", "sandbox_copy_out", "sandbox_screenshot", "sandbox_click", "sandbox_type",
    "sandbox_key", "sandbox_destroy", "sandbox_list",
])


class Client:
    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "vmware_sandbox_mcp.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env,
        )
        self.next_id = 0
        self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"},
        })
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed the connection: " + self.proc.stderr.read()[-500:])
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                return reply["result"]

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=20)
        finally:
            self.proc.stdout.close()
            self.proc.stderr.close()


@pytest.fixture
def unconfigured():
    env = {k: v for k, v in os.environ.items() if not k.startswith("VMSANDBOX_")}
    client = Client(env)
    yield client
    client.close()


def test_unconfigured_server_starts_and_lists_all_tools(unconfigured):
    tools = unconfigured.request("tools/list")["tools"]
    assert sorted(tool["name"] for tool in tools) == EXPECTED_TOOLS
    assert all(tool.get("description") for tool in tools)


def test_unconfigured_server_refuses_every_tool_with_the_reason(unconfigured):
    calls = {
        "sandbox_create": {},
        "sandbox_list": {},
        "sandbox_run": {"sandbox_id": "sbx-00000000", "command": "id"},
        "sandbox_destroy": {"sandbox_id": "sbx-00000000"},
        "sandbox_copy_in": {"sandbox_id": "sbx-00000000", "host_path": "a", "guest_path": "/tmp/a"},
        "sandbox_click": {"sandbox_id": "sbx-00000000", "x": 1, "y": 1},
    }
    for name, arguments in calls.items():
        result = unconfigured.request("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result["content"])
        assert result.get("isError") is True, name
        assert "not set up" in text and "VMSANDBOX_BASE_VMX" in text and "doctor" in text, (name, text)
