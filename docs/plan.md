# The build

One page on the architecture as built. The decisions behind each part are
in `docs/adr/`, cited by number; this page is the map, not the argument. It
carries no site facts (ADR-0014).

## One rule table, two consumers

`src/walk_blocker/search_rules.py` is the single source of truth for what an
unbounded walk looks like: per-tool argv profiles — which flags bound a
walk by depth, which keep it on one filesystem, which only prune output —
the transparent-wrapper list the parent chain is walked through, the
subtree test, and `classify_mount()`, the function that decides a mount's
class from `/proc/mounts` fields and a `Policy`. Nothing in it is a site
fact; every threshold and override arrives in the `Policy` the build
compiles from `site.toml`.

Two consumers read it, and they must agree:

- **Layer 1, the shim**, cannot import Python. `walk-blocker build`
  generates `shim/guard.sh` from the table and `site.toml`, and a test
  drives the table and the generated shim against one shared matrix, row by
  row. A shim that blocks what the reaper does not report, or the reverse,
  is worse than either layer alone (ADR-0015).
- **Layer 2, the reaper**, imports the module directly. It is copied
  verbatim into the payload beside `reaper.py` and obeys the node's
  constraint: stdlib-only Python 3.9, no package import.

A disagreement between the two consumers outranks whatever parsing question
sits underneath it.

## Two layers, and only one enforces

**Layer 1** is a PATH shim (ADR-0001). A shell hook puts `<prefix>/bin`
first on `PATH`; each wrapped name there is a symlink to `guard.sh`, which
reads its argv, its cwd and `/proc/mounts`, and either `exec`s the real
tool or refuses with exit 2 and a message naming the alternative. Before it
decides, it forks nothing and opens one file; no `statfs`, no network, no
configuration read, so it cannot hang when the filesystem does (ADR-0015).
It is advisory: an absolute path, a private `PATH`, a container, a batch
script, a second-level shell or a shell function all go around it, and the
docstrings say so. Every refusal is journaled, and `WALK_BLOCKER_UNSCOPED=1`
is its documented, journaled escape hatch, because a block with no override gets routed around and then
nothing is measurable.

**Layer 2** is the reaper, a root-run systemd timer (ADR-0001, ADR-0002). It
does not care how a process reached the node, which is why it cannot be
evaded by changing how the process gets there. Each poll reads
`io.pressure`, `cpu.pressure` and `cpu.stat` at every `user-*.slice` —
never per scope (ADR-0003) — differences them against the previous poll
(totals are cumulative since boot), then walks `/proc` in full and
classifies every process. PSI corroborates a finding and never gates one
(ADR-0009): the thresholds select which findings carry
`stalling_slice: true`, and a metadata walk of a parallel filesystem
accrues almost none. Stdin is read from `/proc/<pid>/fd/0` by device
number, never assumed a terminal (ADR-0011).

## The generated shim

`guard.sh` is a whole-file template, `node/shim/guard.sh.in`, with the rule
table's profiles and the site's mount policy rendered into it; the wrapped
names are rendered into `shim/wrapped_names.sh` beside it. The rendering is
what lets the grammar be written once, in Python, and tested in Python,
while the thing on the hot path of every `grep` on the node is `sh`
(ADR-0015). The depth ceiling is applied before any mount lookup — a bound
within the global ceiling is allowed on every mount — and only a looser
bound falls through to the per-mount judgement (ADR-0007).

## The compiled site config

Every fact about a site lives in `site.toml`, validated against
`schema/site.schema.json`, and is compiled into the node artifacts by
`walk-blocker build` (ADR-0013). Whole-file templates carry the shim;
marker lines — `# GENERATED from site.toml:<key>` or `# GENERATED from
VERSION` — carry stamped constants in `reaper.py`, `deploy.py`,
`shim/install.sh` and `walk-job`. No code that runs on the node opens a
configuration file, so there is no parse failure to handle and no choice
between failing open and failing closed. The payload carries `site.toml`
and `site.lock.json` as a record, not an input; two builds of the same
inputs are byte-identical, `--check` is the oracle for staleness, and
`examples/payload/` is a build product never edited by hand. A site value
change is a rebuild and a redeploy: a diff someone read.

## Three tiers of mount policy

A mount's class is decided in three tiers, and only the first two run on
the node (ADR-0016):

1. **A compiled default from `/proc/mounts` fields alone.** A type on
   `[filesystems].remote_fstypes`, or — with `remote_proxy` — a `host:`
   source or a `_netdev`/`addr=` option, makes a mount expensive. A remote
   mount of a type no list anticipated is guarded; a local mount of an
   unknown type is cheap. The default fails toward refusal, and the refusal
   names `walk-job`.
2. **Per-mount overrides in `[[filesystems.mounts]]`** — `path`, `class`,
   and an optional `maxdepth` bounded by `depth_allowance_max`. Compiled,
   root-owned, and the only way to loosen. Narrowing the type list or
   turning the proxy off loosens every mount at once and is never the fix
   for one export.
3. **`walk-blocker survey`**, run by an administrator out of band. It takes
   a timeout-bounded `statvfs` per mount in a child process, prints what it
   found, proposes an override block, and never writes configuration. The
   reconcile logs one journal line per poll for every expensive-by-default
   mount no override covers, so a default is visible without anyone running
   the survey.

There is no index of the large mount, by this tree or by a vendor, and the
per-mount depth ceiling is data beside the threshold it overrides, not a
branch in the logic (ADR-0007).

## The reaper's verdicts

`classify()` produces one of six verdicts per finding:

| Verdict | Claim | In `NEVER_KILL` |
|---|---|---|
| `runaway_traversal` | a known tool, a root on an expensive mount, past budget, parent alive | no |
| `orphan_traversal` | a known tool, a root on an expensive mount, reparented to init — the founding incident's shape, whatever its age | no |
| `fanout_traversal` | `[reaper].fanout_n` or more concurrent traversals under one effective parent — the parallelised walk | no |
| `orphan_idle` | an orphaned known tool with no expensive root and almost no CPU: a leak, not a load | yes |
| `opaque_traversal` | a tool this tree has not modelled, in D state past budget, with a command line, not a stream filter | yes |
| `unparsed_traversal` | the reaper could not parse the argv | yes |

`NEVER_KILL` is the partition between what `--kill` may act on and what it
may not. `opaque_traversal` is there because it names an unmodelled tool,
not evidence of a walk (ADR-0010); `unparsed_traversal` because killing on
it would be acting on the reaper's own failure. A finding's `origin` — read
from its cgroup leaf and mapped through `[reaper].origins` — is descriptive
and never an input to `classify()` (ADR-0003). The unit's exit status
follows the partition: 0 quiet or unactionable, 1 a new actionable
finding, 2 blind (ADR-0009). Under `--kill`: `SIGTERM`, `kill_grace_s`,
`SIGKILL`, settle, re-check, and `signalled_but_wedged` when the process is
still there — never a claimed kill that did not land.

## `walk-job`

Every refusal names it. It submits the traversal to `[slurm].partition`
under `[slurm].qos` with a wall-clock limit the scheduler enforces, always
passing `--mem` because a partition with an unlimited default charges the
whole node to a job that names none (ADR-0007). The bound moves from depth
to wall clock, and unlike `-maxdepth` it is not advisory. It moves load off
the login node, not off the filesystem; every place it is documented says
so.

## The deployer's guarantees

`deploy.py` is argumentless (ADR-0005): every location it writes as root is
a literal compiled from `[install]` and `[hooks.*]`, `argparse` rejects
`--prefix` outright, and the shape rules that used to validate flags live
in the schema and one test over the compiled literals. What remains, and
must, is filesystem-state validation — trust chain, traversability,
ownership, symlinked hook file, payload marker — because a literal path is
not a trusted path.

It installs root-owned only (ADR-0004): the prefix, the shims, the hook
blocks, the units and the spool are none of them writable by any monitored
account, and "root-owned" is asserted after the copy, not inferred from it.
The source tree stays user-owned (ADR-0006): the operator copies the
payload into a directory they own; `stage_payload()` snapshots it once,
between the argument checks and the first `systemctl`, and the install
copies from the snapshot. The audit directory is `0755` (ADR-0012):
root-writable, world-readable, so the person who has to make the `--kill`
decision can read the evidence. Hooks are verified, not assumed (ADR-0008):
a `required` shell's hook must be proven to fire under remote-command
conditions or the install fails; a `best-effort` hook never gates; the
reconcile reports and never repairs.

## The IP gate

This tree is generic and Fluid Numerics' own; the sites it is deployed at
are customers, and their operational facts are theirs (ADR-0014). ADRs carry
decisions, the class of measurement, and the re-measure procedure — never
the number. `docs/evidence.md` says what is held privately, keyed by ADR.
`tools/check_no_site_literals.py` fails CI on structural patterns —
identifier prefixes, path shapes, figure-with-unit shapes — and on a
customer term list that reaches CI as a secret and is never committed. The
gate is a floor under review, not a replacement for it; a false positive is
fixed by rewording, never by loosening the gate.

## The review discipline

- **The PR body is the scope boundary.** A finding it covers is fixed in the
  PR; anything else becomes an issue. A finding neither fixed nor filed is
  debt the review created.
- **The architect on demand.** `.claude/agents/architect.md` rules on scope,
  refutes settled proposals by citation, and writes the prompt for whoever
  does the work. It does not write production code. The settled table it
  carries exists because automated review keeps rediscovering the same
  closed questions.
- **Oracles for every fix.** A change to the rule table is checked by the
  shared matrix against the shim; a change to a template by `build
  --check`; a change to a stamped constant by the `CONSUMERS` table, which
  fails the build on a marker that is missing rather than stale; a change
  to the shim's cost by `shim/measure.sh` against the site's recorded
  budgets; a change to an ADR by `tests/test_adr_conventions.py`; a change
  to any prose by the IP gate. A fix without an oracle is a fix that can
  regress silently.
- **Reversing a decision is a new ADR** that names the clause it narrows,
  never an edit to the old one and never an implementation.
