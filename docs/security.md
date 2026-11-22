# Security model

What vmware-sandbox-mcp enforces, what it does not, and what the audit log records. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## What the server enforces

What the server enforces, in code and covered by tests:

- **It only touches VMs it created.** Every VMware call goes through one wrapper class with a command allowlist. The base VM may only be read (`listSnapshots`) and cloned from; it is never started, stopped, modified or deleted. Every other command is refused unless its target is a `.vmx` at `<clones folder>/<sandbox id>/`, after resolving `..`, symlinks and junctions.
- **Base images are an allowlist.** An agent can only start from the snapshots you listed in `VMSANDBOX_IMAGES` (and the default). The name is checked before anything is cloned, and a name with control characters is refused again by the VMware wrapper. Cloning from a snapshot is the only thing ever done to the base VM.
- **It only deletes what it marked.** A folder is removed only if it is directly inside the clones folder, is named like a sandbox id, and contains the marker file the server wrote. Anything else in the clones folder is left alone.
- **Isolation before first boot.** Each clone's `.vmx` is rewritten before it ever starts: shared folders off, clipboard and drag-and-drop off, and the CD-ROM, USB controller, sound card and serial/parallel ports removed.
- **No network by default.** `network="none"` removes the network adapter from the clone entirely.
- **Web access without local access.** `network="web"` attaches the clone only to the dedicated host-only network, and the proxy decides every connection: public addresses and the allowed ports only. The destination rules, including IPv6 forms that hide an IPv4 address and names that resolve to private addresses, are covered by tests.
- **Web access can be limited to named sites.** With `VMSANDBOX_WEB_ALLOW` set, the proxy forwards only to listed names (a name itself, or one level of wildcard below a name). Anything else, numeric addresses included, is refused before any lookup. The address checks still apply to listed sites. The matching rules are covered by tests.
- **One transfer folder.** `copy_in` and `copy_out` only accept host paths that resolve inside the transfer folder. Traversal, absolute paths elsewhere, and links pointing out of the folder are rejected.
- **Folders are unpacked defensively.** A folder travels as one archive. Coming out of a sandbox, that archive is treated as hostile: only plain files and folders are created, each checked to land inside the target folder. Links, devices, absolute or `..` names, and names Windows cannot store are left out, and the reply says how many. Going in, a host folder that contains a link or junction is refused, so a link cannot pull files from outside the transfer folder into a sandbox.
- **No host shell.** `vmrun` is called with an argument list, never through a shell. A command for the sandbox travels as file content, never as a command-line argument.
- **Keyboard and mouse stay on your computer.** The remote display used for input is off unless you enable it. When on, it listens on `127.0.0.1` only, so it is not reachable from your network or from any sandbox, and it requires a per-sandbox random password. The server refuses a display that does not ask for a password.
- **Limits.** Every command has a timeout (1 to 3600 seconds) enforced inside the guest, with a second host-side timeout. Output is cut at 1 MB per stream. The number of sandboxes is capped. Sandboxes past the maximum age are destroyed, and sandboxes left behind by a crash or reboot are removed at startup.

## Limits of this model, stated honestly

- **The boundary is VMware's.** A sandbox is exactly as contained as a VMware virtual machine. A hypervisor escape vulnerability would defeat it. Keep Workstation updated.
- **NAT is not internet-only.** A sandbox created with `network="nat"` can reach the internet, **and also your host and other devices on your local network** (routers, NAS, printers, other computers), through VMware's NAT. Do not use `nat` for code you consider actively hostile; use `web`.
- **Web mode depends on the host setup.** The proxy's rules are enforced by this project's code, but keeping the sandbox away from your computer's other ports is done by the Windows Firewall rules from the setup script. If they are removed or the network is reconfigured, that protection is gone; `doctor` detects it.
- **Web mode still lets data out.** Code in a `web` sandbox can send anything it has to any public website. It protects your network, not the secrecy of what you put in the sandbox.
- **A site list limits where sandboxes connect, not what they send.** Listed sites can still receive anything the sandbox has, and code in a sandbox can use a listed site as a relay if that site fetches or forwards arbitrary addresses for its users. Keep the list to sites that do not.
- **Web mode is web only.** No ping, no DNS lookups in the sandbox, no SSH or other protocols, and one request per connection for plain HTTP. Programs that ignore the proxy variables have no network.
- **Web sandboxes can see each other.** They share one private network. The proxy only works while the server is running.
- **The proxy is code on your host that reads untrusted input.** It is small and tested, but it is attack surface that `none` and `nat` do not have.
- **What comes out is untrusted.** Files copied out of a sandbox, and the text a command prints, were produced by whatever ran in there. Do not open or run copied-out files without thought, and be aware that command output could contain text designed to mislead the agent.
- **The transfer folder is fully exposed to the agent.** It can read every file in it and overwrite files there. Keep nothing in it that you would not hand to the agent.
- **The guest password is visible locally.** `vmrun` only accepts it as a command-line argument, so other processes of local users can see it while a call runs. It may also sit in your MCP client's configuration file.
- **The base VM folder is written to by VMware.** Creating a linked clone makes VMware add a `snapshotN.cloneM` line to the base VM's `.vmsd` file. These lines are harmless but are not removed when a sandbox is destroyed, and Workstation will consider the snapshot "in use" by clones.
- **Sandboxes outlive the server.** They are not destroyed when the MCP client exits (two clients may share them). They disappear when destroyed, when they exceed the maximum age, or at the next startup if they are no longer running.
- **Resources.** Each sandbox uses the base VM's memory and CPU allocation by default. An agent can ask for less, and for more only up to the limits you set (`VMSANDBOX_MAX_MEMORY_MB`, `VMSANDBOX_MAX_CPUS`; by default the base VM's own size). A sandbox's disk grows until it is destroyed, and the cap on sandboxes limits their number, not their disk use.
- **Races.** Path checks happen just before the copy. Another program on the host changing links inside the transfer folder at exactly that moment could defeat them.
- **Folder copies are not exact mirrors.** Files copied in lose the executable flag (Windows has none). Copied out, links, special files (pipes, devices) and names not valid on Windows (containing `:` `?` `*` or a backslash, ending in a dot, or reserved names like `CON`) are left out. Two names differing only in case end up as one file. The limit is 100,000 files or 4 GB per copy.
- **Background commands are a convenience, not a limit.** The cap of 8 per sandbox is counted inside the sandbox, so code running there can get around it, just as any command can start more processes with `&`. Their output is kept on the sandbox's own disk without a size limit; a check returns the newest 1 MB. A program that detaches itself into its own session survives `sandbox_stop`, until the sandbox is destroyed.
- **The remote display is protected against the network, not against your own account.** With keyboard and mouse on, any program running as you on this computer could connect to a sandbox's display if it obtained the password (VNC passwords are 8 characters and the protocol is old). Such a program could already control your VMs through VMware's own tools. The password is also stored in plain text in the sandbox's `.vmx` and in the state file, both in the clones folder.
- **Keyboard and mouse are slow and blind.** Each action is a separate step and the agent only sees what it asks a screenshot for. Prefer `sandbox_run` whenever a command can do the job.

## Audit log

Every operation is appended to the audit log as one line of JSON: sandboxes created and destroyed (including automatic removal of expired or orphaned ones), commands run, background commands started, checked and stopped, files copied, screenshots taken, clicks, typed text and key presses, checkpoints saved and restored, and every request a `web` sandbox makes through the proxy (destination, address, allowed or refused). Refused and failed operations are recorded too, with the reason. Example:

```json
{"time": "2026-11-14T12:08:19Z", "event": "run", "sandbox_id": "sbx-cbe8c2a2", "command": "echo out; echo err >&2; exit 3", "timeout_seconds": 30, "ok": true, "seconds": 4.1, "exit_code": 3, "timed_out": false, "stdout_bytes": 4, "stderr_bytes": 4}
```

What to know about it:

- **Command text is recorded in full.** If the agent runs a command containing a secret (a token in a `curl` call, say), that secret is in the log in plain text. Treat the file accordingly.
- **Command output is not recorded**, only its size, the exit code and the duration. The guest password is never recorded.
- **Times are UTC.**
- **The agent cannot read or change the log.** The server refuses to work if the log is inside the transfer folder, the base VM folder or a sandbox folder.
- **The file grows without limit.** There is no rotation; delete or archive it yourself.
- **Logging never blocks an operation.** If the file cannot be written (disk full, for example), the server prints a warning to stderr and carries on, so a sandbox can always still be destroyed.
