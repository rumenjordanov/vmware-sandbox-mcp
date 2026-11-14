# vmware-sandbox-mcp

An MCP server that gives an AI agent a disposable Linux computer, backed by VMware Workstation on your own machine. The agent can create a sandbox from a clean snapshot, run commands in it, move files in and out, take a screenshot, and throw it away, without being able to touch your host or your other VMs.

This is deliberately **not** a general VMware management server. The agent never sees VMware concepts; it sees "a computer I can use and destroy".

## Demo scenario

> **You:** I found `install.sh` on a forum. Put it in a sandbox, run it with internet access, and tell me what it actually does before I run it for real.

The agent then:

1. calls `sandbox_create(network="nat")` and gets `sbx-3f9a1c2e` about 25 seconds later;
2. calls `sandbox_copy_in` to copy `install.sh` from your transfer folder into the sandbox;
3. calls `sandbox_run` to execute it, then again to inspect new files, processes and services;
4. calls `sandbox_copy_out` to save a log of what changed into your transfer folder;
5. calls `sandbox_destroy`. Whatever the script did is gone.

## Tools

| Tool | What it does |
|---|---|
| `sandbox_create(network)` | New sandbox from the clean snapshot. `network` is `"none"` (default) or `"nat"`. Returns a `sandbox_id`. |
| `sandbox_run(sandbox_id, command, timeout_seconds)` | Runs a bash command. Returns stdout, stderr, exit code, and whether it timed out. |
| `sandbox_copy_in(sandbox_id, host_path, guest_path)` | Copies one file from the transfer folder into the sandbox. |
| `sandbox_copy_out(sandbox_id, guest_path, host_path)` | Copies one file from the sandbox into the transfer folder. |
| `sandbox_screenshot(sandbox_id)` | PNG of the sandbox's screen. |
| `sandbox_destroy(sandbox_id)` | Powers off and deletes the sandbox. |
| `sandbox_list()` | Active sandboxes with age and network mode. |

## Requirements

- VMware Workstation Pro with the `vmrun` command-line tool (developed and tested on Workstation 25H2, vmrun 1.17.0, Windows 11 host). Linux hosts are untested.
- Python 3.11 or newer.
- A Linux base VM prepared as described below.

## Install

Not on PyPI yet. Run it straight from a checkout or from Git with one command:

```
uvx --from git+https://github.com/<your-account>/vmware-sandbox-mcp vmware-sandbox-mcp
```

or

```
pip install git+https://github.com/<your-account>/vmware-sandbox-mcp
```

The server speaks MCP over stdio and is normally started by your MCP client (see below), not by hand.

## Base VM setup

1. Create a Linux VM in VMware Workstation (Debian 13 is what this was tested with). Keep it in its own folder.
2. Inside the VM, install the guest tools and make sure bash, `timeout` and `tar` exist (they do on any standard Debian install):
   ```
   sudo apt-get install open-vm-tools
   ```
3. Create the user account the server will log in as. Its name and password go into environment variables, never into this repository.
4. Optional, if the agent should be able to install software: give that user passwordless `sudo`:
   ```
   echo 'debian ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/sandbox
   chmod 440 /etc/sudoers.d/sandbox
   ```
   Installing packages also needs a sandbox created with `network="nat"`.
5. **Shut the VM down completely** (not suspend), then take a snapshot, for example named `clean`. Every sandbox is a linked clone of this snapshot.
6. Do not delete or move the base VM or that snapshot while sandboxes exist; linked clones read from it.

To update the base image later: boot the base VM, make changes, power off, take a new snapshot, and point `VMSANDBOX_SNAPSHOT` at it.

## Configuration

All configuration is through environment variables. The server refuses to start if a required one is missing.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `VMSANDBOX_BASE_VMX` | yes | | Full path of the base VM's `.vmx` file. |
| `VMSANDBOX_SNAPSHOT` | yes | | Name of the powered-off snapshot to clone from. |
| `VMSANDBOX_CLONES_DIR` | yes | | Folder where sandboxes live. Created if missing. Use it for nothing else. |
| `VMSANDBOX_TRANSFER_DIR` | yes | | The only host folder `copy_in` / `copy_out` can read or write. Created if missing. |
| `VMSANDBOX_GUEST_USER` | yes | | Login name inside the base VM. |
| `VMSANDBOX_GUEST_PASSWORD` | yes | | Password for that login. |
| `VMSANDBOX_MAX_SANDBOXES` | no | `3` | Maximum sandboxes existing at once. |
| `VMSANDBOX_MAX_AGE_MINUTES` | no | `120` | Sandboxes older than this are destroyed automatically. |
| `VMSANDBOX_BOOT_TIMEOUT_SECONDS` | no | `180` | How long `sandbox_create` waits for the guest to become ready. |
| `VMSANDBOX_VMRUN_PATH` | no | auto-detected | Full path of `vmrun` if it is not in the standard location. |
| `VMSANDBOX_AUDIT_LOG` | no | `audit.jsonl` in the clones folder | File the audit log is appended to. |

The clones folder, the transfer folder and the base VM folder must not overlap; the server checks this at startup.

### Audit log

Every operation is appended to the audit log as one line of JSON: sandboxes created and destroyed (including automatic removal of expired or orphaned ones), commands run, files copied and screenshots taken. Refused and failed operations are recorded too, with the reason. Example:

```json
{"time": "2026-11-14T12:08:19Z", "event": "run", "sandbox_id": "sbx-cbe8c2a2", "command": "echo out; echo err >&2; exit 3", "timeout_seconds": 30, "ok": true, "seconds": 4.1, "exit_code": 3, "timed_out": false, "stdout_bytes": 4, "stderr_bytes": 4}
```

What to know about it:

- **Command text is recorded in full.** If the agent runs a command containing a secret (a token in a `curl` call, say), that secret is in the log in plain text. Treat the file accordingly.
- **Command output is not recorded**, only its size, the exit code and the duration. The guest password is never recorded.
- **Times are UTC.**
- **The agent cannot read or change the log.** The server refuses to start if the log is inside the transfer folder, the base VM folder or a sandbox folder.
- **The file grows without limit.** There is no rotation; delete or archive it yourself.
- **Logging never blocks an operation.** If the file cannot be written (disk full, for example), the server prints a warning to stderr and carries on, so a sandbox can always still be destroyed.

### Check your setup

With the environment variables set, run:

```
vmware-sandbox-mcp doctor
```

It checks the settings, `vmrun`, the snapshot (exists and was taken powered off), both folders and the audit log. It then creates one test sandbox, logs in, runs a command and destroys it again, which takes about a minute. Each problem is printed with what to fix, and the exit code is 0 only if nothing failed. `doctor --no-boot` skips the test sandbox.

### Claude Code

Add to `.mcp.json` in your project (or your user-level MCP configuration):

```json
{
  "mcpServers": {
    "vmware-sandbox": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/<your-account>/vmware-sandbox-mcp", "vmware-sandbox-mcp"],
      "env": {
        "VMSANDBOX_BASE_VMX": "E:\\Virtual Machines\\sandbox-base\\Debian 13.x 64-bit.vmx",
        "VMSANDBOX_SNAPSHOT": "clean",
        "VMSANDBOX_CLONES_DIR": "E:\\vmsandbox-clones",
        "VMSANDBOX_TRANSFER_DIR": "E:\\vmsandbox-transfer",
        "VMSANDBOX_GUEST_USER": "${VMSANDBOX_GUEST_USER}",
        "VMSANDBOX_GUEST_PASSWORD": "${VMSANDBOX_GUEST_PASSWORD}"
      }
    }
  }
}
```

Claude Code expands `${VAR}` from your own environment, so the credentials stay out of the file.

### Claude Desktop

Add to `claude_desktop_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "vmware-sandbox": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/<your-account>/vmware-sandbox-mcp", "vmware-sandbox-mcp"],
      "env": {
        "VMSANDBOX_BASE_VMX": "E:\\Virtual Machines\\sandbox-base\\Debian 13.x 64-bit.vmx",
        "VMSANDBOX_SNAPSHOT": "clean",
        "VMSANDBOX_CLONES_DIR": "E:\\vmsandbox-clones",
        "VMSANDBOX_TRANSFER_DIR": "E:\\vmsandbox-transfer",
        "VMSANDBOX_GUEST_USER": "your-guest-user",
        "VMSANDBOX_GUEST_PASSWORD": "your-guest-password"
      }
    }
  }
}
```

Here the guest password sits in that file in plain text. Use a password that is used nowhere else; it only unlocks a throwaway VM.

## Security model

What the server enforces, in code and covered by tests:

- **It only touches VMs it created.** Every VMware call goes through one wrapper class with a command allowlist. The base VM may only be read (`listSnapshots`) and cloned from; it is never started, stopped, modified or deleted. Every other command is refused unless its target is a `.vmx` at `<clones folder>/<sandbox id>/`, after resolving `..`, symlinks and junctions.
- **It only deletes what it marked.** A folder is removed only if it is directly inside the clones folder, is named like a sandbox id, and contains the marker file the server wrote. Anything else in the clones folder is left alone.
- **Isolation before first boot.** Each clone's `.vmx` is rewritten before it ever starts: shared folders off, clipboard and drag-and-drop off, and the CD-ROM, USB controller, sound card and serial/parallel ports removed.
- **No network by default.** `network="none"` removes the network adapter from the clone entirely.
- **One transfer folder.** `copy_in` and `copy_out` only accept host paths that resolve inside the transfer folder. Traversal, absolute paths elsewhere, and links pointing out of the folder are rejected.
- **No host shell.** `vmrun` is called with an argument list, never through a shell. A command for the sandbox travels as file content, never as a command-line argument.
- **Limits.** Every command has a timeout (1 to 3600 seconds) enforced inside the guest, with a second host-side timeout. Output is cut at 1 MB per stream. The number of sandboxes is capped. Sandboxes past the maximum age are destroyed, and sandboxes left behind by a crash or reboot are removed at startup.

### Limits of this model, stated honestly

- **The boundary is VMware's.** A sandbox is exactly as contained as a VMware virtual machine. A hypervisor escape vulnerability would defeat it. Keep Workstation updated.
- **NAT is not internet-only.** A sandbox created with `network="nat"` can reach the internet, **and also your host and other devices on your local network** (routers, NAS, printers, other computers), through VMware's NAT. Do not use `nat` for code you consider actively hostile.
- **What comes out is untrusted.** Files copied out of a sandbox, and the text a command prints, were produced by whatever ran in there. Do not open or run copied-out files without thought, and be aware that command output could contain text designed to mislead the agent.
- **The transfer folder is fully exposed to the agent.** It can read every file in it and overwrite files there. Keep nothing in it that you would not hand to the agent.
- **The guest password is visible locally.** `vmrun` only accepts it as a command-line argument, so other processes of local users can see it while a call runs. It may also sit in your MCP client's configuration file.
- **The base VM folder is written to by VMware.** Creating a linked clone makes VMware add a `snapshotN.cloneM` line to the base VM's `.vmsd` file. These lines are harmless but are not removed when a sandbox is destroyed, and Workstation will consider the snapshot "in use" by clones.
- **Sandboxes outlive the server.** They are not destroyed when the MCP client exits (two clients may share them). They disappear when destroyed, when they exceed the maximum age, or at the next startup if they are no longer running.
- **Resources.** Each sandbox uses the base VM's memory and CPU allocation, and its disk grows until it is destroyed. The cap limits the count, not the size.
- **Races.** Path checks happen just before the copy. Another program on the host changing links inside the transfer folder at exactly that moment could defeat them.
- **Screenshots are view-only.** There is no keyboard or mouse tool, so a graphical guest usually shows its login screen.

### Future improvements

- Block access to the host and local network in NAT mode (dedicated virtual network with firewall rules).
- Keyboard and mouse input.
- Copying whole folders.
- Publishing to PyPI.

## Development

```
uv venv
uv pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

The unit tests replace the `vmrun` process with a fake, so the real wrapper and its path guard are exercised in every test and no VMware installation is needed.

End-to-end test against your real base VM (creates and destroys one sandbox, about a minute), with the environment variables above set:

```
.venv\Scripts\python scripts\e2e.py
```

## License

MIT. See [LICENSE](LICENSE).
