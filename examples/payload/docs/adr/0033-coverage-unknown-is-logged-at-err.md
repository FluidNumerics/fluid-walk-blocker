# ADR-0033: A coverage change the relink cannot judge is logged at err, above real drift

**Status:** accepted, 2026-10-05
The clause on a caller-bug record's priority, in the Decision and in two Consequences bullets, was amended when issue #189 was decided: every caller-bug record is at `user.warning`, and a long-form call naming any action but `uncovered_mount` is one. The wording it replaced is recorded under "Superseded wording" below.
**Narrows:** ADR-0031, "The priority is the change record's."
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

`sg_report` in `node/shim/install.sh` logs every record of its short form
at `user.warning`. Two of those records are `coverage_change` and read
alike in a journal filtered by priority, though they say different
things. `unwrapped-N` is drift the relink has seen: a tool it linked is
not linked now. `unknown` says the linked-set memory (ADR-0031) was
missing, damaged or a link, so the relink cannot tell whether anything
drifted at all. The first is a fact about the farm; the second is the
relink saying it has lost the means to report that fact.

ADR-0031 gave a re-assertion its change record's priority, in the words
"The priority is the change record's." That holds for both records, and
it put them at the same level. Trevor ruled in issue #159 that `unknown`
ranks above `user.warning`, stays standing, and is re-asserted on the
`[timer].reassert_interval_s` cadence until the next `--system`, as now.

The alternatives, in their strongest form:

- **Leave both at warning.** A priority filter that shows one shows the
  other, so nothing is hidden. Rejected: nothing is hidden, but nothing
  is ranked either. An operator filtering for what to act on first sees
  routine drift and a blind relink as equals.
- **Raise only the change record, and keep re-assertions at warning.**
  The first line gets attention, and repetition stays quiet. Rejected: a
  re-assertion exists because the change record rotates out of the
  journal (ADR-0030); demoting it brings back the state where the one
  line at the right level is gone.
- **Raise every `coverage_change`.** Drift is drift. Rejected: it
  repeats the original problem one level up, with the two records still
  indistinguishable by priority.

## Decision

`sg_report` logs `coverage_change` with state `unknown` at `user.err`:
the change record and each re-assertion of it, both in the short form,
the only form the relink reports it in. Every other record keeps its
priority: `coverage_change` `unwrapped-N` at `user.warning`, marked or
not, `uncovered_mount` at `user.notice`, and the `hook_check`,
`audit_dir` and `relink_refused` records at `user.warning`. A caller-bug
record is never raised, whatever state the bad call named: every
caller-bug record is at `user.warning`, whatever its form, so a bug report
always shows under `journalctl -p warning`. The long form belongs to
`uncovered_mount` alone: a four- or five-argument call naming any other
action, `coverage_change` included, is a caller bug.

## Consequences

- **Ranked above real drift.** syslog's err (3) is more severe than
  warning (4). `journalctl -t walk-blocker -p warning` still shows
  `unknown`, because a priority filter includes everything more severe;
  `-p err` isolates it.
- **A re-assertion is at its change record's priority**, which is now a
  priority per state rather than one for the whole short form. The
  marker still says which line is a re-assertion, and the priority never
  does.
- **The raise is for the fact, not the name.** It lives after
  `sg_report`'s caller-bug check, so a malformed call that happens to
  name `unknown` stays a bug report at `user.warning`.
- Tests in `tests/test_install.py` pin `unknown` at err with and without
  `reasserted`, `unwrapped-1` at warning, a caller-bug call at warning in
  each form, and a long-form `coverage_change` as a caller bug at
  warning, under both shells.

## Re-measure when

n/a: structural.

## Site config touched

none

## Superseded wording

Amended when issue #189 was decided. The Decision above read, in its last
two sentences:

> A caller-bug record is never raised, whatever state the bad call named:
> it keeps its form's priority, `user.warning` in the short form and
> `user.notice` in the long. The record's grammar is unchanged, so a
> long-form call naming `coverage_change` keeps the long form's
> `user.notice`; no caller makes one, and issue #189 asks whether it
> should be a caller bug instead.

The Consequences above read:

> - **The raise is for the fact, not the name.** It lives after
>   `sg_report`'s caller-bug check, so a malformed call that happens to
>   name `unknown` stays a bug report at its form's priority.
> - Tests in `tests/test_install.py` pin `unknown` at err with and without
>   `reasserted`, `unwrapped-1` at warning, a short-form caller-bug call
>   at warning, and a long-form `unknown` at notice, under both shells.

A long-form bug report at `user.notice` was hidden from
`journalctl -p warning`, so every caller-bug record is now at warning,
and the long form is `uncovered_mount`'s alone.
