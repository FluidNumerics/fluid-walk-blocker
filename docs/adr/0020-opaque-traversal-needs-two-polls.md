# ADR-0020: `opaque_traversal` names a process blocked at two consecutive polls, not one

**Status:** accepted, 2026-09-17
**Narrows:** ADR-0010, "a tool this tree has not modelled, doing something in D that might be a walk"
**Evidence:** held privately by Fluid Numerics, keyed ADR-0020 — see `docs/evidence.md`

## Context

ADR-0010 settled what `opaque_traversal` is for: keeping the corpus honest
about which tools it has not modelled, by recording an unknown tool with a
command line, not a stream filter, in D state past the traversal budget. It
excluded `tail -F` on the ground that a stream filter is not a traversal,
and it was explicit that the arm "means 'a tool this tree has not modelled,
doing something in D that might be a walk', and nothing weaker".

A predecessor's report-only trail, read after v0.1.0 landed and covering
several days at a reference deployment, showed what "might be a walk"
admits. The arm recorded two orders of magnitude more distinct processes
than the known-tool arms did over the same period. Almost all were
long-lived shells, interpreters and daemons — interactive and cron-launched
scripts, Python and JavaScript runtimes, a monitoring agent, and in a few
cases init and the journal daemon. Each was in D at the instant of one
poll, on a node whose expensive filesystem blocks a plain `stat()` from
time to time, and each had an age far past the budget simply by being old.
None was a traversal. The three real traversals in the same trail all came
from the known-tool arms. Two things make the arm behave this way:
`past_budget` is age since process start, which a weeks-old daemon
satisfies trivially, and one D-state observation is a snapshot. A blocked
`stat()` clears before the next poll; a walk does not.

Three ways of narrowing the arm were weighed.

- **A list of interpreter and shell basenames** recorded under a distinct
  verdict, so the actionable signal is separable in the trail. Rejected as
  the weakest: it is a list a site maintains, it hides a real `python
  os.walk` offender behind the name of its interpreter, and ADR-0010's
  own consequence warns that an exclusion list is where defects hide.
- **Measure the budget from the first D observation** rather than from
  process start, for this arm only. Rejected because it needs the same
  per-process memory as persistence and adds a second meaning of "budget"
  — a walker blocked for less than the budget would be unrecorded for as
  long as a daemon blocked for the same time, which is not the distinction
  wanted.
- **Persistence**: require D at two consecutive polls. Chosen. It is the
  smallest change that turns a snapshot into evidence, it needs one
  integer per live process in the state file that already carries per-poll
  samples for PSI, and it keys on the behaviour that separates the two
  populations rather than on a name.

`stream_filters` is not the fix: it is defined as tools that cannot walk a
tree under any argv, and `bash`, `python` and `node` can.

## Decision

The `opaque_traversal` arm gains one conjunct: the process has been
observed in D at this poll **and the poll before it**. `OPAQUE_D_POLLS` is
two, a constant and not a site key: two is the smallest count that means
"persisted across a poll interval", and the interval is already the site's
`[timer].on_calendar`. A site that needs a longer streak has a
signal-to-noise problem a new record should name.

`run()` keeps the streak in the state file, `d_streak`, keyed on
`Proc.key` (pid and kernel starttime) so a reused pid cannot inherit one:
a process seen in D extends last poll's count by one; a process seen in
any other state, or not seen at all, drops out. A streak, not a total — a
process that clears and blocks again counts from one. The blind returns
leave the previous poll's streaks in place, since a poll that saw nothing
is not evidence that anything cleared. The record carries the count as
`d_polls`.

`classify()` takes the streak as an argument. A caller that classifies one
snapshot and passes none gets no `opaque_traversal` finding at all: one
observation is not evidence, and pretending otherwise for the caller's
convenience would put the snapshot semantics back in by a side door.

Nothing else moves. The known-tool arms do not consult the streak — a
`find` on an expensive root past budget is a walk however it is caught —
and neither does anything killable. `opaque_traversal` stays in
`NEVER_KILL`; this is a change to the trail's signal-to-noise, not to any
kill decision. The other conjuncts, including `past_budget` from process
start, are unchanged.

## Consequences

**The trail's `opaque_traversal` rows mean what ADR-0010 said they mean.**
A process named by this arm has been blocked across at least one full
poll interval, which is what "might be a walk" needed to say and did not.

**A flickering unknown tool is not recorded.** A process in D at every
other poll never reaches two consecutive, and leaves no row. That is the
intended reading: a walk on a slow filesystem is in D at most instants, a
daemon that blocks briefly is not, and the test fixture that models a
first weekend's trail says so.

**A real unknown walker is named one poll later than before**, and never
if it finishes inside one interval. Accepted: the arm exists to grow the
tool table, and a walk that ends within one interval is not the incident
this repo was written for. The known-tool arms, which are what `--kill`
would act on, are unaffected.

**One more per-process key in the state file**, pruned when the process
leaves D or dies, so the file does not grow a key per process that ever
blocked.

**Tests that classify a snapshot pass the streak explicitly.** Every test
about another term of the arm — the kernel-thread guard, the stream-filter
exclusion, the argv-not-comm keying — says so in one argument, and a
reader can see which term it is about.

**The interim advice to read the trail with `verdict != "opaque_traversal"`
first is withdrawn.** The arm is now selective enough to read as recorded.

## Re-measure when

After the first week of `--report` at a new deployment, count distinct
`(verdict, pid, starttime)` under `opaque_traversal` against the
known-tool verdicts, and read the command lines. If the rows are still
dominated by shells and interpreters that were not walking, the poll
interval is short relative to how long a `stat()` blocks at that site, and
the answer is a record that makes the streak length a site key — not a
longer constant here, and not a name list. If a real unknown walker was
missed because it finished inside one interval, that is the accepted cost
above, and the answer is a profile for that tool in the rule table.

## Site config touched

- `[timer].on_calendar` — the poll interval is what "two consecutive polls"
  measures in.
- `[reaper].traversal_budget_s` — unchanged, still a conjunct.
