# ADR-0025: The audit trail is readable by a site-named group, and a listed service group may hold a spool ancestor

**Status:** accepted, 2026-09-25
**Narrows:** ADR-0012, "**`[install].spool_dir` is `0755`, root-owned: writable by root alone, readable by everyone.**"
**Narrows:** ADR-0004, "none of it writable by the account being monitored"
**Narrows:** ADR-0019, "**One root-owned file more in the spool**, world-readable like the rest of the directory (ADR-0012), so the person reading the journal can also read what the relink currently believes is uncovered without waiting for the next change."
**Evidence:** held privately by Fluid Numerics, keyed ADR-0025 — see `docs/evidence.md`

## Context

ADR-0012 made the spool `0755` and root-owned: writable by root alone,
readable by everyone. Its reasoning was that the records carry nothing
`/proc` does not already publish, and that the account deciding `--kill` has
to be able to read them. Two things about that decision did not survive
contact with a node.

**Readable by everyone was more than the reading needed.** The people who
decide `--kill` are a small set a site can name. Everyone else on a shared
login node gains nothing from reading other tenants' command lines out of a
file, and a site that restricts `/proc` gets no help from this tree in
keeping them apart. ADR-0012 anticipated this in its own Consequences: a
reader set narrower than "everyone on the node" is a different decision and
needs its own record. This is that record.

**An ancestor the tree does not own can change underneath it.** ADR-0005's
trust chain demands that every directory above a root-write path be
root-owned and writable by root alone, and ADR-0006 keeps that check on the
installed components however the payload arrived. Measured at a reference
deployment: a distribution package upgrade reset `/var/log` to
group-writable for the log daemon's group, and a node that had installed
cleanly could no longer be reinstalled — the spool's chain now failed on a
directory this installer neither owns nor should repair. The chain checks
were right to notice. What they could not do was tell a service group's
write bit from a person's.

The alternatives:

**Keep the spool world-readable, and loosen nothing.** Its strongest form is
that ADR-0012's argument still holds on a node without `hidepid`, and that a
reinstall blocked by an ancestor's mode is the operator's to fix by
restoring the mode. It was rejected on both halves: a package manager that
resets the mode will reset it again at the next upgrade, so the fix does not
hold, and the reader set was already wider than any reading required.

**Chown the spool's group on every write, from the reaper.** Its strongest
form is that it needs no setgid bit and no directory mode anyone could get
wrong. It was rejected because a `chgrp` by name puts an NSS lookup into
Layer 2 on every poll, and the reaper resolves no names by design: a
directory service that stalls would stall the backstop with it.

**Trust any group on any chain.** Its strongest form is simplicity — one
rule for every ancestor. Rejected: the prefix, staging, unit and hook chains
hold code root executes, where a group that can rename an entry can choose
what runs. The spool holds records. A narrower rule for the narrower risk is
what this record is for.

## Decision

The spool is `root:<spool_group> 02750`, and its files are `0640`. Files take
the group from the directory's setgid bit, so nothing on a poll resolves a
group name; the reaper sets each file's mode through the fd it wrote with.
`[install].spool_group` names the group and has no default. `validate`
refuses `root`: a trail only root can read is the state ADR-0012 recorded
as the incident.

`[install].trusted_groups` lists service groups whose write bit is accepted
on the spool's **strict ancestors**, and nowhere else. The prefix, staging,
unit and hook chains stay root-only, the spool itself is never loosened,
and an other-write bit is never accepted on a group's strength. The list has
no default; empty is the ordinary answer.

A listed group is refused unless the deployer proves it holds no person, at
deploy time and in the dry run. "Person" means a uid in `[UID_MIN, UID_MAX]`
from `/etc/login.defs`, and the check fails closed when either bound is
missing. A group is refused if it does not resolve; if a name in its
`gr_mem` does not resolve; if a supplementary member, or an account whose
primary gid it is, has a human uid; or if its gid is at or above
`GID_MIN`. The reader group only has to resolve: who is in it is the site's
decision.

Root's writes into the spool never follow a link. The deployer creates the
spool with `mkdir` and sets its owner, group and mode on a descriptor opened
`O_NOFOLLOW|O_DIRECTORY`; the reaper writes every file relative to the
spool's descriptor, `O_NOFOLLOW`, and renames with directory descriptors;
the relink enters the spool with `cd -P`, checks it is the inode it
examined, and writes only relative names inside it.

The spool carries its identity inside it. `deploy.py` writes
`.walk-blocker-spool`, a root-owned `0640` regular file, into the spool it
creates, and into an existing root-owned spool that predates it. Run as
root, the relink and the reaper write into a spool — or chmod or chgrp it —
only when that marker is there: a regular file root owns, reached by its
relative name inside the pinned working directory or opened `O_NOFOLLOW`
relative to the spool's descriptor. A spool without it is journalled
(`unmarked`), nothing is written, and the reaper exits `4`. Neither of them
creates the spool or the marker; both are `deploy.py`'s.

ADR-0004's clause "none of it writable by the account being monitored" is
kept in full, and this record clarifies how it reads: the account being
monitored is a human login account. A listed, human-free service group may
hold write on a spool ancestor, never on the spool.

## Consequences

- **The trail is no longer readable by the person whose process is in it,
  unless that person is in the group.** ADR-0012 could say a user might read
  the records of their own processes; that property is lost, deliberately,
  and the `audit_filename` description in the schema no longer claims it.
  The journal is unchanged: an ordinary user still reads their own Layer 1
  records there.
- **A listed group can hide or replace the trail, but it cannot redirect a
  root write.** Holding write on an ancestor lets it rename entries in that
  ancestor, and each way of using that is answered:
  - a link at the spool's name, or at any name inside it: every writer
    refuses to follow one;
  - a directory of its own at the spool's name: it is not root's, and root
    writes nothing into it;
  - a root-owned directory that already sat beside the spool, renamed into
    the spool's name: root's and a directory, it passes every test but the
    marker's. The marker stayed with the real spool, so the relink leaves
    the sibling's mode, group and contents alone and journals `unmarked`,
    and the reaper writes nothing and exits `4`.

  What remains possible is what write on the ancestor always gave: the group
  can delete or replace the trail, and it can move the real spool aside,
  which stops the writes rather than redirecting them — the relink journals
  `absent` or `unmarked`, and the reaper exits `4` with its findings in the
  unit's journal until a deploy puts the spool back.
- **`deploy.py` trusts the directory at the spool's name when it adds the
  marker to a spool that predates it.** It proves that entry is a directory
  root owns, reached without a link, and no more; a sibling renamed into the
  name at the moment an operator deploys would be marked. The dry run lists
  what it will repair, and the operator runs the deploy; the unattended
  paths never mark anything.
- **The member check claims only what NSS enumerates at deploy time.** A
  directory-service account that `getpwall()` does not list is not seen, and
  membership can change after the deploy. `GID_MIN` narrows that gap — a
  service group is allocated below it — without closing it. Every refusal
  that rests on the check says so.
- **The dry run makes the same check as an ordinary user.** NSS and
  `login.defs` are readable by anyone, so the check is made, not reported as
  unknown, and a listed group with a person in it refuses the preview with
  the install's words.
- **ADR-0013 permits these reads.** The group names are compiled; only their
  gids and the login.defs bounds are read, by `deploy.py`, at deploy and in
  the dry run. That is node state, like an `lstat`, not configuration.
  Nothing on the node chooses which groups these are, and neither the reaper
  nor the shim reads any of it.
- **Setgid, not a per-write `chgrp`.** The group reaches every file without
  the reaper naming it, which is what keeps NSS out of Layer 2's poll.
- **The migration is a repair, not a refusal.** A spool at `0755` or `0750`,
  with the wrong group, holding `0644` trail files, is what every node that
  installed before this record has; the install sets it right through
  no-follow descriptors and the relink keeps it so. A group- or
  other-writable spool, a setuid bit, or a setgid bit anywhere below the
  spool directory itself still refuses. The spool directory's own setgid
  bit is the decision, not a finding.
- **The reaper has a fifth exit code.** `4` means the spool failed its check
  or a write into it failed: the poll scanned, printed every finding to the
  unit's journal, wrote nothing and sent no signal.
- **The relink no longer creates a missing spool.** It journals `absent`;
  the `created` record is gone with the creating. A non-root hand run of
  the reaper still makes its own `/var/tmp` spool, and neither it nor the
  unprivileged debug relink asks for the marker: neither writes as root.
- **The relink's memory is `0640` like the rest of the spool.** ADR-0019
  called it world-readable; that clause is narrowed here and moves to its
  superseded wording.

## Re-measure when

A package upgrade touches any ancestor of the spool, or a listed group's
membership changes. On each ancestor, `stat -c '%U:%G %a'`; for each listed
group, `getent group <group>`; then `python3 deploy.py --system --dry-run`
as an ordinary user, which makes the member check and the chain check and
names any refusal.

## Site config touched

- `[install].spool_dir`
- `[install].spool_group`
- `[install].trusted_groups`
- `[install].audit_filename`
