# ADR-0009: PSI corroborates a finding; it does not decide what is looked at

**Status:** accepted, 2026-09-16
**Narrows:** ADR-0002, "it gates scanning, not reporting"
**Evidence:** held privately by Fluid Numerics, keyed ADR-0009 — see `docs/evidence.md`

## Context

One clause of ADR-0002's third consequence bullet — *"it gates scanning, not
reporting"*, inside the bullet about setting the threshold from the rate — is
the claim that does not survive. Everything else in ADR-0002 stands, including
the rest of that bullet: the CPU-and-memory arbiters are still out of range,
attributing cost to a user through their cgroup is still the right instinct,
and the reasoning for a low threshold over a high one is still correct and now
does double duty as the reason a threshold cannot be the detector at all.

The reaper classified only processes belonging to a `user-*.slice` whose
differenced PSI crossed `[reaper].io_stall_fraction` or
`[reaper].cpu_stall_fraction`. Measured at a reference deployment, differencing
`io.pressure` `full` totals over a fixed window across every user slice:

- a deep depth-bounded `find` of the expensive mount, in D state, with a large
  accumulated CPU time — the closest thing on the node to the originating
  incident, and a command the rule table refuses — read **effectively zero**
  I/O pressure, and had been running for days without ever being looked at;
- D-state `grep`s over files on the expensive filesystem read exactly zero;
- a `tail -F` on a home there read nothing;
- a byte-reading unbounded recursive `ugrep` of `/` sustained a large fraction
  and was scanned every poll.

ADR-0002 recorded this failure once already, one order of magnitude up:

> The reaper first shipped with a threshold read off the cumulative table; it
> duly ranked the offender first and then declined to scan it.

Lowering the threshold again does not reach these. ADR-0002 calibrated its
threshold against an unbounded recursive grep — a byte reader; the wedged
metadata walkers sit several orders of magnitude below that, which is not a
tuning margin. The cumulative total still ranks the right users first; the
differenced rate, which ADR-0002 rightly insists on, is silent about them.

### There is no counter that sees a metadata walk on this filesystem

Each candidate substitute was measured, not reasoned about:

| signal | result |
|---|---|
| cgroup `io.pressure` (`full`, differenced) | zero over the window for the wedged walkers |
| `delayacct_blkio_ticks` (`/proc/PID/stat`) | unavailable: `kernel.task_delayacct` is off, a node-wide sysctl on a shared node and not this tree's to flip |
| `/proc/PID/io` | `rchar`, `syscr` and `read_bytes` essentially unchanged across a walk of thousands of entries — `getdents64` and `statx` are not read syscalls, so it is structurally blind to a metadata walk |
| `cpu.pressure` (`some`) | quiet for those slices, far below any plausible threshold |
| process `cpu_s` | **does** move, and is already read |

So the answer is not a better counter. The evidence this reaper can stand on is
the evidence `classify()` already computes: a known tool, a root on an
expensive mount, past `[reaper].traversal_budget_s`, orphaned or with a live
parent.

### Neither half of the gate's cost argument survives measurement

- `scan_procs()` was **already unconditional**. The gate narrowed the
  candidate list *after* the full `/proc` walk, so ADR-0002's "once PSI says
  which slice is stalling, walk `/proc`" is not what the code did. The walk it
  was meant to avoid was paid on every poll in which any slice crossed — and
  with one byte-reading slice sustaining a high fraction, that was every poll.
- Reading every field `scan_procs()` touches, for the whole process table, is
  a fraction of a second of wall time at the reference deployment; on the
  poll interval that is a negligible duty cycle.
- Classifying the node's **entire** process table with no gate produced a
  handful of findings, not a flood: `runaway_traversal` on the `find` and
  `opaque_traversal` on the `ugrep`. The gate allowed exactly one of those. It
  was not holding back a flood; it was costing the killable one.

## Decision

**PSI stops deciding what is looked at.** `run()` scans and classifies the
whole process table every poll.

`sample_slices()` and `stalling_slices()` are unchanged — same slice-level
read, same differencing, same cumulative-since-boot discipline. Every rule
about *how* PSI is read is untouched; only what the result is used for
changes. The ranking still drives `--top` and still populates
`io_pressure_delta` on every record, and each finding now carries
`stalling_slice`, saying whether its user's slice agreed.

**`--kill` does not gain a stall precondition.** Making the threshold gate
action instead of observation is the tempting half-measure, and it fails for
the same measured reason: PSI cannot see a metadata walk on this filesystem,
so the clearest runaway on the node would become permanently unkillable — the
blindness moved down a layer rather than removed. Acting on the cgroup signal
*alone* stays forbidden, and the `/proc` step that names a specific process is
still what makes an action defensible; it does not require the cgroup signal
to be present. `stalling_slice: false` is what tells the human reviewing a
kill that PSI did not corroborate it.

## Consequences

- **`/proc` needs a blind check of its own.** It is the primary input now, and
  `scan_procs()` returning nothing cannot happen on a live node — the reaper's
  own pid is always there — so it means the scan failed. Returning quietly with
  no findings would read identically to a healthy quiet node, the clean bill of
  health this layer must never give. It mattered less while a closed PSI gate
  produced the same silence for ordinary reasons. Latched like the
  no-user-slice check, under its own key so the two cannot dedup against each
  other.
- **Kernel threads are excluded on their own merits.** More than half the
  process table is kernel threads, and a `kworker` blocked in D past budget
  satisfies every term of the `opaque_traversal` arm. They never surfaced
  because they are uid 0 and there is no `user-0.slice` to cross a threshold —
  the gate was hiding them by accident, not by decision. The arm now requires
  `any(proc.argv)`: a kernel thread has a zero-byte `/proc/<pid>/cmdline`, and
  a record naming nothing is the opposite of what the `/proc` step is for. Two
  traps worth recording: `[ -s /proc/PID/cmdline ]` is the wrong test, because
  procfs files stat as size 0 however much they contain, so it calls every
  process a kernel thread — read the **bytes**; and `ps -eo args=` renders
  kernel threads as `[kworker/...]`, non-empty, so a `ps`-based harness cannot
  see this class at all.
- A poll where nothing stalls now reports what it actually examined, instead
  of "no slice above threshold; nothing scanned" — a line that was accurate
  about what it did and misleading about what it meant.
- **The unit fails more often, and by how much is a reading, not a guess.**
  `runaway_traversal` does not test `state == "D"` or PSI — only a known tool,
  a root on an expensive mount, a live parent and the budget. That arm is
  unchanged, but it used to be reached only for a slice PSI had flagged and is
  now reached for every user. Any *ordinary* long job against the filesystem —
  a nightly transfer, a slow `grep -r` someone is waiting on — is a new
  `(verdict, pid, starttime)` key the first time it crosses budget, and the
  unit fails for it. Nothing is killed: `--report` is the default. Suppressing
  that noise before reading the trail would be re-introducing a gate on a
  guess, which is the mistake this ADR exists to undo. At the reference
  deployment the arm produced a small number of findings over several days,
  not a flood, and **was not narrowed**.
- **Exit codes: 0 quiet, 1 new actionable finding, 2 blind.** A new finding
  inside `NEVER_KILL` exits 0, because at the reference deployment
  unactionable findings outnumbered actionable ones by a wide margin, and an
  alert nobody can act on trains people to stop reading the alert. Scanning
  and reporting are unchanged — every finding still reaches the trail and is
  still marked `NEW`. This is the ADR's own distinction between suppressing a
  record and ranking one, applied to the alert rather than to the record.
  Derive the ratio by counting distinct `(verdict, pid, starttime)` keys, not
  rows: the trail re-logs a standing process every poll, so rows run several
  times findings, and `starttime` is on every record so that this is possible
  at all.
- **The alerting channel may already be saturated by other tenants.** At the
  reference deployment, `systemctl --failed` was dominated by failed session
  scopes belonging to other users, with the reaper the one non-session entry.
  Not reading the alert was the ambient condition, not a habit the reaper's
  noise created. The exit-code split is still right — adding to that pile is
  worse than not adding to it, and exit 2 for blindness is a distinction worth
  having whatever the channel looks like — but an operator told to watch
  `systemctl --failed` should be told what else is in it.
- `[reaper].io_stall_fraction` and `[reaper].cpu_stall_fraction` keep their
  meaning and lose their power to suppress. They now select which findings
  carry `stalling_slice: true`.
- **`stalling_slice` has three states, and the third is load-bearing.** `false`
  means measured and quiet; the key being **absent** means the uid had no
  differenced reading at all — no slice of its own, or a slice first seen this
  poll, which `stalling_slices()` skips because a cumulative total is history
  rather than a finding. Collapsing those two would let the field the `--kill`
  decision is read against say "no stall here" about a user nobody sampled —
  the clean bill of health this code must never give.
- Like `age_s`, `cpu_s` and `io_pressure_delta` before it, `stalling_slice` is
  written when a finding's **action** changes and not on every poll, so a
  standing finding's recorded value is the one from the poll that logged it.
  That is the existing dedup working as designed, but worth knowing before
  reading an old entry as a current measurement.

## Re-measure when

- **Before promoting to `--kill`.** Run `classify()` over the live process
  table as root (an unprivileged run leaves most `cwd` links unreadable, so
  relative-root traversals come back `unresolved` and the finding count is a
  floor, not an estimate). Then read the trail for several days and count
  distinct `(verdict, pid, starttime)` keys inside and outside `NEVER_KILL`.
  The bar is that every finding outside `NEVER_KILL` is one a human would have
  killed.
- **If a per-process I/O signal arrives** — `kernel.task_delayacct` enabled
  node-wide, or a filesystem client that accounts through PSI — repeat the
  differenced `io.pressure` table above against a known wedged metadata
  walker. The objection here is to what these counters can see on *this*
  filesystem, not to the idea of measuring cost.

## Site config touched

- `[reaper].io_stall_fraction`
- `[reaper].cpu_stall_fraction`
- `[reaper].traversal_budget_s`

**This is not to be "fixed" by a later agent.** The question is settled here
because it kept coming back. If it is revisited, revisit it with a new ADR
that names the clause above it narrows, and with a measurement that
contradicts this record — not with an implementation.
