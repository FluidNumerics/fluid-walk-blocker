# ADR-0006: The source tree stays user-owned; only the installed components are root's

**Status:** accepted, 2026-09-16
**Evidence:** held privately by Fluid Numerics, keyed ADR-0006 — see `docs/evidence.md`

## Context

Automated review raised this in three separate rounds, each time correctly,
and each time asking for the same remedy.

`deploy.py` is run as root by an authorized sysadmin, out of a checkout that
sysadmin owns. It reads the payload out of that checkout and installs it under
`[install].prefix`, root-owned. The observation is that the *destination*
checks — `--no-preserve=ownership`, the `chown`, `unowned_by()`,
`untrusted_prefix_chain()` — authenticate the destination's **metadata**, not
the bytes that arrived there. The checkout's owner can rewrite
`node/shim/install.sh` after the operator has started `deploy.py`, and those
bytes are then copied, made root-owned, pass every ownership check, and are
executed as root by the systemd unit.

The proposed remedy was: **refuse to deploy from a source tree that is not
itself root-owned and non-writable.** The operator would clone as themselves,
then copy the checkout into a root-owned directory, and run `deploy.py` from
there.

## Decision

**Rejected.** The repo is cloned by a sysadmin into a folder that user owns,
and the installation places components under root control. That is the
workflow, and it does not change.

What *was* taken from the finding is the timing half, which is a real defect
and is fixed:

- `stage_payload()` reads every payload source **once**, into a 0700
  root-owned snapshot under `[install].staging_parent`, and the install copies
  from the snapshot. The payload copies read the snapshot; the only remaining
  direct read of the checkout is the snapshot's own.
- The snapshot is taken **between the argument checks and the first
  `systemctl`** — after everything that can refuse (all pure Python, none of
  it blocking) and before anything that can block. It first landed just
  before the payload copies, i.e. after the whole unit teardown, which left
  the window as wide as a `systemctl stop --now` takes.

## Consequences

**The residual window runs from Python reading `deploy.py` until
`stage_payload()` has finished copying, and the snapshot is not atomic.**
That second half was once described here as "microseconds of pure-Python
argument validation", which was wrong and a later review was right to say
so. `stage_payload()` spawns a subprocess per step — an `install -d` plus a
copy for each payload source — and the copies of directory sources are
recursive walks. An entry the copy has not reached yet can be rewritten while
an earlier one is being read, so the window spans the whole snapshot rather
than closing before it starts.

What the snapshot does buy is still worth having: it collapses the window
from *minutes* — it used to sit after the argument checks and the entire
`systemctl` teardown, so a `systemctl stop --now` was inside it — to the
duration of one small `cp` of a few hundred kilobytes, and it makes the bytes
that are executed the bytes that were copied rather than whatever the
checkout holds later on.

**And it is the same exposure the operator already accepts** by running this
script out of that checkout at all: `deploy.py`'s own bytes are read from the
same user-owned directory, by the same root process, one moment earlier. A
checkout owner who can win the copy race can equally rewrite `deploy.py`
before Python opens it. Requiring a root-owned payload source would close the
second of those and not the first, unless the whole tree — `deploy.py`
included — were relocated, which is the workflow being rejected.

**The trust boundary is the installed artifact, not the source.** ADR-0004's
claim is about what ends up on the node: "none of it writable by the account
being monitored." That claim is about the prefix and the system-wide shell rc,
and it holds — the payload is root-owned from creation, verified after the
copy, verified again after `install.sh` runs, and `install.sh` itself refuses
to run an approved install from anywhere but the deployed copy (ADR-0005,
`require_deployed_copy()` and `require_root_owned_payload()`).

**The operator is trusted to have reviewed what they cloned.** That is the
premise of handing a root command to a sysadmin at all, and it is stated in
ADR-0004. A guard that assumed otherwise would have to distrust `deploy.py`
too, at which point there is nothing left to run.

## Re-measure when

n/a: structural. The race is between the operator and the owner of the
checkout the operator chose to run from, and no site fact changes who those
two are.

## Site config touched

- `[install].prefix`
- `[install].staging_parent`

**This is not to be "fixed" by a later agent.** The question is settled here
because it kept coming back. If it is revisited, revisit it with a new ADR
that names the clause above it narrows, and with a measurement that
contradicts this record — not with an implementation.
