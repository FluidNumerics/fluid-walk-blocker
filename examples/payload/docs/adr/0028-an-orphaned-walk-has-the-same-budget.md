# ADR-0028: An orphaned traversal is a finding only past the traversal budget, the same budget a runaway has

**Status:** accepted, 2026-09-30
The Consequences clause "Under `--kill-others`, an orphaned walk is killed at the first poll after it passes the budget" is narrowed by ADR-0034: the flag is retired and `--kill` is the whole action, and the superseded wording is recorded under "Superseded wording" below.
**Evidence:** held privately by Fluid Numerics, keyed ADR-0028 — see `docs/evidence.md`

## Context

`classify()` has two per-process arms for a known traversal tool with a root
on an expensive mount. `runaway_traversal` requires the process to be past
`[reaper].traversal_budget_s` with its parent alive. `orphan_traversal`
required only that the process had been reparented to init: no age at all.
`docs/plan.md` recorded that as "whatever its age", the users' page told
people an orphaned walk "can be killed at the next run, however young", and
a test pinned it. Both arms are outside `NEVER_KILL`, so under
`--kill-others` an orphan was killed at the first poll that saw it.

ADR-0009 already names the evidence the reaper stands on as "a known tool, a
root on an expensive mount, past `[reaper].traversal_budget_s`, orphaned or
with a live parent" — the budget before the disjunction, not inside one side
of it. The code and the plan disagreed with that sentence, and the site
documentation written from it disagreed with the code.

A reference deployment's trail showed what the missing budget admits: a known
tool's walk, reparented to init, named a few minutes after it started. It
happened to be one that went on running for many hours. Nothing in the
record could have told that apart, at the moment it was named, from a walk
that was minutes from finishing.

The alternatives on the table:

- **Keep the orphan arm unbudgeted.** Its strongest form: the founding
  incident is the orphan shape, a walk whose reader has gone produces output
  nobody receives from its first second, and every second of budget is load
  on a shared filesystem for nothing. Rejected, because reparenting to init
  is not evidence that nobody wants the output. `nohup CMD > file &` followed
  by a logout, `setsid`, a double fork and `disown` all reparent a live,
  wanted walk to init, and so does a client that detaches on purpose. The
  founding incident ran for days; a budget of minutes costs nothing measured
  against that, and it is the one fact that separates the two cases.
- **Gate the orphan arm on its output having no reader** — for example on
  stdout being a pipe whose other end is closed. It is closer to the founding
  incident's mechanism than age is. Rejected for now: a walk writing to a
  file, or to nothing, has no pipe to test, so age would still be needed for
  those, and the two rules would disagree about which processes are safe to
  kill. It is not ruled out as a later, additional condition.
- **A separate `orphan_budget_s`.** Rejected: no measurement distinguishes
  the two cases, and a second number is a second calibration every site has
  to do. It can be split from the shared key when a measurement says the two
  should differ.

## Decision

`orphan_traversal` requires the process to be past `[reaper].traversal_budget_s`,
the same budget and the same comparison `runaway_traversal` uses. Below the
budget, a known tool walking an expensive mount is not a per-process finding
whether or not its parent is alive. Past it, reparented to init is
`orphan_traversal` and a live parent is `runaway_traversal`.

The budget does not gate fan-out. A young walk, orphaned or not, still counts
toward `fanout_traversal`'s group.

## Consequences

- Under `--kill`, an orphaned walk is killed at the first poll after it
  passes the budget, so it may run for up to the budget plus one poll
  interval. That is the price of not killing a detached walk someone wants.
- A process whose age cannot be read is not an orphan finding, as it is
  already not a runaway one. Age is read from the process table, never
  inferred (the schema's own description of the key).
  `test_an_orphan_whose_age_cannot_be_read_is_not_a_finding` pins it.
- `orphan_idle` is unchanged. It covers orphans with no expensive root, and a
  young orphan on an expensive mount does not fall through to it;
  `test_a_young_idle_orphan_on_an_expensive_mount_is_not_orphan_idle` pins
  that.
- The users' page states the budget for both reasons. The orphan case no
  longer carries a "no time limit" clause, and
  `test_the_orphan_reason_carries_the_budget` pins that the page says so.
- `test_an_orphan_traversal_must_run_past_the_budget_first` pins the arm, and
  `test_a_young_orphan_still_counts_toward_fanout` pins that the budget did
  not leak into the aggregate verdict.

## Re-measure when

n/a: structural. This record adds no number. The budget it shares is
calibrated as `[reaper].traversal_budget_s` always was.

## Site config touched

- `[reaper].traversal_budget_s`, now read by the orphan arm as well as the
  runaway arm.

## Superseded wording

Narrowed by ADR-0034. The Consequences above read:

> Under `--kill-others`, an orphaned walk is killed at the first poll after it passes the budget, so it may run for up to the budget plus one poll interval.

The flag is gone: `--kill`, which the unit carries when `[reaper].action`
is `kill`, acts on every finding outside `NEVER_KILL`, and the orphan's
window of the budget plus one poll interval is unchanged.
