# ADR-0031: Coverage drift is a standing condition, remembered as a high-water linked set

**Status:** accepted, 2026-10-05
**Narrows:** ADR-0030, "**`coverage_change` is deferred to issue #152.** It records that coverage shrank — an event, with a count and no names — and it has no standing form until a memory of the linked set exists to say what is still missing. Issue #152 designs that memory; until it lands, this record leaves `coverage_change` as ADR-0019's sibling records are: once, on the change."
**Narrows:** ADR-0030, "A standing condition is one that persists and is otherwise reported only when it changes; today that is `uncovered_mount` with `state: expensive`, and nothing else."
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

`link_farm()` links a wrapped name only where the tool resolves on the
node, and unlinks it when the tool goes. The unlink writes one
`coverage_change` record, `unwrapped-N`, on the poll the link goes, and
nothing after it. That is the right shape for an event and the wrong one
for what follows it: the tool stays unwrapped, a Layer 1 the operator
believes covers it does not, and the one line that said so rotates out of
the journal on the schedule ADR-0030 describes. ADR-0030 made a standing
uncovered mount survive that rotation and deferred this record, in its
own words, because "it has no standing form until a memory of the linked
set exists to say what is still missing". This record is that memory.

What has to be remembered is not obvious, and each of the plausible
answers fails in a way a reader cannot see from the journal.

- **The table minus what is linked.** It needs no memory at all: every
  wrapped name not linked now is "missing". Its strongest form is that it
  can never forget anything and costs no state. Rejected: the table wraps
  a dozen names and a node has a few of the tools, so the count is a
  constant that says which tools this node never had. A record that is
  always non-zero is a heartbeat with a number in it, and readers learn to
  filter it, which is ADR-0019's failure again.
- **The last relink's linked set.** It is the smallest memory and the
  obvious one: compare this poll with the last. Rejected: it forgets in one
  poll. The poll a tool goes, the memory stops listing it, and the next
  poll has nothing to compare against. That is the event record again,
  with a file behind it.
- **Reset at boot**, as ADR-0019 does for the mount memory. Its strongest
  form is that the mount table changes for a reboot's own reasons and the
  memory should follow it. Rejected for this memory: a tool removed before
  a reboot is still removed after it, and a reboot is exactly when a
  volatile journal loses the change record, so resetting there loses the
  drift at the moment it most needs saying. A new boot makes the
  re-assertion due instead, so the volatile journal gets the line back.
- **A record per name.** It would let a reader filter by tool and would
  carry the names into the journal. Deferred, not rejected:
  `sg_report` cannot escape a caller-shaped string, and the change record
  already carries a count with the names on stdout for the same reason.
  Changing the record's shape is a separate decision with its own review.
- **A one-shot `unknown`**, written once when the memory cannot be read.
  Its strongest form is that it is honest and costs one line. Rejected: it
  rotates out like the original change record did, leaving silence where
  the relink does not know, which is the problem this record exists to
  close.

## Decision

The relink keeps `<spool>/linked-names.state`, in the pinned spool and
written as ADR-0025 writes everything there. Line 1 is `boot <id>`, line 2
`asserted <uptime-int>`, line 3 `seeded system` or `seeded relink`, then
one wrapped table name per line. It is a high-water mark: the names the
last `--system` linked, plus every name a later relink linked. `walk-job` is
never in it. `--system` seeds it after `link_farm()` as `seeded system`,
its clock starting then; `--uninstall` removes it. A new boot never resets
it, and makes the re-assertion due.

A name is **standing** when the memory lists it, the table still wraps
it, and this poll did not link it. On a poll where ADR-0030's clock is due
— by its own `asserted` line, under the same rule and the same
`[timer].reassert_interval_s` — a non-empty standing set is reported as one
`coverage_change`, `unwrapped-N`, N the standing count, marked
`"reasserted": true`, with the names on stdout. A name unlinked on this
poll gets `link_farm()`'s change record only. A swept link, whose name the
table no longer carries, is never standing.

A memory that is absent, damaged or a link is reseeded from this poll as
`seeded relink`, and the relink writes one unmarked `coverage_change`
`unknown`. While the memory says `seeded relink`, `unknown` is re-asserted
on the cadence; only `--system` clears it. Without a pinned spool nothing
is read, written or reported.

## Consequences

- **Two meanings of N, one action.** On an unmarked `coverage_change`, N
  counts the links that went on that poll, swept ones included, as before.
  On a marked one, N counts the remembered names still unlinked. A query
  for `coverage_change` matches both, and the marker says which.
- **`unknown` is a state of the reader, not of the farm.** It says the
  relink cannot say what drifted before the poll it reseeded on. Names it
  links afterwards are remembered, so a `seeded relink` memory can also
  report standing drift beside its `unknown`.
- **A tool missing at the install is not drift.** The install's own
  linked set is the baseline, so a site that never had a tool never hears
  about it, and an install is the answer to every standing name: a site
  that accepts a tool's absence reinstalls, and the next poll is quiet.
- **No fallback.** Where the spool cannot be pinned the relink reports no
  drift at all, never the table minus the linked set. `link_farm()`'s
  change record still fires there, so an unlink is never silent.
- **A directory at the memory's name cannot be renamed over**, so every
  poll reports `unknown`, unmarked, until someone removes it: repetition,
  never silence. The relink deletes nothing it did not write.
- **`sg_report` takes the marker in its short form.** A third argument
  must be the literal `reasserted`; anything else writes `caller-bug`,
  where it used to be ignored. The priority is the change record's.
- **No heartbeat.** An empty standing set on a due poll writes nothing and
  still moves the clock (ADR-0008). Tests pin each case in
  `tests/test_install.py`, under both shells.

## Re-measure when

As ADR-0030: the journal's retention changes, or the active-user
population grows, and `[timer].reassert_interval_s` is checked against the
shortest retained window by ADR-0030's procedure. Nothing about this
memory adds a measurement of its own.

## Site config touched

- `[timer].reassert_interval_s` — the cadence, shared with ADR-0030; each
  memory keeps its own clock.
- `[install].spool_dir` — where `linked-names.state` lives.
