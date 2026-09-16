# ADR-0007: No namespace index; the depth ceiling is per-mount site config, and the general answer is a wall-clock-bounded job

**Status:** accepted, 2026-09-16
The clause "`[filesystems].remote_fstypes` is a filesystem-*type* test" is narrowed by ADR-0016 (mount class: remoteness default, per-mount overrides, out-of-band survey).
**Evidence:** held privately by Fluid Numerics, keyed ADR-0007 — see `docs/evidence.md`

## Context

Every command Layer 1 refuses is required to have an approved alternative with
a smaller impact on the shared login node. For one intent the shim already does
this well: a scheduler job token in argv makes it print the accounting query
and the node's own job-triage command, which is the cheap answer to the
question that caused the originating incident.

Two intents had no answer at all: a whole-home unbounded name search, and the
size question (`du -sh ~`). The first was originally described as "a user
cannot search their own home directory", which overstates it. The depth gate
runs before the mount judgement, so any bound within the ceiling passes, and
any root at or below `[filesystems].unscoped_depth` passes unbounded. The real
gap is narrower than claimed, and both halves of it are already covered by
`walk-job`. An index is therefore a *convenience upgrade over `walk-job`*, not
a gap-filler, which is why it is gated on measurement rather than assumed.

The obvious remedy is an index: build it once, on a schedule, off the login
node, and answer from the index instead of walking. The question is whether
that is achievable. Measured at a reference deployment, read-only, the mounts
on one login node differed by several orders of magnitude in inode count while
two of them shared a filesystem type. They are not one problem, and the guard
cannot tell them apart, because `[filesystems].remote_fstypes` is a
filesystem-*type* test. That is the correct call for a shim that must decide in
milliseconds from `/proc/mounts` alone, and it is why the alternative has to be
chosen per mount rather than per tool.

Both routes to an index fail at scale, and neither failure needed an experiment
to establish:

- **The filesystem vendor's catalog.** It has the right shape — rolling
  snapshots plus a change-detection service feeding an index database, so it is
  genuinely not a repeated POSIX walk. At the reference deployment it was not a
  command on the deployed build, so it was an upgrade before it was a
  configuration, and its published sizing ceiling was exceeded by the largest
  mount's inode count by roughly two orders of magnitude.
- **A home-grown crawler.** Extrapolating the local root's indexed bytes per
  entry to the largest mount's inode count gave a database larger than the
  local disk's free space, before the full-path-list sort that building a
  trigram index requires. A single-threaded walk of that namespace runs in
  weeks to months. This is not a tuning problem; parallelising the walk converts
  it from a slow crawler into the incident this tree exists to prevent. The
  crawl itself would *be* the incident.

The same inode table is the argument against a single global depth ceiling. Two
mounts of one type differing by orders of magnitude cannot share one number: a
user searching their own home wants more than two levels, and nobody can afford
deep levels of the largest namespace, because dataset trees there are shallow
and wide. That last clause was an assertion when the decision was first made;
it has since been measured (see "Re-measure when"), and it holds, in the
direction that argues for leaving the largest mount at the global ceiling.

Two alternatives to a per-mount table were tried on paper and neither survives:

- **Derive the ceiling from the mount's size.** `statfs()` would answer it
  exactly, and it would answer it *by blocking on the filesystem that wedges*.
  The shim reads `/proc/mounts` and nothing else for precisely this reason. A
  guard that hangs when the filesystem hangs is worse than no guard.
- **Key on filesystem type.** `[filesystems].remote_fstypes` already does, and
  it cannot help: it cannot separate two mounts of one type.

## Decision

**No index of the large mount in this tree, by us or by the vendor.** Not "not
yet" — not at this scale. A site may index its small mounts with its own
tooling; that is out of scope here.

**The depth ceiling is per mount.** `[[filesystems.mounts]]` entries carry a
`maxdepth` that overrides `[filesystems].maxdepth_allowed` for that mount
point, bounded above by `[filesystems].depth_allowance_max`. **Absence is
safe**: a mount that is not listed, or a node without that mount, takes the
global ceiling and behaves exactly as before. It is data beside the threshold
it overrides, not a branch in the logic — an override, not a dependency.
`[filesystems].unscoped_depth` is untouched: a deeper *bounded* walk is a
different claim from an unbounded one, and only the first is granted here.

**Everything else goes through `walk-job`**, which submits the traversal to
`[slurm].partition` under `[slurm].qos` with a wall-clock limit the scheduler
enforces. This is the general answer, and it covers all three intents — name
search, content grep, and size. The substitution is deliberate: a namespace
this large cannot be bounded by `-maxdepth` without answering a different
question than the user asked, so **the bound moves from depth to wall clock**.
Unlike `-maxdepth`, that bound is not advisory — the scheduler kills the job,
so a walk nobody is watching any more still ends.

Server-side directory accounting, where a site's filesystem has it, is the O(1)
answer to "how big is this directory" and the one `walk-job -- du` should give
way to. Enabling it is site policy, not a deployment detail; and if the figure
is published, it must carry a freshness stamp, because the counters themselves
do not say when they were last true.

## Consequences

- **`du -d N` is not a bound, so every `du` of an expensive mount is refused.**
  `du(1)`'s `-d` prints the total for a directory only if it is N or fewer
  levels below the operand — it prunes **output, not traversal**, because a
  directory's total cannot be known without walking beneath it. Verified on the
  tool: `du -d 1` on a tree whose only file sits several levels down reports
  that file's size in the depth-1 total. The per-mount allowance therefore does
  nothing for `du` at any value, and `depth_bound()` reading `-d` as a bound
  was reading it as something it is not. `-d`/`--max-depth` leave `du`'s
  `depth_flags` and stay in `value_flags` (still a flag that takes a value the
  operand scan must step over), and the refusal names the flags rather than
  claiming the tool has none. The cost, taken deliberately: a bounded-looking
  `du` of a small mount is refused too. It was never a bound; what is lost is a
  convenience that was only cheap because the mount was small, and
  `walk-job -- du -sh <path>` replaces it.
- **The size question has no cheap answer, and the docs say so.**
  `walk-job -- du -sh <path>` is a bounded walk, not a lookup, absent
  server-side accounting. The alternatives document states that plainly rather
  than implying the scheduler made it free.
- **`walk-job` moves load off the login node, not off the filesystem.** The
  same metadata operations reach the same storage cluster from a different
  client. Every place this tool is documented says so, because a guardrail
  people believe is free gets used more, not less.
- **The comparison moved; the fast path did not.** `check()` used to return at
  `if bounded(...)` before resolving any root, so the ceiling was applied
  without knowing which filesystem was involved. A per-mount ceiling cannot be
  a constant swap: the numeric comparison now lives beside `judge_root()`,
  where the mount is known. The early exit is preserved by accepting anything
  within the *global* ceiling before any mount lookup — such a bound is allowed
  on every mount, so it needs none. Only a looser bound falls through.
- **Layer 2 reports walks Layer 1 permits.** The reaper's `traversal_roots()`
  deliberately does not consult `bounded()`: a depth-bounded walk of an
  expensive mount still counts. Raising a Layer 1 ceiling widens a gap that
  already existed. That is correct, not drift. A permitted walk still stalling
  I/O past budget is exactly what a backstop is for, and `--report` is the
  default so nothing is killed. **Do not "fix" this into agreement.** The two
  layers answer different questions: Layer 1 asks whether a command is
  reasonable to start; Layer 2 asks whether a process is currently hurting
  other people.
- **The allowance is keyed on the mount, so it loosens the mount point
  itself**, not only the directories under it. The rationale for a home
  allowance is stated per-home, so this reads like an oversight; it is not.
  Reviewed and kept deliberately: *bounded and finite* is the property this
  guard cares about, and a depth-bounded walk of the whole mount point,
  measured at a reference deployment, finished in minutes, nowhere near the
  hours-long unbounded walk the guard exists to refuse. The alternative
  considered was gating the override on `depth_below >= 1`, which would have
  matched the per-home rationale exactly and given up nothing measured. It is
  pinned by a test whose name says the mount point is covered deliberately; if
  the allowance is ever narrowed, change that test in the same commit — it
  exists to make this decision visible rather than inferable.
- **A small scheduler partition is bounded by its default wall clock.** A
  low-priority QoS keeps `walk-job` submissions from preempting tenant work,
  but it does not stop one user's long walk from occupying a partition of one
  node. `[slurm].default_time` is what bounds that, and it is set low so that
  raising it is a visible, explicit choice.
- **`[slurm].default_mem` is load-bearing.** A partition configured with
  `DefMemPerNode=UNLIMITED` charges the whole node to a job that names no
  memory, so a `walk-job` submitted without one takes the partition for
  everyone. Raise `default_mem` only after a job has been killed for exceeding
  it, never pre-emptively.

## Re-measure when

Adding or raising any per-mount allowance, including adding a mount that is not
yet listed. The method, in full, belongs in `docs/site-config.md`, which is
written with the schema rather than with this record; the bar is here:

- **Pre-register the criteria** before the run — what entry count and what wall
  time at the proposed depth would be acceptable — and make them stricter for a
  larger mount, not looser.
- **Walk one root at a time**, bounded by depth, capped per root, paused between
  roots, from a `[slurm].partition` node and never from the login node, with
  the filesystem watched from elsewhere so the run can be cancelled. An
  unbounded `find` per root — the natural first draft of the script — is the
  exact incident this tree exists to prevent, run in the name of preventing it.
- **Never run depth N+1 when depth N already fails the bar.** A depth-(N+1)
  walk is a superset of a depth-N walk, so its entry count and wall time are at
  least as large by definition. If the depth the ceiling already permits
  enumerates more than the proposed allowance on the smaller mount bought, the
  question is answered without walking any deeper. Measuring the thing must not
  cause the thing.
- **Sample the shape.** A root that is trivial at the permitted depth can be
  enormous one level down; "shallow and wide" means the width sits exactly at
  the level a raise would open. A capped sample of roots, aborting on the first
  cap hit, supplies the shape of the risk.
- **Measure the content-reading tool before `find`.** The allowance is
  tool-agnostic, and `rg --max-depth` is the content-reading tool this tree's
  own refusal text recommends in place of `grep -r`. What a raise authorises is
  not merely a metadata walk but a multi-threaded read of every byte one level
  deeper, on the login node, with no wall-clock bound.

## Site config touched

- `[filesystems].remote_fstypes`
- `[filesystems].maxdepth_allowed`
- `[filesystems].unscoped_depth`
- `[filesystems].depth_allowance_max`
- `[[filesystems.mounts]]` — `path`, `class`, `maxdepth`
- `[slurm].partition`, `[slurm].qos`, `[slurm].default_time`, `[slurm].default_mem`

**This is not to be "fixed" by a later agent.** The question is settled here
because it kept coming back. If it is revisited, revisit it with a new ADR
that names the clause above it narrows, and with a measurement that
contradicts this record — not with an implementation.
