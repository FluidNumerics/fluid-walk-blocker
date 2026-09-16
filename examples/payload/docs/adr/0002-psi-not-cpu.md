# ADR-0002: Budget on per-user I/O pressure, not CPU time

**Status:** accepted, 2026-09-16
One clause of the third Consequences bullet — "it gates scanning, not reporting" — is narrowed by ADR-0009.
**Evidence:** held privately by Fluid Numerics, keyed ADR-0002 — see `docs/evidence.md`

## Context

A colleague surfaced two mature projects aimed at exactly this problem —
managing user behaviour on shared HPC login nodes via cgroups. They were
evaluated before building anything. The lead changed this layer's design even
though neither tool was adopted, which makes it the most valuable single
contribution to the investigation.

**Arbiter2** (`chpc-uofu/arbiter2`, GPL-2.0) and its successor **Arbiter3**
(`chpc-uofu/arbiter`, GPL-2.0, self-described beta) are both real, maintained
and in production at the University of Utah's CHPC. Arbiter3 is a Django
service querying a Prometheus TSDB, fed by a Go agent
(`chpc-uofu/cgroup-warden`) on each login node, which enforces by setting
`CPUQuota` and `MemoryMax` over an authenticated RPC.

### They measure CPU and memory. They do not measure I/O

Verified at source level, not inferred from documentation:

- Arbiter2's `arbiter/badness.py` defines
  `Badness.__init__(self, cpu=0.0, mem=0.0, …)`. Its config exposes
  `cpu_badness_threshold` and `mem_badness_threshold` and nothing else of the
  kind. Its own `CGROUPS.md`: *"This document will focus on just the CPU and
  memory controllers."* Grepping `cginfo.py`, `usage.py`, `pidinfo.py` and
  `triggers.py` for `blkio|io.stat|read_bytes|iowait|D.state` returns nothing.
- Arbiter3 can only query what its agent exports, and `metrics/collector.go`
  defines exactly eight metrics: `memory.usage_bytes`, `cpu.usage_seconds`,
  `proc.cpu_usage_seconds`, `proc.memory_usage_bytes`, `proc.count`,
  `proc.memory_pss_bytes`, `memory.max`, `cpu.quota`. No I/O. No PSI.

### Which means neither would have caught this

The incident process ran for days. Its CPU time over that span, averaged,
came to a small fraction of one core on a large login node — well under any
CPU badness threshold Arbiter ships with — and it held essentially no
resident memory.

It was never a CPU hog and never a memory hog. It was a metadata-I/O hog, and
that axis is absent from both tools. A guard tuned to CPU would have watched
this process run for days and reported the node healthy.

They *would* catch an unbounded recursive grep of the kind a shared login node
also carries, which burns CPU as well as I/O. That makes them a genuine
complement, not a substitute.

### Two further blockers

- Arbiter2 states it *"uses cgroups v1."* A current login node runs the
  unified v2 hierarchy (`cgroup2fs`) with no v1 hierarchy mounted. Adopting it
  means booting with `systemd.unified_cgroup_hierarchy=0`. That is a reboot of
  a production login node to install a monitor.
- Arbiter3 requires a Prometheus TSDB, a Django service with its own database,
  and a token-authenticated daemon on every login node. That is a platform
  decision with an operator, not a guardrail.

## Decision

Take the idea and drop the tooling. **Attribute cost to a user through their
cgroup rather than to a process through a scan** — that part is right, and it
is the reason Arbiter works where process-matching heuristics do not.

Budget on **`io.pressure`** from each `user-*.slice`. This is PSI: the fraction
of wall time in which tasks in that cgroup were stalled waiting on I/O, which
is precisely what a `find` hammering a network filesystem's metadata generates
and precisely what CPU time fails to capture.

Measured at a reference deployment, the file is **readable as an unprivileged
user** for every user slice on the node, and ranking slices by cumulative
`full` stall placed the known offender first, unaided, far ahead of an idle
slice read for scale. It costs one `read()` per user per poll. It needs **no
privilege, no daemon, no reboot, no Prometheus and no agent.**

The `/proc` scan is demoted to what it is actually good at: once PSI says which
slice is stalling, walk `/proc` to name which process in it to blame and
recover its command line. `cpu.pressure` and `cpu.stat` come along free and
cover the CPU-hog case Arbiter would have caught.

## Consequences

- `io.stat` is **absent** from the slices wherever `IOAccounting=no`, which is
  the systemd default. Enabling it is a root-level property change affecting
  every user. PSI does not need it — which is the point, and is what makes
  report-only mode deployable with no privilege at all.
- PSI is cumulative since boot. The reaper must difference successive polls,
  not read totals, or every finding will be a finding forever.
- **The threshold must be set from the differenced rate over a window, never
  from the cumulative ranking.** A lifetime sum ranks users by how long they
  have been logged in as much as by what they are doing now. Measured over one
  poll window at the reference deployment while an unbounded recursive grep
  was consuming a slice, that slice's differenced `full` stall and the next
  busiest slice's were separated by a wide gap with nothing in between. The
  reaper first shipped with a threshold read off the cumulative table; it duly
  ranked the offender first and then declined to scan it. The threshold now
  sits inside that gap, and it gates *scanning*, not reporting: a slice that
  crosses it costs one `/proc` walk, and a walk that finds no traversal
  reports nothing.
- A stalling slice is evidence about a *user*, not a *process*. The reaper must
  never act on the slice alone; the `/proc` step that names a specific offender
  is what makes an action defensible to the person whose work is being killed.
- This requires the unified cgroup v2 hierarchy with PSI enabled in the kernel
  (`CONFIG_PSI`, and not disabled with `psi=0` on the command line). A node
  still on the v1 hierarchy has no `user-*.slice/io.pressure` to read, and the
  reaper must fail loudly there rather than poll an empty table.
- If Arbiter ever grows an I/O metric, revisit. The objection here is to the
  instrument's range, not to its design.

## Re-measure when

On first deployment at a site, after any change to the network filesystem
client (a new client version, a different filesystem, a change in mount
options), and always before promoting the reaper from report-only to
`--kill`.

Procedure: under known load — one unbounded recursive traversal running in a
slice you control, ordinary work everywhere else — record every user slice's
`full` stall at the start and end of one poll interval and difference them.
The offending slice should stand apart from the busiest legitimate slice with
a clear gap. Set `io_stall_fraction` inside that gap, nearer the legitimate
side than the offender. Repeat with a CPU-bound traversal (a recursive grep
over a hot cache) for `cpu_stall_fraction`.

The bar: if there is no gap — if a legitimate slice sustains a differenced
stall comparable to the offender's — PSI cannot separate them at that site,
and the answer is not to tune the threshold until it pretends otherwise.

## Site config touched

- `[reaper].io_stall_fraction`
- `[reaper].cpu_stall_fraction`
