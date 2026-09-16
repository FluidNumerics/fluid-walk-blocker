# ADR-0003: Read PSI at the user slice, never per-scope; a finding's origin is descriptive

**Status:** accepted, 2026-09-16
**Evidence:** held privately by Fluid Numerics, keyed ADR-0003 — see `docs/evidence.md`

## Context

Both layers encode assumptions about how a remote command arrives on a node,
and neither originally named the transport. ADR-0001 argues the whole design
around the shape `ssh host 'find …'` without ever asking what is serving that
`ssh`.

More than one transport may. At the reference deployment, checked read-only,
an OpenSSH `sshd` was listening and so was a second ssh transport that
registers seatless sessions — one that terminates the connection in its own
daemon and incubates the session shell as that daemon's child rather than
`sshd`'s. Both were in live use, in comparable numbers. logind names a seated
session `session-<N>.scope` and a session registered without a seat
`session-c<N>.scope`; the `c` prefix is what the second transport's sessions
land under. Container scopes and the per-user manager, `user@<uid>.service`,
also live under the user slice. **No process sits directly in a user slice** —
everything is inside a scope or the manager service.

Two things depend on that layout. What the shell hook (Layer 1) depends on is
the subject of ADR-0008. What the reaper (Layer 2) depends on is this record,
along with a second gap the same investigation exposed: findings named a user
and a process but not the way in. A traversal from a login shell is one Layer 1
was in front of and did not stop; one from a scheduler step or a container is
one Layer 1 never applied to. Those were indistinguishable in the audit log,
and telling them apart is most of what reading it for a few days is meant to
answer.

The alternative on the table was to sample per scope, so that a finding could
carry its transport natively and a busy scope could be told from an idle one
in the same slice. It was rejected: it splits one user's activity across two
naming schemes for no gain, and every new transport or container runtime adds
a third.

## Decision

Sample at `user-<uid>.slice`, which aggregates every child scope. Never narrow
the PSI read to per-scope granularity. Verified at the reference deployment
that `io.pressure`, `cpu.pressure` and `cpu.stat` are present and readable at
that level for another user's slice, unprivileged, as ADR-0002 requires.

`read_proc()` also reads `/proc/<pid>/cgroup` and keeps the leaf name.
`classify_origin()` maps that leaf through `[reaper].origins`, an ordered list
of `{pattern, label}` entries: the first pattern that matches supplies the
label; no match is `other`; an unreadable cgroup file is `unknown`. This is
coarse on purpose — the full path carries another user's session id, and the
audit log has to stay a document that can be shown to the person whose process
is in it.

**Origin is descriptive and is never an input to `classify()`.** A process is
not more or less of a runaway because of how its owner logged in, and wiring
the transport into the kill decision would make it so.

`effective_parent()` does not treat the session shell as a generic wrapper.
`bash` is not in `GENERIC_WRAPPERS`, so a remote command attributes to its
session shell under either transport, never to the transport daemon that
spawned the shell.

## Consequences

- Layer 2 is transport-agnostic. At the reference deployment this was true by
  accident — `sample_slices()` happened to read at the slice — and this record
  makes it deliberate. Any refactor that reads a scope's pressure file is a
  regression, not a refinement.
- If a future change wants finer attribution than the slice, the `/proc` step
  already provides it and is the defensible place for it — a stalling slice is
  evidence about a user, a named process is evidence about a process (ADR-0002).
- A test asserts that the verdict from `classify()` is identical across every
  origin label, `other` and `unknown` included. Origin appears in the finding
  and nowhere upstream of it.
- A test pins `bash` out of `GENERIC_WRAPPERS`, because adding it is a
  plausible edit that would silently reattribute every remote command on the
  node to the transport daemon.
- `[reaper].origins` is site configuration rather than a literal because
  logind's two session forms are the only universal ones. Which container
  runtimes, scheduler adapters or transports produce which leaves is a fact
  about a site, and the labels a site wants to see in its trail are its own.
- The scope-name census that showed both transports in live use was a
  one-time measurement and is not something the reaper repeats. It reads at
  the slice and does not need to know.

## Re-measure when

Whenever a leaf shows up in the audit trail labelled `other`, and whenever a
site adds or replaces an ssh transport, container runtime or scheduler
adapter. Procedure: collect the distinct `other` leaves from the trail over a
period of ordinary use, identify what produced each one, and add a
`{pattern, label}` entry for any class worth telling apart in the log. The bar
is that `other` should be rare enough that each occurrence is worth a look;
when it is not, the origin table is behind the site.

## Site config touched

- `[reaper].origins`
