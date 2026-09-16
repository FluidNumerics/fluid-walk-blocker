# ADR-0012: The audit trail is root-writable-only, and world-readable

**Status:** accepted, 2026-09-16
**Narrows:** ADR-0004, "none of it writable by the account being monitored"
**Evidence:** held privately by Fluid Numerics, keyed ADR-0012 — see `docs/evidence.md`

## Context

ADR-0004 made the deployed artifacts root-owned, and stated the property they
exist to have: the shims, the payload, the audit trail under
`[install].spool_dir`, and the reaper as a root-run `systemd` timer — **none of
it writable by the account being monitored**.

Writable. The word is load-bearing and it is the only property that ADR records
about the trail. Nothing in this tree had ever decided whether the trail is
*readable*, and for months it was not.

The installer asserted `install -d -m 0750` on the audit directory, with a
comment, from the ADR-0004 change onward. `install -d -m` chmods an
**existing** directory, so every deploy re-asserted it. The claim spread: the
deployer's docstring said the audit directory and the unit directory were
"root-only by design", and the shim generator, the plan document and a test
docstring each repeated the mode as the reason for something.

Three other places in the same tree assumed the opposite, and all three were
false on the node:

- the reaper chmods the audit **file** `0644`, deliberately. Dead code as
  deployed: the directory denied traversal, so nothing could reach the mode;
- the deployer's own post-install message tells the operator to `tail` the
  trail, qualifying only the *writing* as root;
- the README's checklist ends with "read **both** audit trails for several
  days" — the evidence base for the `--kill` decision the whole project is
  building toward.

Observed at the reference deployment: the audit directory was `0750`
`root:root`, and the maintainer — the account that has to make the `--kill`
call — could not read it. Both documented read commands failed. The `--kill`
promotion was blocked on an access-control fact that nothing in the repository
recorded.

The mode was not drift. It was this tree's own output, asserted on every
deploy, for a reason that was never the one written down.

## Decision

**`[install].spool_dir` is `0755`, root-owned: writable by root alone, readable
by everyone.** The mode is asserted by the deployer before `install.sh` runs,
and re-asserted by the reconcile on every poll, which records a
`mode-corrected` line to journald under the `walk-blocker` tag when it had to.

ADR-0004's invariant is untouched. A monitored account still cannot write the
directory or the files in it, which is why Layer 1's escape-hatch records still
go to journald rather than appending to a file they cannot create. The
historical bug the installer's comment was written against was *loosening* the
mode so the shim's unprivileged append would work; an append needs `w`, and
`0755` refuses it exactly as `0750` did. What changes is `other` gaining `r-x`.

**This narrows ADR-0004's clause.** "None of it writable by the account being
monitored" is kept in full; "and therefore unreadable" was never part of it and
is now explicitly not part of it.

The mode is a refusal ground in **one direction only**, and this is the
asymmetry the deployer's own docstring used to deny:

- **Too restrictive** — `0750`, the state this decision corrects — is *not* a
  blocker. `install -d -m 0755` asserts the right mode and the reconcile
  re-asserts it, so finding it wrong is finding the thing about to be fixed.
- **Too permissive** — group- or other-writable, or setuid/setgid — *is* a
  blocker, because that is ADR-0004's property failing, and a `0777` directory
  plainly fails it.

ACLs are never read or written. On a directory carrying one, the group bits are
the mask; `0750` and `0755` both leave that mask at `r-x`, so a named grant
keeps exactly the access it has and only `other` changes. A named grant is
somebody's deliberate decision and not this installer's to revoke.

## Consequences

- The trail is readable by every account on the node. Its records carry other
  users' `cmdline`, uid and paths — all of which are already world-readable via
  `/proc` on a node that mounts it without `hidepid`. The change publishes
  nothing that was not already visible.
- The deployer now creates the directory explicitly, rather than leaving it to
  whichever of `install.sh` or the reaper's first write got there first. The
  mode is a recorded decision instead of an artifact of ordering and umask.
- Refusals are classified by what is actually wrong. `unowned_by()` reports
  several distinct things and they do not share a remedy — a `chown` does
  nothing for a mode, a `chmod` does nothing for a symlink leaving the tree.
  Each class gets a true sentence and a command that works, or no command where
  there is no correct one. An unrecognised reason still refuses, and says it
  could not characterise the finding rather than guessing.
- **`main` moving is not the node moving.** This decision takes effect on a
  node at its next deploy, not at merge. Until then that node's trail stays
  unreadable and every reading that depends on it stays blocked.
- The ADR-0004 boundary should be re-read, not assumed, if the reader set ever
  needs to be narrower than "everyone on the node" — that would be a different
  decision from this one, and it would need its own record.

## Re-measure when

The site mounts `/proc` with `hidepid`, or otherwise restricts cross-user
process visibility. The consequence above — that the trail publishes nothing
`/proc` does not — then no longer holds, and the reader set needs its own
record before the next deploy. Check with `mount | grep proc` for `hidepid=`,
and confirm as an unprivileged account that another user's `/proc/<pid>/cmdline`
reads. After any deploy, `stat` the spool directory and run the documented
read command as an unprivileged account; both must succeed.

## Site config touched

- `[install].spool_dir`
