# Security Policy

This repository is the community catalog of MCP servers that OtoDock installs
and runs inside agent sandboxes. A malicious or careless catalog entry is a
supply-chain problem for everyone who installs it, so reports here get the
same priority as platform vulnerabilities.

## Reporting a vulnerability

Please report vulnerabilities **privately** — do not open a public issue for
anything security-sensitive.

- **Preferred:** GitHub private vulnerability reporting — go to this
  repository's **Security** tab → **Report a vulnerability**. Reports land
  directly with the maintainer, privately.
- **Email:** [security@otodock.io](mailto:security@otodock.io)

You'll get an acknowledgment within **72 hours** and a status update as the
report is triaged. Confirmed issues are fixed ahead of feature work, with
credit to the reporter (if you'd like it).

This catalog is rolling — reports are always assessed against the current
`main` branch. Vulnerabilities in the OtoDock **platform** itself belong on
[OtoDock/oto-dock](https://github.com/OtoDock/oto-dock/security) instead.

There is no paid bounty program at this time — just fast fixes, honest
credit, and our thanks.

## Scope

Especially interesting to us:

- Catalog entries that exfiltrate data, phone home, or abuse the credentials
  they are handed
- Credential leakage in a server (tokens written to disk, logged, or exposed
  through tool output)
- Command / path injection reachable through a server's tools
