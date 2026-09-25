# ADR-0019: The reconcile reports an uncovered mount when its state changes, not on every poll

**Status:** accepted, 2026-09-17
The Consequences clause "**One root-owned file more in the spool**, world-readable like the rest of the directory (ADR-0012), so the person reading the journal can also read what the relink currently believes is uncovered without waiting for the next change." is narrowed by ADR-0025: the memory is `0640`, readable by the spool's one reader group, and the superseded wording is recorded under "Superseded wording" below.
**Narrows:** ADR-0016, "The reconcile logs one journald line per mount in the live table that no override covers"
**Evidence:** held privately by Fluid Numerics, keyed ADR-0019 — see `docs/evidence.md`

## Context

ADR-0016's third tier made a mount running on its compiled default visible
without anyone running the survey: the reconcile, which the timer runs
before every Layer 2 poll, logs one journald line per mount in the live
table that no `[[filesystems.mounts]]` override covers and that the tier-one
default guards. The plain reading of that sentence is per poll, and that is
what was built: a stateless pass over the mount table, one `uncovered_mount`
record per qualifying mount, at notice priority, every time.

Read against the first real deployment plan, the owner named the failure
this produces. A site with two such mounts gets two identical lines every
poll, indefinitely, and a line that repeats identically trains its readers
to filter the tag — which is the failure ADR-0001 describes for an advisory
guard, arriving in the journal instead. Notice priority made the repetition
bearable only by hiding it below the `-p warning` filter the operating
guide suggests, and a report whose only defence is that nobody reads it is
not a report. The operating guide's own instruction, "filter them out with
`-p warning`", was the symptom.

Two alternatives were weighed in their strongest form.

- **Keep per-poll and fix it at the site**, by writing an explicit
  `[[filesystems.mounts]]` row for every expensive mount so nothing is
  uncovered. That is good practice and the first site does it (a config
  handed to a client should be a complete worked example, not a set of
  decisions by omission), and it is not the fix: it silences the report by
  making its subject empty, so the one case the report exists for — a new
  remote mount appearing after the config was written — is still announced
  every ten minutes until someone rebuilds. The mechanism has to be right
  for the site that has not surveyed, because that site is the one the
  record was written for.
- **Report once, ever, per mount**, with no memory of state changes. Rejected
  because it cannot say when a mount stopped being uncovered — an override
  landed, or the export went away — and a journal that announces a problem
  and never its resolution leaves the reader to re-derive the current set by
  hand, which is what the report was meant to save them.

## Decision

`report_uncovered_mounts()` in the installer's `--relink` arm reports
**changes** in the set of uncovered expensive mounts, and is silent in a
steady state. Three records, all under the existing `uncovered_mount`
action, distinguished by `state`:

- `expensive` — a mount is first seen running on its guarded default;
- `covered` — a mount reported earlier is still in the table but no longer
  uncovered, because an override or a narrower default now covers it;
- `unmounted` — a mount reported earlier has left the table.

The memory is one root-owned file in `[install].spool_dir`,
`uncovered-mounts.state`: a first line naming the boot it was written under,
then one `mountpoint fstype` line per mount the last relink found
uncovered. It is rewritten atomically on every root relink. **A state from
an earlier boot is read as no state**: every uncovered mount is reported
once more, so a journal on volatile storage that forgot the earlier line
gets it back, and nothing is called `covered` or `unmounted` against a
table that changed for the reboot's own reasons. A fresh `--system` install
removes the memory, so the first poll after an install names every mount
on its default once; `--uninstall` removes it and leaves the spool, which
holds the audit trail.

**Where the memory cannot be written, the report is per poll**, as before:
the unprivileged debug relink, or a spool that does not yet exist, reports
the whole set each time rather than nothing. Degrading toward repetition is
the safe direction; degrading toward silence is not.

Nothing else moves. The set reported is the set ADR-0016 defined — a
cheap-by-default local mount is still never reported — and the priority
stays at notice.

## Consequences

**A steady state is silent, and silence means what it did before**: nothing
was refused, no override was used, and no mount has changed its standing
since the last line. The operating guide no longer tells readers to filter
the record out; there is nothing to filter.

**ADR-0016's promise is kept in its stronger form.** A new remote mount is
guarded the moment it appears (tier one) and is announced on the first poll
after it appears, once. The announcement of its resolution is new: the line
that asked for a survey gets an answer in the same journal.

**The memory is upkeep state, not a record**, and is treated as such: it
lives beside the audit trail because the spool is what the relink asserts
every poll, but it is never a source of evidence. The audit trail is. A
lost or corrupted memory costs one repeated round of `expensive` lines and
nothing else.

**One root-owned file more in the spool**, `0640` like the rest of the
directory (ADR-0025), so a member of the spool's reader group can also read
what the relink currently believes is uncovered without waiting for the
next change.

**The unprivileged relink repeats itself**, by design, and the tests pin
that: a debug relink run by hand is not a place where a suppressed report
should be mistaken for a clean table.

## Re-measure when

n/a: structural. The condition that would revisit this is a site whose
mount table churns faster than the poll — a per-job container runtime that
mounts and unmounts remote filesystems every few minutes — so that
`expensive`/`unmounted` pairs become the repetition this record removed.
Read the journal for that shape after the first week of a deployment; the
answer there is an override row for the churning mount's parent, not a
change to this mechanism.

## Site config touched

- `[install].spool_dir` — where `uncovered-mounts.state` lives.
- `[[filesystems.mounts]]` — the rows that turn an `expensive` line into a
  `covered` one.

## Superseded wording

Narrowed by ADR-0025. The Consequences above read:

> **One root-owned file more in the spool**, world-readable like the rest of the directory (ADR-0012), so the person reading the journal can also read what the relink currently believes is uncovered without waiting for the next change.

The memory is `0640`, readable by root and the spool's reader group, and by
nobody else.
