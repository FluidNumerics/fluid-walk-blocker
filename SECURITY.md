# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the **Security** tab of this
repository, then **Report a vulnerability**. That keeps the report private
until a fix exists. Please do not open a public issue for a suspected
vulnerability.

Expect an acknowledgement within a week. If a report is accepted, the advisory
names the reporter unless the reporter asks otherwise.

## What this tool is, before you file

Two design facts decide most of what is and is not a vulnerability here. Both
are deliberate, both are documented, and a report that one of them is true will
be closed with a pointer to the record that decided it.

**Layer 1 is advisory, not enforcement (ADR-0001).** The PATH shim is bypassable
— by an absolute path, a private `PATH`, an ephemeral environment, a container,
a scheduler job script, or a shell function. That is not a defect being
tolerated; it is the reason Layer 2 exists. A report that the shim can be
circumvented is a restatement of its design.

**The installer expects an already-root operator (ADR-0004, ADR-0021).**
`deploy.py --system` installs because the person running it holds root on the
target node, which is a standing position of trust that already carries the
authority to do everything the installer does by hand. Root *is* the
authorization; the absence of a second confirmation is not a missing check.

## In scope

- Any path by which the monitored, unprivileged account can influence what the
  root-owned timer executes — a writable component in the installed trust
  chain, a symlinked hook file, a payload whose ownership the install did not
  assert.
- Escalation from an unprivileged account into the guard itself, or any way to
  make the installed components run attacker-controlled code as root.
- The audit trail being writable by anyone but root, or readable content in it
  that should not be.
- A way to make the reaper act on a process it should not — in particular, any
  route to a kill that the `/proc` step did not independently name.
- Leakage of the customer term list, or of any value derived from it, into a CI
  log, an error message, or a committed file.
- Command injection or unsafe interpolation in `deploy.py`, `install.sh`,
  `guard.sh`, or the reaper.

## Out of scope

- Bypassing Layer 1, per above.
- That `--kill` can kill processes. It is opt-in, off by default, and promoted
  only by a person deciding so at a site.
- Damage done by running the installer as root against a node you should not
  have. The tool assumes the operator is authorized on the node.
- Findings that require root on the node to begin with, where root is already
  the tool's trust boundary.
