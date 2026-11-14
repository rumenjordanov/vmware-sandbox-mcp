# Security policy

vmware-sandbox-mcp exists to keep untrusted code away from your computer, so reports of ways around it are welcome.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Use GitHub's private reporting instead: on the repository page, open the **Security** tab and choose **Report a vulnerability**. If that option is not available, open an issue that only says you have a security report, without details, and you will be contacted.

Useful things to include:

- what an agent, or code inside a sandbox, was able to do that it should not;
- the network mode and settings in use;
- your Windows and VMware Workstation versions;
- steps to reproduce.

## What counts

In scope, for example:

- touching a virtual machine or folder the server did not create;
- reading or writing host files outside the transfer folder;
- reaching the host or the local network from a `none` or `web` sandbox;
- getting agent-supplied text executed on the host;
- reading or changing the audit log from the agent's side.

Out of scope: the documented limits in [docs/security.md](docs/security.md), such as `nat` mode reaching the local network, and vulnerabilities in VMware Workstation itself (report those to its vendor).

## Supported versions

Only the latest version on the `main` branch.
