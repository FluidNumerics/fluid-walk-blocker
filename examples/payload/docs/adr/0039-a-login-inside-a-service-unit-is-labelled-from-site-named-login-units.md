# ADR-0039: A login inside a service unit is labelled from site-named login units

**Status:** accepted, 2026-11-02
**Evidence:** held privately by Fluid Numerics, keyed ADR-0039 — see `docs/evidence.md`

## Context

ADR-0003 labels a finding's origin from its leaf cgroup alone, through
`[reaper].origins`. It assumes a login lands in a session scope: logind
names a seated session `session-<N>.scope` and a seatless one
`session-c<N>.scope`, and the second ssh transport ADR-0003 describes
lands under the second form.

That holds only when the transport's login registers a logind session,
through PAM's `pam_systemd` or by the daemon's own request to logind. At a
reference deployment, interactive logins over an ssh transport that
terminates the connection in its own daemon were found where it did not
happen. The whole chain stayed in the daemon's own unit cgroup: a
terminal multiplexer's server, the login shell under it, a coding agent, a
`bash -c` and the walk, all with the leaf `tailscaled.service`, the
multiplexer's parent pid 1 in `init.scope`, and no audit login uid set. That
leaf matches the generic `.service` row, so the finding read
`systemd_service`: "Layer 1 never applied, a daemon did this", about a
person who had logged in with the shim on `PATH`. A reader triaging the
trail draws the wrong conclusion about which layer was in front of the
walk, which is the question the label exists to answer.

The login shell in that chain was the multiplexer's, whose `argv[0]` starts
with `-`. A second route to the same unit comes from reading the
transport's source, not from a node: when its in-process ssh server hands
the session to neither login(1) nor `su`, it starts the session shell
itself as `<shell> -l`, with `argv[0]` the shell's path. It asks logind for
a session only when it runs as root, and goes on without one if the
request fails.

The alternatives on the table:

- **A site row for the unit, ahead of the generic one** — `{ pattern =
  '^tailscaled\.service$', label = "ssh_tailscale" }`. No engine change, and
  accurate in practice where the daemon itself never runs a traversal tool.
  Rejected as the answer, kept as the stopgap it was: it encodes a guess
  about one unit's name instead of evidence from the process, and labels
  the daemon and anything else the unit runs as a login.
- **Walk up to the unit's main pid** (`systemctl show -p MainPID`) and look
  for a login shell below it. Rejected: a fork and a D-Bus round trip per
  finding, on a poll that must not block, on a node that may be wedged. The
  unit's cgroup already bounds the walk, and the snapshot already holds
  every process's leaf.
- **An unset audit login uid with a non-system uid.** Corroboration at
  best: it is also what a daemon's own children look like, and it would be
  a new `/proc` read per process. Not used.
- **A login shell anywhere in a service unit, with no site map.** Rejected:
  it would need a label the site never chose, and would reclassify a
  service that happens to run a login shell for reasons of its own.

## Decision

`[reaper].login_units` maps a service unit's bare leaf name to an origin
label, and defaults to empty. A process whose leaf is a named unit takes
that label only when a login shell is among itself and its ancestors while
they remain in the same unit cgroup; otherwise the leaf falls through to
`[reaper].origins` unchanged, so the daemon and its own children stay
`systemd_service` or whatever the table says.

A login shell takes one of two forms. Its `argv[0]` starts with `-`, the
form login(1), sshd and `su -l` give it; or its argv is exactly a shell's
path followed by `-l` or `--login`, the form an ssh server that starts the
session shell itself can use. The second form counts only when the path's
basename is in a fixed list in the reaper (`sh`, `bash`, `dash`, `zsh`,
`ksh`, `mksh`, `fish`, `tcsh`, `csh`), so `bash -l -c <cmd>`, a shell
outside the list and another tool's `-l` fall through to the table.

The ancestry is read from the poll's own snapshot, walking `ppid` within
the unit, with a seen-set and a hop cap: no further `/proc` read and no
fork. A missing ancestor or an empty `argv` is "not found", never a guess.
A login shell above the unit's boundary, in a different cgroup, is evidence
about that cgroup and does not count.

Origin stays descriptive. The lookup lives in `classify_origin()` and is
reached only from the finding's record; `classify()` neither calls it nor
sees its answer (ADR-0003).

## Consequences

- A login unit's label must differ from the generic service label, or the
  key buys nothing, and should differ from the session-scope label the
  same transport gets when its login does register a session: the two
  shapes place the process differently for PSI (below) and possibly for
  user-slice resource limits, and a site wants to tell them apart. The
  schema does not enforce this; the example and `docs/site-config.md` say
  it.
- **PSI cannot attribute such a process, and the record does not always say
  so.** With no logind session the process is in the unit, not under a
  `user-<uid>.slice`, so no slice reading includes its pressure. The reaper
  joins a finding to a reading by uid, not by cgroup. When the uid has no
  slice of its own, or one first seen this poll, `stalling_slice` is absent
  — ADR-0009's third state, and the honest answer. When the uid also has a
  slice from another session, the record carries that slice's
  `stalling_slice` and `io_pressure_delta`, which describe a cgroup this
  process is not in. This record does not change that join; whether a
  finding outside its user's slice should carry that slice's reading is a
  separate question.
- The lookup trusts a snapshot taken across a scan, not an instant, so a
  `ppid` link can name a process that has exited or a pid reused since; a
  missing link reads as no login, and a reused pid outside the unit ends the
  walk at the boundary. Both err toward the table's answer.
- A process's argv is set by whatever started it, so either form is
  evidence of a login's shape, not proof of one: a daemon child started
  with a leading `-`, or as a bare `bash -l` reading its standard input,
  reads as a login. The label is descriptive and never reaches
  `classify()`, so the cost is a mislabel, never a verdict.
- Tests pin the reported tree, and a session shell run as `<shell> -l` or
  `--login` with no multiplexer, to the unit's label. They pin a daemon
  child with no login shell, a chain with a missing link, `bash -l -c`, a
  non-shell's `-l` and a login shell above the unit's boundary to
  `systemd_service`, and the hop cap on a `ppid` cycle. The verdict is
  pinned identical across four shapes, and every other record field across
  the three whose chain inside the unit is unbroken (`tests/test_reaper.py`).

## Re-measure when

Whenever a finding in the trail reads `systemd_service` and its owner is a
person rather than a service account, and whenever a site adds or replaces
an ssh transport. Procedure: for one such process, read its leaf and its
ancestors' leaves and argv from `/proc`, and whether its uid has a
session in `loginctl`. A unit that carries login shells with no session is
a `login_units` entry; a transport whose logins land in a session scope is
an `origins` row, as ADR-0003 says. The bar is that no login reads as a
daemon in the trail.

## Site config touched

- `[reaper].login_units`
- `[reaper].origins`
