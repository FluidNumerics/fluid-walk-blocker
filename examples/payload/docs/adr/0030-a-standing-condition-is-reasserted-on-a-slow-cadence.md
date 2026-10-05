# ADR-0030: The reconcile re-asserts a standing uncovered mount on a slow cadence, beside its change records

**Status:** accepted, 2026-10-05
The Decision clause naming `uncovered_mount` as the only standing condition and the Consequences clause "**`coverage_change` is deferred to issue #152.**" are narrowed by ADR-0031: coverage drift is a standing condition too, remembered as a high-water linked set, and the superseded wording is recorded under "Superseded wording" below.
**Narrows:** ADR-0019, "`report_uncovered_mounts()` in the installer's `--relink` arm reports **changes** in the set of uncovered expensive mounts, and is silent in a steady state."
**Narrows:** ADR-0019, "**A steady state is silent, and silence means what it did before**: nothing was refused, no override was used, and no mount has changed its standing since the last line."
**Evidence:** held privately by Fluid Numerics, keyed ADR-0030 — see `docs/evidence.md`

## Context

ADR-0019 made the reconcile's `uncovered_mount` report a report of changes:
`expensive` when a mount is first seen on its guarded default, `covered` or
`unmounted` when that is answered, and nothing in between. Its Decision said
the report "reports **changes** in the set of uncovered expensive mounts, and
is silent in a steady state", and its Consequences that "a steady state is
silent, and silence means what it did before". The operating guide read the
second clause into its account of an empty journal: quiet means, among other
things, that no expensive mount is running uncovered.

That reading holds only while the journal still holds the change record. It
does not hold for long everywhere. journald's retention is a property of the
node, and under its default `SplitMode=uid` every active user has a journal
file of their own and `SystemMaxFiles` caps the archived files for all of
them together, so on a busy login node it is file count, not disk, that
rotates the oldest lines away. Measured at a reference deployment, the
window the journal retained was short enough that a change record can
rotate out while the condition it names still holds. A reader of that journal sees silence, and by the
operating guide's own sentence reads it as "nothing is uncovered". That is
a parse of silence as a clean bill of health, which this tree treats as
worse than repetition (ADR-0019 itself chose repetition where the memory
cannot be written).

The alternatives, in their strongest form:

- **Ship a journald retention drop-in**, raising `SystemMaxFiles` or setting
  `MaxRetentionSec`, so the change record outlives any plausible standing
  condition. Its strongest form is that it fixes the cause rather than the
  symptom, and helps every Layer 1 record, not only this one. Declined: the
  journal is the node's, shared by every tenant, and its retention is a
  disk and exposure decision for the site (ADR-0026 already makes a longer
  history a wider exposure for the spool group). And it does not close the
  gap, it moves it: any finite retention is outlasted by a mount that stays
  uncovered longer.
- **Report only the retained window**: have the reader measure how far back
  the journal reaches, and read silence against that window, pointing at
  `uncovered-mounts.state` for the current set. Its strongest form is that
  it costs no journal lines, the state file already is the authority, and
  the operating guide already measures the oldest readable entry. Rejected
  as the fix, though the measurement stays: it tells the reader that the
  silence says nothing, not whether the condition holds, and a condition
  visible only to someone who already knows to open a file in the spool is
  not reported. Doing nothing at all was declined for the same reason.
- **Go back to per-poll**, which never goes stale. Rejected for ADR-0019's
  own reason, which still holds: an identical line every poll trains readers
  to filter the tag.
- **A new action**, say `uncovered_standing`, so a filter on the action
  tells a change from a repeat without reading a field. Rejected: the
  re-assertion is the same fact as the change record, and every existing
  query for `uncovered_mount` would silently miss it — the one record that
  matters after rotation. A field on the same action keeps one query.
- **A heartbeat**, a record emitted on the cadence whether or not anything
  is uncovered, so that silence would be provably "checked and empty".
  Rejected: it narrows ADR-0008's "healthy is silent", a separate decision
  with a separate cost, and whether the reconcile is running at all is the
  timer's and the unit journal's to show, not a Layer 1 record's.

## Decision

The reconcile re-asserts each **standing** condition on a slow cadence, on
top of ADR-0019's change records. A standing condition is one that persists
and is otherwise reported only when it changes: `uncovered_mount` with
`state: expensive`, and the coverage drift ADR-0031 remembers.

`[timer].reassert_interval_s` sets the cadence: an integer from 3600 to
604800, default 21600, compiled into `install.sh` as a literal (ADR-0013).
On a poll where the interval has elapsed since the last assertion, each
mount the memory already lists as uncovered is reported again with the same
action, state and notice priority, plus `"reasserted": true`. A mount not
yet listed gets the unmarked change record only, never both. `covered` and
`unmounted` are never re-asserted, and an empty set writes no record.

The clock is a second line of `uncovered-mounts.state`, `asserted N`, where
`N` is the integer part of `/proc/uptime` at the last full assertion, read
with the shell's `read` builtin. It is written in the same file, the same
exclusive create and the same single rename as the rest of the memory. A
missing, empty, malformed or future value is due. A fresh memory — none,
another boot's, or the one an install removes — reports the set unmarked,
as ADR-0019 has it, and starts the clock. Where no memory can be written
the report stays per poll and unmarked.

## Consequences

- **Silence has a horizon.** Over a window at least as long as the interval,
  an empty journal still means no expensive mount is standing uncovered;
  over a shorter one it means only that nothing changed. The operating
  guide says so, and ADR-0019's Consequences now say what holds.
- **The per-poll kinds are not re-asserted, because they already repeat.**
  `hook_check`, the `audit_dir` states that describe a standing condition
  (`absent`, `symlink`, `not-a-directory`, `owner-not-root`, `unreadable`,
  `moved`, `unmarked`, and a correction that failed), and `relink_refused`
  are written on every poll for as long as they hold, so any retained window
  that contains a poll contains them. Marking them would add a second copy
  of a line already there.
- **Events are not re-asserted.** `covered` and `unmounted` answer an earlier
  line once; the shim's `refused` and escape-hatch records describe a moment.
  None has a standing form to repeat.
- **`coverage_change` has a standing form, with its own memory.** ADR-0031
  keeps the linked set in `linked-names.state`, with its own `asserted`
  clock read by the same rule, and re-asserts the names still unlinked as
  one `coverage_change` marked `reasserted` on this cadence.
- **The marker is a field, never a different record.** A query for
  `uncovered_mount` keeps matching, and a record without `reasserted` is a
  change. `sg_report` accepts the marker only as the literal `reasserted`;
  anything else in that place writes `caller-bug`, as every other
  unrepresentable argument does.
- **A clock that cannot be read never reads as recent.** A memory in
  ADR-0019's format, with no clock line, re-asserts on its first poll under
  this code; so does a damaged clock, a clock from the future, and a node
  whose `/proc/uptime` cannot be read, which re-asserts every poll. The
  cost of any of these is a repeated line, never a missing one.
- **No writable memory, no marker.** The unprivileged relink and a spool
  that does not exist yet keep reporting the whole set every poll, unmarked:
  `reasserted` claims a memory of an earlier line, and there is none.
- **The cost is bounded and chosen.** One notice line per standing mount per
  interval, rounded up to the next poll; a site that surveys and covers its
  mounts pays nothing. Tests pin each case beside the ADR-0019 block in
  `tests/test_install.py`.

## Re-measure when

`journald.conf` changes on the node — `SplitMode`, `SystemMaxFiles`,
`SystemMaxUse`, `MaxRetentionSec` — or the active-user population grows,
which under `SplitMode=uid` shortens the window without any configuration
change. Read the merged configuration, `journalctl --disk-usage`, and the
oldest entry a reader can read (`docs/site-config.md` gives the commands),
and keep `[timer].reassert_interval_s` at no more than half the shortest
retained window, and no lower than 3600. Half, not the whole window: a line
written once per window rotates out about when the next one is written, so
a reader at the wrong moment would see none; half keeps at least one inside
it.

## Site config touched

- `[timer].reassert_interval_s` — the cadence.
- `[install].spool_dir` — where `uncovered-mounts.state`, and its clock,
  live.

## Superseded wording

Narrowed by ADR-0031. The Decision above read:

> A standing condition is one that persists and is otherwise reported only when it changes; today that is `uncovered_mount` with `state: expensive`, and nothing else.

The Consequences above read:

> **`coverage_change` is deferred to issue #152.** It records that coverage shrank — an event, with a count and no names — and it has no standing form until a memory of the linked set exists to say what is still missing. Issue #152 designs that memory; until it lands, this record leaves `coverage_change` as ADR-0019's sibling records are: once, on the change.

Coverage drift is a standing condition as well: a wrapped name linked
since the install and not linked now is re-asserted on this cadence, as
one `coverage_change` marked `"reasserted": true`, and a memory the relink
cannot read is re-asserted as `coverage_change` `unknown`.
