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
- **Measure blocked time from the first D observation**, in seconds, for
  this arm only. In its strongest form this is the better-shaped rule: a
  threshold in seconds is independent of the poll interval, so a
  one-minute site and a fifteen-minute site get the same semantics; it
  subsumes persistence, since blocked longer than one interval is two
  polls; and it discriminates finer than a poll. Rejected all the same,
  on three grounds. A duration in seconds is a site figure — it needs
  measuring per site and a new `[reaper]` key, which is the knob the
  Decision below declines to mint — whereas two polls inherits its
  duration from an interval the site already chose against its live
  schedule (ADR-0017). The per-process memory is not a wash: a first-D
  timestamp has to be forgotten when the process clears and blocks again,
  or it decays into "ever blocked since", and that forgetting rule *is*
  the streak — so the alternative is the streak plus a tunable, not an
  alternative to it. And at equal duration it separates the two
  populations no better: a daemon blocked for a given time and a walker
  blocked for the same time are indistinguishable under either rule.
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
A process named by this arm was blocked at two successive observations.
That is not proof it stayed blocked between them — it may have cleared
and blocked again, and a missed poll stretches the gap without the streak
noticing, since the streak counts runs, not wall-clock time — but it is
the evidence "might be a walk" needed and did not have: a tool in D at a
small fraction of instants clears the bar at roughly the square of that
fraction, while a walk on a slow filesystem is in D at most instants.

**Flicker produces fewer rows, not none.** A process in D at strictly
every other poll never reaches two consecutive and leaves no row; a
process that blocks at random will land two in a row now and then and be
named when it does. The test fixture that models a first weekend's trail
uses strict alternation and cannot generate the random case, so it shows
the reduction, not an absence.

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

**ADR-0009's re-measure procedure reads the trail, not a snapshot.** It
counts findings inside and outside `NEVER_KILL` to justify the exit-code
split; a hand-run `classify()` over the live table now sees no
`opaque_traversal` at all, so take that ratio from the trail `run()`
produced with its streaks, where the rows are.

**The first poll after the state file is absent or reset names no
`opaque_traversal` for one interval** — a fresh install, a wiped spool —
the same class of consequence ADR-0019 writes down for a new boot. It
heals on the next poll.

**The interim advice to read the trail with `verdict != "opaque_traversal"`
first is withdrawn** where it was given, which is the first deployment's
own notes rather than this tree. The arm is now selective enough to read
as recorded.

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
