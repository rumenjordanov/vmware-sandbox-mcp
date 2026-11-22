"""End-to-end test against the REAL base VM.

Starts the server exactly as an MCP client would (stdio, JSON-RPC), exercises all
fifteen tools and reads the audit log back. The keyboard and mouse part runs when
VMSANDBOX_REMOTE_INPUT is on, the web part when VMSANDBOX_WEB_VNET is set. Needs the VMSANDBOX_* environment variables (see README) and takes about
a minute. Run:

    python scripts/e2e.py

Exits 0 on success. Any sandbox it creates is destroyed, also on failure.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import sys
from pathlib import Path

REQUIRED = [
    "VMSANDBOX_BASE_VMX", "VMSANDBOX_SNAPSHOT", "VMSANDBOX_CLONES_DIR",
    "VMSANDBOX_TRANSFER_DIR", "VMSANDBOX_GUEST_USER", "VMSANDBOX_GUEST_PASSWORD",
]


class Client:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "vmware_sandbox_mcp.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.next_id = 0
        self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "0"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed the connection")
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method} failed: {reply['error']}")
                return reply["result"]

    def tool(self, tool_name: str, /, **arguments) -> dict:
        return self.request("tools/call", {"name": tool_name, "arguments": arguments})

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def text(result: dict) -> str:
    return "".join(c.get("text", "") for c in result["content"] if c["type"] == "text")


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"  [{'ok' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        raise AssertionError(label)


def main() -> int:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print("Missing environment variables:", ", ".join(missing))
        return 2
    transfer = Path(os.environ["VMSANDBOX_TRANSFER_DIR"])
    transfer.mkdir(parents=True, exist_ok=True)
    (transfer / "e2e-in.txt").write_text("hello from the host\n", encoding="utf-8")

    client = Client()
    sandbox_id = None
    try:
        names = [t["name"] for t in client.request("tools/list")["tools"]]
        check("server exposes exactly the 17 tools", sorted(names) == sorted([
            "sandbox_create", "sandbox_run", "sandbox_copy_in", "sandbox_copy_out",
            "sandbox_screenshot", "sandbox_destroy", "sandbox_list",
            "sandbox_start", "sandbox_check", "sandbox_stop",
            "sandbox_click", "sandbox_type", "sandbox_key",
            "sandbox_checkpoint", "sandbox_restore", "sandbox_scroll", "sandbox_drag",
        ]), names)

        created = client.tool("sandbox_create")
        check("sandbox_create succeeds", not created.get("isError"), text(created))
        sandbox_id = text(created).strip()
        print(f"  sandbox: {sandbox_id}")

        listed = client.tool("sandbox_list")["structuredContent"]["result"]
        check("sandbox_list shows it with network none",
              [(s["sandbox_id"], s["network"]) for s in listed if s["sandbox_id"] == sandbox_id]
              == [(sandbox_id, "none")], listed)

        run = client.tool("sandbox_run", sandbox_id=sandbox_id,
                          command="echo out; echo err >&2; exit 3", timeout_seconds=30)["structuredContent"]
        check("sandbox_run returns stdout, stderr, exit code",
              (run["stdout"], run["stderr"], run["exit_code"]) == ("out\n", "err\n", 3), run)

        slow = client.tool("sandbox_run", sandbox_id=sandbox_id, command="sleep 60", timeout_seconds=2)["structuredContent"]
        check("sandbox_run enforces the timeout", slow["timed_out"], slow)

        offline = client.tool("sandbox_run", sandbox_id=sandbox_id,
                              command="ip -o link | grep -vc LOOPBACK", timeout_seconds=30)["structuredContent"]
        check("network 'none' means no network adapter", offline["stdout"].strip() == "0", offline)

        copied = client.tool("sandbox_copy_in", sandbox_id=sandbox_id, host_path="e2e-in.txt", guest_path="/tmp/e2e-in.txt")
        check("sandbox_copy_in succeeds", not copied.get("isError"), text(copied))
        client.tool("sandbox_run", sandbox_id=sandbox_id,
                    command="tr a-z A-Z < /tmp/e2e-in.txt > /tmp/e2e-out.txt", timeout_seconds=30)
        out = client.tool("sandbox_copy_out", sandbox_id=sandbox_id, guest_path="/tmp/e2e-out.txt", host_path="e2e-out.txt")
        check("sandbox_copy_out succeeds", not out.get("isError"), text(out))
        check("file made the round trip",
              (transfer / "e2e-out.txt").read_text(encoding="utf-8") == "HELLO FROM THE HOST\n")

        escape = client.tool("sandbox_copy_out", sandbox_id=sandbox_id, guest_path="/etc/hostname", host_path="../e2e-escape.txt")
        check("path traversal out of the transfer folder is rejected",
              bool(escape.get("isError")) and not (transfer.parent / "e2e-escape.txt").exists(), text(escape))

        folder = transfer / "e2e-folder"
        (folder / "sub dir").mkdir(parents=True, exist_ok=True)
        (folder / "a.txt").write_text("alpha\n", encoding="utf-8")
        (folder / "sub dir" / "b.txt").write_text("beta\n", encoding="utf-8")
        sent = client.tool("sandbox_copy_in", sandbox_id=sandbox_id, host_path="e2e-folder", guest_path="/tmp/e2e folder")
        check("sandbox_copy_in copies a folder", not sent.get("isError") and "2 files" in text(sent), text(sent))
        client.tool("sandbox_run", sandbox_id=sandbox_id, timeout_seconds=30, command=(
            "cd '/tmp/e2e folder' && cat a.txt 'sub dir/b.txt' > joined.txt && ln -s /etc/passwd leak && echo x > 'bad:name'"))
        got = client.tool("sandbox_copy_out", sandbox_id=sandbox_id, guest_path="/tmp/e2e folder", host_path="e2e-folder-back")
        back = transfer / "e2e-folder-back"
        check("sandbox_copy_out copies a folder", not got.get("isError") and "3 files" in text(got), text(got))
        check("folder made the round trip",
              (back / "joined.txt").read_text(encoding="utf-8") == "alpha\nbeta\n"
              and (back / "sub dir" / "b.txt").read_text(encoding="utf-8") == "beta\n")
        check("links and unusable names from the sandbox are left out",
              "2 entries were left out" in text(got) and sorted(p.name for p in back.iterdir()) == ["a.txt", "joined.txt", "sub dir"],
              text(got))

        started = client.tool("sandbox_start", sandbox_id=sandbox_id, command="cd /tmp && exec python3 -m http.server 8000")
        check("sandbox_start returns a job id", not started.get("isError") and text(started).startswith("job-"), text(started))
        job_id = text(started).strip()
        asked = client.tool("sandbox_run", sandbox_id=sandbox_id, timeout_seconds=30,
                            command="sleep 2; wget -qO- http://localhost:8000/ | grep -c e2e-in.txt")["structuredContent"]
        check("a later command can talk to the background server", asked["stdout"].strip() == "1", asked)
        state = client.tool("sandbox_check", sandbox_id=sandbox_id, job_id=job_id)["structuredContent"]
        check("sandbox_check shows it running, with its output",
              state["status"] == "running" and "GET /" in state["stderr"], state)
        stopped = client.tool("sandbox_stop", sandbox_id=sandbox_id, job_id=job_id)
        check("sandbox_stop stops it", not stopped.get("isError") and "stopped" in text(stopped), text(stopped))
        state = client.tool("sandbox_check", sandbox_id=sandbox_id, job_id=job_id)["structuredContent"]
        check("sandbox_check then shows it stopped", state["status"] == "stopped", state)

        def run_ok(command, timeout=60):
            return client.tool("sandbox_run", sandbox_id=sandbox_id, command=command,
                               timeout_seconds=timeout)["structuredContent"]

        run_ok("echo original > ~/cp.txt; mkdir -p ~/cpdir; echo keep > ~/cpdir/f.txt; sync")
        began = time.time()
        saved = client.tool("sandbox_checkpoint", sandbox_id=sandbox_id, name="e2e")
        check(f"sandbox_checkpoint saves the sandbox ({time.time() - began:.0f}s)",
              not saved.get("isError") and "'e2e' saved" in text(saved), text(saved))
        listed = client.tool("sandbox_list")["structuredContent"]["result"]
        check("sandbox_list shows the checkpoint",
              [s["checkpoints"] for s in listed if s["sandbox_id"] == sandbox_id] == [["e2e"]], listed)
        run_ok("echo changed > ~/cp.txt; rm -rf ~/cpdir; echo extra > ~/extra.txt; sync")
        began = time.time()
        restored = client.tool("sandbox_restore", sandbox_id=sandbox_id, name="e2e")
        check(f"sandbox_restore rolls the sandbox back ({time.time() - began:.0f}s)",
              not restored.get("isError"), text(restored))
        back = run_ok("cat ~/cp.txt; cat ~/cpdir/f.txt; ls ~/extra.txt 2>/dev/null | wc -l")
        check("the saved files are back and the later changes are gone",
              back["stdout"].split() == ["original", "keep", "0"], back)
        refused = client.tool("sandbox_restore", sandbox_id=sandbox_id, name="no-such-checkpoint")
        check("restoring an unknown checkpoint is refused and says what exists",
              bool(refused.get("isError")) and "e2e" in text(refused), text(refused))

        shot = client.tool("sandbox_screenshot", sandbox_id=sandbox_id)
        images = [c for c in shot["content"] if c["type"] == "image"]
        check("sandbox_screenshot returns a PNG",
              len(images) == 1 and base64.b64decode(images[0]["data"]).startswith(b"\x89PNG"))

        destroyed = client.tool("sandbox_destroy", sandbox_id=sandbox_id)
        check("sandbox_destroy succeeds", not destroyed.get("isError"), text(destroyed))
        remaining = client.tool("sandbox_list")["structuredContent"]["result"]
        check("sandbox is gone from the list", all(s["sandbox_id"] != sandbox_id for s in remaining), remaining)
        check("sandbox folder is deleted", not (Path(os.environ["VMSANDBOX_CLONES_DIR"]) / sandbox_id).exists())

        audit_path = Path(os.environ.get("VMSANDBOX_AUDIT_LOG")
                          or Path(os.environ["VMSANDBOX_CLONES_DIR"]) / "audit.jsonl")
        entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        mine = [e for e in entries if e.get("sandbox_id") == sandbox_id]
        check("audit log has one entry per operation, in order",
              [e["event"] for e in mine] == ["create", "run", "run", "run", "copy_in", "run",
                                             "copy_out", "copy_out", "copy_in", "run", "copy_out",
                                             "start", "run", "check", "stop", "check",
                                             "run", "checkpoint", "run", "restore", "run", "restore",
                                             "screenshot", "destroy"],
              [e["event"] for e in mine])
        check("audit log records the command text and exit code",
              (mine[1]["command"], mine[1]["exit_code"]) == ("echo out; echo err >&2; exit 3", 3), mine[1])
        check("audit log records the refused path traversal",
              mine[7]["ok"] is False and mine[7]["host_path"] == "../e2e-escape.txt", mine[7])
        sandbox_id = None

        small = client.tool("sandbox_create", memory_mb=1024, cpus=1)
        check("sandbox_create(memory_mb=1024, cpus=1) succeeds", not small.get("isError"), text(small))
        sandbox_id = text(small).strip()
        sized = client.tool("sandbox_run", sandbox_id=sandbox_id, timeout_seconds=30,
                            command="echo $(nproc) $(awk '/MemTotal/{print $2}' /proc/meminfo)")["structuredContent"]
        cpus_seen, memory_kb = (int(x) for x in sized["stdout"].split())
        check("the sandbox really has 1 CPU and about 1 GB", cpus_seen == 1 and 800_000 < memory_kb < 1_100_000, sized)
        listed = client.tool("sandbox_list")["structuredContent"]["result"]
        check("sandbox_list shows its size",
              [(s["memory_mb"], s["cpus"]) for s in listed if s["sandbox_id"] == sandbox_id] == [(1024, 1)], listed)
        greedy = client.tool("sandbox_create", memory_mb=262144)
        check("asking for more memory than the base VM has is refused, with the maximum",
              bool(greedy.get("isError")) and "at most" in text(greedy), text(greedy))
        client.tool("sandbox_destroy", sandbox_id=sandbox_id)
        sandbox_id = None

        if os.environ.get("VMSANDBOX_REMOTE_INPUT", "").strip().lower() in ("1", "true", "yes", "on"):
            # Written for the tested base VM: Debian 13 with GNOME at 1280x800, where the
            # login screen shows the user in the middle. Adjust the click for another image.
            def tool_ok(name, **arguments):
                reply = client.tool(name, sandbox_id=sandbox_id, **arguments)
                check(f"{name}({', '.join(str(v)[:24] for v in arguments.values())!r}) succeeds",
                      not reply.get("isError"), text(reply))

            created = client.tool("sandbox_create")
            sandbox_id = text(created).strip()
            time.sleep(40)                                   # the graphical login screen appears
            tool_ok("sandbox_click", x=640, y=348)           # pick the user
            time.sleep(3)
            tool_ok("sandbox_type", text=os.environ["VMSANDBOX_GUEST_PASSWORD"] + "\n")
            time.sleep(25)                                   # the desktop loads
            tool_ok("sandbox_key", keys="Escape")
            tool_ok("sandbox_key", keys="super")             # application search
            time.sleep(2)
            tool_ok("sandbox_type", text="terminal")
            time.sleep(2)
            tool_ok("sandbox_key", keys="Return")
            time.sleep(8)
            wanted = "Hello, World! ~@#$%^&*()_+{}|:<>? aZ09 -=[];',./"
            tool_ok("sandbox_type", text=f'echo "{wanted}" > /tmp/typed.txt\n')
            time.sleep(3)
            typed = client.tool("sandbox_run", sandbox_id=sandbox_id, command="cat /tmp/typed.txt",
                                timeout_seconds=30)["structuredContent"]
            check("typed text, capitals and symbols arrive exactly", typed["stdout"] == wanted + "\n", typed)
            tool_ok("sandbox_type", text="sleep 300\n")
            time.sleep(1)
            tool_ok("sandbox_key", keys="ctrl+c")
            tool_ok("sandbox_type", text="echo interrupted > /tmp/ctrlc.txt\n")
            time.sleep(3)
            ctrl = client.tool("sandbox_run", sandbox_id=sandbox_id, timeout_seconds=30,
                               command="cat /tmp/ctrlc.txt; pgrep -x sleep | wc -l")["structuredContent"]
            check("a shortcut (ctrl+c) reaches the program", ctrl["stdout"].split() == ["interrupted", "0"], ctrl)
            tool_ok("sandbox_scroll", x=640, y=400, direction="up", amount=5)
            tool_ok("sandbox_drag", from_x=300, from_y=300, to_x=500, to_y=320)
            outside = client.tool("sandbox_click", sandbox_id=sandbox_id, x=5000, y=10)
            check("a click outside the screen is refused", bool(outside.get("isError")), text(outside))
            log_text = audit_path.read_text(encoding="utf-8")
            check("the guest password typed at the login screen is blanked in the audit log",
                  '"text": "[guest password]\\n"' in log_text, "")
            client.tool("sandbox_destroy", sandbox_id=sandbox_id)
            sandbox_id = None
        else:
            print("  (keyboard and mouse not switched on; skipping their checks)")

        if os.environ.get("VMSANDBOX_WEB_VNET"):
            created = client.tool("sandbox_create", network="web")
            check("sandbox_create(network='web') succeeds", not created.get("isError"), text(created))
            sandbox_id = text(created).strip()
            web = client.tool("sandbox_run", sandbox_id=sandbox_id, timeout_seconds=120, command=(
                "sleep 5; wget -q -T 30 -O /dev/null https://example.com; echo public=$?; "
                "wget -q -T 10 -t 1 -O /dev/null http://192.168.0.1/; echo private=$?; "
                "timeout 3 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>/dev/null; echo direct=$?; "
                "ip route show default | wc -l"))["structuredContent"]
            seen = web["stdout"].split()
            check("web sandbox downloads from a public site through the proxy", "public=0" in seen, web)
            check("web sandbox is refused a private address", "private=8" in seen, web)
            check("web sandbox has no direct route to the internet", "direct=0" not in seen and seen[-1] == "0", web)
            client.tool("sandbox_destroy", sandbox_id=sandbox_id)
            sandbox_id = None
        else:
            print("  (network='web' not configured; skipping its checks)")
        print("PASS")
        return 0
    except AssertionError:
        print("FAIL")
        return 1
    finally:
        if sandbox_id:
            client.tool("sandbox_destroy", sandbox_id=sandbox_id)
        client.close()
        for name in ("e2e-in.txt", "e2e-out.txt"):
            (transfer / name).unlink(missing_ok=True)
        for name in ("e2e-folder", "e2e-folder-back"):
            shutil.rmtree(transfer / name, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
