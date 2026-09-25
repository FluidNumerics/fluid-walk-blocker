# Operating walk-blocker

The operator's runbook, from an empty `site.toml` to a node that reports.
It is generic: every site fact is a value in your own `site.toml`, and this
page names the key rather than the value (ADR-0014). Every command here is
checked against the tree's own help text.

Two rules frame every step below. **Layer 1 is advisory** (ADR-0001): the
shim refuses the naive command and names the alternative, and an absolute
path, a private `PATH`, a container, a batch script or a shell function all
go around it. **The reaper reports by default**: nothing is killed until a
human reads real findings and decides otherwise (ADR-0009).

## 1. Prerequisites

On the workstation where you build:

- `uv`. Every command below runs as `uv run walk-blocker ...` from a
  checkout; no virtual environment needs activating.
- A checkout of this tree, and a directory of your own — outside this tree
  — for `site.toml` and the evidence beside it.

On the node you deploy to:

- systemd, with the unified cgroup v2 hierarchy and PSI enabled in the
  kernel (`CONFIG_PSI`, not disabled with `psi=0`). The reaper reads
  `io.pressure` at each `user-*.slice`; a node on the v1 hierarchy has no
  such file and the reaper fails loudly there rather than polling an empty
  table (ADR-0002).
- Python 3.9 or later, stdlib only. Nothing under `node/` imports a
  third-party package, and there is no `uv` on the node (ADR-0015).
- A POSIX `sh` that is dash-clean. The shim, `install.sh`, `walk-job` and
  `measure.sh` all run under `[trusted_binaries].sh`.
- The Slurm client (`sbatch`) reachable at `[slurm].sbatch_glob`, for
  `walk-job`. The scheduler is what enforces the wall-clock bound the
  refusal text offers (ADR-0007).
- Root, held by the person running the install. Nothing here escalates;
  `deploy.py` checks `os.geteuid()` and refuses otherwise (ADR-0004).

## 2. Write `site.toml`

Start from `examples/site.example.toml`, a fictional site with every key
written out, and replace every value with what your site measured. The
procedure for each measurement — the shell census, the mount survey, the
depth allowance, the stall calibration, the timer slot, the shim budgets —
is `docs/site-config.md`. The schema is `schema/site.schema.json`, and
`uv run walk-blocker schema` prints its path.

The tables, in the order the schema lists them:

- `[site]` — how the node names itself in refusal text and unit
  descriptions, where a refused user is sent for the site's own
  explanation, and whom they contact. Every refusal also names the
  installed `docs/what-to-run-instead.md`, which needs no URL.
- `[filesystems]` — the mount policy in three tiers (ADR-0016): the
  remote-type list and remoteness proxy that decide the compiled default,
  the global depth ceiling and unscoped depth, and `[[filesystems.mounts]]`,
  the per-mount overrides that are the only way to loosen a default.
- `[install]` — where the payload, the audit trail and the units land on the
  node. Every value is a root-write target compiled into the installer as a
  literal (ADR-0005, ADR-0013), and all of it must be on local disk.
- `[hooks.bash]`, `[hooks.zsh]`, `[hooks.fish]` — one table per shell whose
  startup file gets the hook block, each `required` or `best-effort`
  according to the site's shell census (ADR-0008).
- `[trusted_binaries]` — absolute paths of the binaries the node artifacts
  call, so nothing on the node resolves them through a `PATH` a user
  controls.
- `[slurm]` — how `walk-job` submits a refused traversal: partition, QoS,
  the default wall clock and memory, and where `sbatch` lives.
- `[shim]` — tools in the rule table the site chooses not to wrap.
- `[reaper]` — the cgroup layout, the origin table, the traversal budget
  and fan-out count, the stall thresholds, the kill budget and the stream
  filters. Thresholds select which findings carry `stalling_slice: true`;
  they never suppress a record (ADR-0009).
- `[timer]` — the reaper's `OnCalendar=` slot and its budgets. The slot is
  chosen against the live schedule of the target node, never copied
  (ADR-0017).

Keep the measurements that justified each value beside `site.toml`, outside
this tree. Validation cannot tell a measured value from a copied one.

## 3. Validate

```sh
uv run walk-blocker validate --site site.toml
```

This runs the schema and the semantic checks — canonical paths, unique
mount entries, no per-mount `maxdepth` above `depth_allowance_max`, a
`timeout_start_sec` at or above the floor derived from the relink and kill
budgets — and prints the derived values a build would use. It exits 2 on
the first failure, naming the key. Fix the file and run it again; nothing
downstream accepts an invalid site.

## 4. Survey the mounts on the node

The survey is tier three of the mount policy (ADR-0016). It runs on the node
as an administrator, takes a timeout-bounded `statvfs` per mount in a child
process so a wedged mount is reported `unmeasured` rather than hanging the
survey, and never writes a file. It ships in the payload as `survey.py` and
needs only the node's `python3`:

```sh
python3 payload/survey.py
python3 payload/survey.py --timeout 5 --all --json
```

On the workstation, the same code runs against a saved mount table, with
`--site` marking each mount with the override that already covers it:

```sh
uv run walk-blocker survey --mounts saved-mounts.txt --site site.toml
```

It prints a table — type, remoteness and why, the compiled default,
capacity and inode count where measured — and then a proposed
`[[filesystems.mounts]]` block. Treat the block as a proposal:

- A small remote export that is cheap to walk in full: keep its entry with
  `class = "cheap"` and a comment saying why.
- A remote mount that should keep its default: delete the proposed entry. A
  mount left on its default should be left there on purpose, and the survey
  output kept beside `site.toml` is what records that.
- A mount that deserves a deeper bounded walk: add `maxdepth`, but only after
  the measurement in `docs/site-config.md` (ADR-0007). Never as a guess.

Network and parallel filesystems may report synthetic totals — a quota, a
tiered capacity, a per-client view — so the figures are evidence to weigh,
not a verdict. Re-run the survey when the reconcile reports a mount you did
not decide on (step 10).

## 5. Build the payload

```sh
uv run walk-blocker build --site site.toml --out payload/
```

The build validates `site.toml`, renders every artifact in memory, and
writes the payload in one rename, so a failed build leaves the previous
payload intact. It refuses an output directory it did not create — one
that exists, is not empty, and has no `.walk-blocker-build` marker — and
rebuilds one that carries the marker.

What the payload contains:

| Path | What it is |
|---|---|
| `.walk-blocker-build` | build marker: this directory may be rebuilt |
| `deploy.py` | the argumentless deployer, stamped from `[install]`, `[hooks.*]` and `[timer]` |
| `reaper.py` | Layer 2, stamped from `[reaper]` and `[filesystems]` |
| `search_rules.py` | the rule table, verbatim; the reaper imports it |
| `survey.py` | the mount survey, verbatim |
| `walk-job` | the sanctioned alternative, stamped from `[slurm]` and the `bfs` pin |
| `shim/guard.sh` | Layer 1, rendered from the rule table and `site.toml` |
| `shim/wrapped_names.sh` | the wrapped-name list, rendered likewise |
| `shim/install.sh` | the shell installer, stamped from `[install]`, `[hooks.*]` and the mount policy |
| `shim/measure.sh`, `shim/measure-flags.sh` | the shim's performance gate, and the flag-clustering probe for the rule table |
| `docs/` | this documentation tree, verbatim |
| `docs/what-to-run-instead.md` | the users' page, rendered from `node/docs/what-to-run-instead.md.in` and `site.toml`: the mounts, their allowances and measurements, and this site's `walk-job` |
| `README.md` | the repository README, verbatim |
| `site.toml` | the input, byte for byte, as a record |
| `site.lock.json` | schema, tool and payload versions, and a hash per file |

Nothing in the payload reads `site.toml`; it is carried as a record. Two
builds of the same inputs are byte-identical — no clock, no hostname — so
the lock file's hashes identify a build.

To check a payload without writing anything:

```sh
uv run walk-blocker build --site site.toml --out payload/ --check
```

`--check` renders afresh and compares; it exits 1 when the payload differs
from what the current `site.toml` and tree would build, and 0 when it is
current. Run it before every deploy, and run it in the site's own CI so a
stale payload is a red build rather than a stale node (ADR-0013).

## 6. Copy the payload to the node

Copy the payload directory to the node into a directory that your own
account owns, on local disk:

```sh
scp -r payload/ node:walk-blocker-payload/
```

It does not need to be root-owned, and the installer does not check that it
is. The trust boundary is the installed artifact, not the source (ADR-0006):
`deploy.py` snapshots the payload once into a root-owned staging directory
under `[install].staging_parent` before anything that can block, and copies
from the snapshot; everything under `[install].prefix` is root-owned from
creation and verified after the copy. A checkout owner who could win that
copy race could equally rewrite `deploy.py` before Python opened it, which
is the exposure the operator already accepts by running the script at all.
Automated review has proposed refusing a user-owned source three times; it
is settled.

Do not put the payload under a home directory that lives on the filesystem
under investigation. The install reads it once, but the staging snapshot is
what protects the install, not the payload's location, and reading a wedged
filesystem is the one thing that can stall the snapshot.

## 7. Dry-run the install

As any user, on the node — this needs no privilege, and running it from a
root shell is one flag away from a live install:

```sh
python3 walk-blocker-payload/deploy.py --system --dry-run
```

`--dry-run` prints what the install would do and exits without writing. It
names the prefix, the spool directory, each hook file it will write a block
to, the unit files, and the command that will actually install.

The copy commands read from the snapshot under `[install].staging_parent`,
not from the payload directory you are standing in — the install takes that
snapshot first and copies out of it (ADR-0006). Its last component is chosen
when it is created, so the preview writes it as
`walk-blocker-stage.XXXXXXXX`. **That pattern is the only part of the printed
commands a dry run cannot know**; everything else is the command that will
run. A preview naming your payload directory as the source is reading an old
build — the source there is the snapshot.

**Do not drop the flag to "see what it says".** Since ADR-0021 the gate is
root alone: `--system` on its own installs, immediately, with no further
confirmation.

**It makes every check the install makes**, from the same
`preflight()` function, so that the install cannot refuse what the
dry run accepted:

- the six root-write locations are the literals compiled from `site.toml`;
- each of them sits in a trust chain that is root-owned end to end, and
  each hook file is a plain, root-owned, not-group-writable regular file —
  not a symlink under a dotfile manager;
- `[install].spool_dir` is usable: it is not something other than a
  directory (a file, a fifo, a symlink to either, or a dangling symlink,
  which the install's `install -d` fails on outright), its ancestors can be
  traversed by an ordinary user, and nothing in it is writable beyond root;
- `[install].prefix` can be reached by the ordinary users Layer 1 exists
  for — a root-only ancestor passes every ownership check and still leaves
  every monitored account with an unreachable directory on `PATH`;
- `[install].prefix` is not already somebody else's populated directory;
- `[install].staging_parent` exists, is a directory, and sits in a trust
  chain that is root-owned end to end — the install snapshots the payload
  there, as a root-only `0700` directory, between the last of the checks
  above and its first `systemctl`, so a parent someone else can write is a
  refusal and a parent that is not there at all is one too. A `--dry-run`
  install snapshots nothing, so this is the one check it does not make — it
  names the path it would snapshot into, which is not the same as having
  found that path usable. Re-run as root to make the check before deploying.

Where one of those refuses, the dry run prints the whole plan, says which
check refused and why, and **advertises no command** — because the install
would refuse too, and it would do so after the timer had already
been disabled and stopped.

Where the dry run is run by an account that may not make one of those
stats — an ancestor with no `o+x`, a root-only file — it says
`could not be checked as this user` and names the path. That is neither
"clean" nor a refusal: the check was not made, which is not the same as
made and passed. Re-run the dry run as root to make it before installing.

There are no path flags: `argparse` rejects `--prefix` and its siblings
outright. Every location the deployer writes as root is a literal compiled
from `[install]` and `[hooks.*]` (ADR-0005, ADR-0013). A different location
is a `site.toml` change, a rebuild and a redeploy.

Read the preview in full. If it advertises a location you did not intend,
stop here and go back to step 2.

## 8. Install

As root, on the node:

```sh
python3 walk-blocker-payload/deploy.py --system --dry-run   # read this first
python3 walk-blocker-payload/deploy.py --system             # then install
```

Root is the only gate. There is no second flag to type, because the person
who holds root on the node already holds the authority this installs with,
and a script that asked them to confirm it would be relocating a judgement
away from the one party with the context to make it (ADR-0021).

The consequence is worth stating plainly: `--system` installs, immediately,
with no further confirmation. Run `--dry-run` first. It makes every check the
install makes, needs no privilege, writes nothing, and prints both the
rendered units and the exact command sequence — and where it runs as an
ordinary user who cannot make one of the checks, it says so rather than
reporting it clean. Sessions of automated agents working in this repository
never run any of this as root, in any mode (see `CLAUDE.md`).

What it writes, all root-owned and none of it writable by any monitored
account (ADR-0004):

- the payload under `[install].prefix`, with a `.walk-blocker-payload`
  marker so a later install or uninstall knows the directory is its own;
- the symlink farm under `<prefix>/bin`, one shim per wrapped name that
  resolves in `[install].tool_search_path`, plus `walk-job`;
- `[install].spool_dir` as `root:<spool_group> 02750`, its files `0640`:
  writable by root alone, readable by the one group `[install].spool_group`
  names, so the people who make the `--kill` decision can read the trail and
  nobody else can (ADR-0025). It is created with `mkdir` and set through a
  descriptor opened without following a link, never `install -d`. The relink
  keeps `uncovered-mounts.state` there too, its memory of which mounts it has
  reported (ADR-0019);
- a hook block in each enabled shell's startup file named by
  `[hooks.<shell>].file` — above the interactivity guard in the bash rc,
  since a non-interactive shell returns before reaching anything below it —
  and a dedicated `conf.d` drop-in for fish. The pre-install content of each
  hook file is saved once to `<file>.walk-blocker.orig`, the first time
  this runs;
- the reaper's service and timer under `[install].unit_dir`, enabled and
  started as `walk-blocker.timer`.

What it verifies, and refuses on:

- **the hooks fire.** Each `required` shell's hook is proven under
  remote-command conditions with the shim directory stripped from `PATH`: a
  non-interactive bash with `SSH_CLIENT` set and `SHLVL` at zero, a zsh
  with `ZDOTDIR` blanked, must resolve a wrapped name to the shim
  directory. A required shell whose binary is absent is a hard failure — an
  automatic pass on "absent" would spell "unchecked" as "verified". A
  `best-effort` shell is written and checked only when its binary resolves,
  and its failure is a warning (ADR-0008);
- **ownership.** Everything under the prefix is reasserted `root:root` with
  group and other write stripped, and the unit is not written if anything
  under the prefix still fails that test;
- **the audit directory is `root:<spool_group> 02750`**, asserted before
  `install.sh` runs. The wrong read scope — `0755`, `0750`, another group,
  `0644` trail files — is repaired; group- or other-writable, setuid, or
  setgid anywhere below the spool directory is a refusal;
- **the groups.** `[install].spool_group` must resolve. Each group in
  `[install].trusted_groups` must resolve, have a gid below `GID_MIN`, and
  have no member — in `gr_mem` or by primary gid — with a uid in
  `[UID_MIN, UID_MAX]` from `/etc/login.defs`; its write bit is then accepted
  on the spool's strict ancestors and nowhere else. The check sees what NSS
  enumerates at deploy time and no more, and the dry run makes it as an
  ordinary user (ADR-0025);
- **the hook files are plain, root-owned regular files**, not symlinks, not
  group-writable, in a trusted directory chain — they are read and
  rewritten `0644`, and sourced as root to verify the hook, so their owner
  would otherwise choose what runs during the deploy.

The reaper runs `--report`. Its unit's `ExecStartPre` runs
`install.sh --relink` — the reconcile — which re-links the farm, re-checks
every hook and reports rather than repairs, re-asserts the audit
directory's mode, and reports each mount running on its default; the
reconcile never fails, so a Layer 1 diagnosis cannot stop the Layer 2
backstop (ADR-0008).

After the install, as an unprivileged member of `[install].spool_group`,
confirm two things:

```sh
stat <spool_dir>
tail -n 1 <spool_dir>/reaper-audit.jsonl
```

Both must succeed, and the `stat` must show `root:<spool_group>` and `2750`.
If the trail is unreadable to the group, the `--kill` decision is blocked on
an access-control fact and nothing else in this runbook can be read. An
account outside the group is refused the `tail` by design (ADR-0025).

## 9. Measure the shim

The shim runs on every `grep`, `find` and `du` on the node, and its cost is
paid by every user whether or not it ever refuses anything (ADR-0015).
`measure.sh` measures two paths — the **fast path**, a `grep PATTERN` with
no `-r`, decided before any file is opened; and the **guarded path**, an
allowed traversing call that judges every operand, which is the only path
`find`, `du`, `rg`, `fd` and `tree` ever take — and gates them two ways:

- **the absolute gate**: the shim's overhead over the bare binary, on each
  path, against a budget in milliseconds that you pass. It exits non-zero
  when a budget is exceeded;
- **the ratio gate**: with `--against`, it measures a previous `guard.sh`
  and the new one alternately, in the same minute, and gates on the median
  per-pair ratio. This is the gate that survives a change of machine — an
  absolute reading moves with the node's load; a ratio between two shims
  measured together does not.

Keep both: a ratio gate alone cannot see cumulative drift, and an absolute
gate alone cannot be run anywhere but the machine it was calibrated on.

The budgets and ceilings are arguments, not values in `site.toml` and not
constants in this tree; they live in your site's own evidence beside
`site.toml`. Copy the `shim/` directory to a directory on the node's local
disk that you own — never run it from the expensive mount, because the
measurement must not depend on the thing it is measuring the cost of
avoiding — and run it from there. The candidate `guard.sh` must be
executable; a copy extracted with `git show` needs `chmod +x`.

```sh
cp -r walk-blocker-payload/shim/ ./shim-measure/
sh ./shim-measure/measure.sh ./shim-measure/guard.sh N BUDGET_MS GUARDED_BUDGET_MS
sh ./shim-measure/measure.sh --against ./previous/guard.sh ./shim-measure/guard.sh N PAIRS MAX_RATIO GUARDED_MAX_RATIO
```

`N` is the number of timed runs per measurement, `PAIRS` the number of
alternating old-versus-new pairs; every budget and ratio is yours. Run
under representative load: a quiet machine reports on the hour of the day,
not on the code, and if the result moves between runs, record the load with
it before deciding anything. Repeat on every deploy and whenever the node's
load class changes.

`measure-flags.sh`, beside it, measures a different thing: which of a
tool's short flags may cluster with a digit, by running the tool against a
tree of known depth. It is for maintaining the rule table's `depth_digits`
sets against the tool build a node actually runs, not for operating a
deployment.

## 10. Read the trails

There are two, and they answer different questions.

**The reaper's audit trail** is one JSON record per line at
`<spool_dir>/reaper-audit.jsonl`, where `<spool_dir>` is
`[install].spool_dir` — the unit passes it as `--spool`, and the reaper
names the file. It rotates once, to `reaper-audit.jsonl.1`, past
`[reaper].audit_max_bytes`. Every record carries the build version,
`layer`, an `action`, and — for a finding — the verdict, the pid,
`starttime`, the uid, the `origin` label, `age_s`, `cpu_s`,
`io_pressure_delta` and `stalling_slice` — and, on `opaque_traversal`
alone, `d_polls`, the consecutive polls the process has been seen in D
(ADR-0020). Read it as a member of `[install].spool_group`; the trail is
`0640`, readable by root and that group and nobody else, by decision
(ADR-0025). (`[install].audit_filename`
names a second file in the same directory, Layer 1's optional file sink;
the shim's records go to the journal, below, because a monitored account
cannot append to a root-owned file.)

```sh
tail -F <spool_dir>/reaper-audit.jsonl
```

Three habits when reading it:

- **Count keys, not rows.** A standing process is re-logged every poll, so
  rows run several times findings. Derive the finding count by counting
  distinct `(verdict, pid, starttime)`.
- **`stalling_slice` has three states**, and the absent one is load-bearing:
  `true` means PSI corroborated, `false` means measured and quiet, and the
  key being absent means the uid had no differenced reading at all this
  poll. Do not read absent as false (ADR-0009).
- **Sort by `NEVER_KILL`.** `orphan_idle`, `opaque_traversal` and
  `unparsed_traversal` can never be acted on and exit the unit 0; a new
  `runaway_traversal`, `orphan_traversal` or `fanout_traversal` exits 1; a
  `blind` record — no user slice, or `/proc` unreadable — exits 2; a spool
  that fails the reaper's own check, or a write into it that fails, exits 4,
  with every finding in the unit's journal and nothing written or signalled
  (ADR-0025). Under
  `--kill` only, a kill that was attempted and left the process not known
  to be gone — an action of `signalled_but_wedged`, `signal_failed` or
  `kill_error` — exits 3, and does so on every poll it recurs, because each
  poll attempts the kill afresh and the unit must not read green while the
  trail fills with attempts that changed nothing. That is what
  `systemctl --failed` is tracking, and it is
  dominated at some sites by other tenants' failed session scopes; know
  what else is in it.

**The journal**, under the `walk-blocker` tag, is where Layer 1 writes,
because a monitored account cannot append to a root-owned file:

```sh
journalctl -t walk-blocker -o json
```

It carries three kinds of record:

- the shim's **escape-hatch overrides** — `WALK_BLOCKER_UNSCOPED=1` on a
  command the shim would have refused — with the tool, the mount judgement
  and the reason, and `_UID` stamped by journald from the socket rather than
  taken from the environment being audited. The test seams
  (`WALK_BLOCKER_MOUNTS`, `_SHIM_DIR`, `_FSTYPES`, `_DEPTH_BY_MOUNT`) are
  recorded the same way, and only when they changed the outcome;
- the **reconcile's reports**: `hook_check` when a hook block is missing or
  no longer fires, naming `[hooks.<shell>].package` as the likely conffile
  actor; `audit_dir` when the spool had to be created, or its mode or group
  corrected (`created`, `mode-corrected`, `group-corrected`), or when it was
  left alone because it is a `symlink`, `not-a-directory` or
  `owner-not-root` — nothing is written into such a spool;
  `coverage_change` when the set of wrapped names changed; `relink_refused`
  when the relink stopped at one of its own checks;
- one **`refused`** record per refusal, from the shim itself, carrying the
  tool, the root it was asked to walk, the mount and type that judged it,
  the reason class and the caller's uid. This is the count that answers
  "did Layer 1 do anything", and the denominator for the escape-hatch
  count when the time comes to read real findings against real traffic.
  Two properties of journald apply: an ordinary user sees their own
  records and the `adm` group and root see everyone's; and a session that
  produces refusals faster than journald's per-unit rate limit loses the
  excess, which journald marks with its own "suppressed N messages" line;
- an **`uncovered_mount`** record, at notice priority, when a mount's
  standing changes (ADR-0016, ADR-0019): `state: expensive` the first time
  a mount in the live table is seen running on its compiled default with no
  `[[filesystems.mounts]]` override, `covered` once an override or a
  narrower default has taken it over, `unmounted` once it has left the
  table. A steady state is silent. The `expensive` line is the visible cost
  of not having surveyed: read the mount and the type, run the survey, and
  either add an override — the next poll answers with `covered` — or keep
  the survey output beside `site.toml` as the record that the default was
  chosen. After a reboot every uncovered mount is named once more, so a
  volatile journal is not left without the line. What the relink currently
  believes is uncovered is in `<spool_dir>/uncovered-mounts.state`,
  readable by the spool's group.

**What an empty journal means.** Quiet is healthy: nothing was refused, no
override was used,
every hook block is present and fires, the audit directory has the right
mode, and no expensive mount is running uncovered. **What it does not
mean** is that no unbounded walk ran. Every Layer 1 bypass — an absolute
path, a private `PATH`, a container, a batch script, a shell function, a
second-level shell — leaves no journal record, because the shim never ran.
A hook that is not on anyone's `PATH` also produces silence, and the
reconcile's `hook_check` distinguishes that case only when it can see the
block is gone. The journal tells you what overrode Layer 1 and when Layer 1
stopped being installed; the reaper's trail is what tells you what reached
the node regardless. Read both, for several days, before deciding anything.
Whether the journal persists across a reboot is a property of the node's
journald configuration, not of this tree.

## 11. Ask the node what is deployed

Four answers, and they should agree:

```sh
cat <prefix>/.walk-blocker-payload
cat <prefix>/site.lock.json
python3 <prefix>/reaper.py --version
sh <prefix>/shim/install.sh --version
<prefix>/bin/walk-job --version
```

The payload marker is written by the installer before any other file, so it
is the answer that survives a half-installed payload. `site.lock.json`
carries the schema version, the tool version and a hash per file. The
three `--version` lines are stamped literals, one number per payload
(ADR-0013); `reaper.py --version` needs `search_rules.py` beside it, like
every other invocation. `VERSION` at the root of this tree is the only
hand-written version anywhere; everything else is stamped from it.

### Is what is running what we reviewed?

Three commands answer it, and no single one of them does. None needs a
credential on the node, network access, or privileges.

```sh
# 1. on the node: do the installed files match the record they were built from?
python3 <prefix>/deploy.py --verify

# 2. off the node: is that record the one the site's repository holds?
sha256sum <prefix>/site.lock.json      # against payload/site.lock.json there

# 3. off the node: is that configuration on the reviewed branch?
walk-blocker provenance --payload payload/ --repo . --ref origin/main
```

Step 1 exits 0 when everything matches, 1 on drift, and 4 when it cannot
tell — a missing or unreadable record is not a clean install, and it does
not report one. It needs no privilege on purpose: every installed file is
world-readable (ADR-0012), so the people whose `PATH` this tool changed can
check the guard that refuses their commands.

Step 3 answers by content and never by location. It takes the `site_sha256`
the payload recorded and looks for a commit reachable from the reviewed ref
whose configuration hashes to it, so the answer survives the repository
changing hands and its URL with it. A pinned URL would become false on the
day the intended handover succeeds, and a rename redirect is a convenience
rather than a boundary: the vacated name can be claimed by anyone. Exit 0
reviewed, 1 not reviewed, 2 cannot answer. Its `--sha256` flag takes the
hash straight from step 1's output, so nothing has to be copied off the
node.

**Where the answer stops.** Step 1 compares a record with the files sitting
beside it, so anyone who edits a file *and* its entry in the record gets a
clean report. It is a tripwire for accidents — the undocumented hotfix, the
truncated copy, the install that stopped half way — and not a seal against
someone who means it. What it buys is that falsifying both takes knowing
the record exists and deciding to change it, which turns carelessness into
intent. The check that actually binds a deployment to a review is step 2
followed by step 3, and neither of them runs on the node.

**Nothing here is enforced, and it cannot be from this tree.** Making the
installer refuse an unreviewed payload would need the reviewing commit
recorded inside the payload, and that is a fixed point that does not exist:
committing the payload changes the commit the payload would have to name.
The gate belongs in the site repository's own CI, where the commit already
exists so the problem dissolves — run `provenance` against the pull
request's own revision, and `build --check` beside it, as required checks.

## 12. Promoting to `--kill`

The reaper's default is `--report`, and the unit is written that way.
Promoting it is a decision a person makes after reading real findings
against real traffic, not a default that drifts. The bar, from ADR-0009's
"Re-measure when": read the trail for several days, count distinct
`(verdict, pid, starttime)` keys inside and outside `NEVER_KILL`, and
promote only when every finding outside `NEVER_KILL` is one a human would
have killed. Recalibrate the stall thresholds under known load first
(ADR-0002); `stalling_slice: false` on a kill record is what tells the
reviewer PSI did not corroborate it, and it is not a precondition.

Two flags, both explicit:

- `--kill` acts on findings outside `NEVER_KILL` — but only on processes
  owned by the invoking user;
- `--kill-others` permits acting on other users' processes. Killing someone
  else's long-running work is a per-incident human decision, and this flag
  is how the decision is made visible in the unit file rather than buried
  in a default.

The kill budget is `[reaper].kill_grace_s` between `SIGTERM` and `SIGKILL`,
at most `[reaper].max_kills` per poll (the rest are recorded
`skipped_kill_cap`), and a re-check after `[reaper].settle_s`. A process
blocked in a filesystem syscall does not die until the syscall returns; if
it is still there after the re-check the record says `signalled_but_wedged`,
never `killed`. An audit log that reports success it did not achieve is
worse than no audit log.

Changing the unit's arguments is not a `site.toml` value; it is an edit to
the unit the deployer writes, made at the site, and it should be recorded
beside the evidence that justified it.

## 13. Changing a site value

Every value in `site.toml` is compiled into the payload as a literal
(ADR-0013). There is no file on the node to edit and no flag to pass. To
change one:

```sh
$EDITOR site.toml
uv run walk-blocker validate --site site.toml
uv run walk-blocker build --site site.toml --out payload/
```

then copy the payload to the node and run the install again (steps 6 to
8). The install over an existing prefix is the redeploy: it recognises its
own payload marker, replaces the payload from the snapshot, rewrites the
hook blocks between their markers, and restarts the timer. The diff to
`site.toml`, reviewed in the site's own repository, is the review ADR-0005
wanted for a root-run installer's write targets.

Two changes deserve a second look before the rebuild. A change under
`[install]` or `[hooks.<shell>].file` moves where root writes; read the
preview (step 7) with particular care. A change to `[timer].on_calendar`
must be re-surveyed against the live schedule of the node, not carried over
(ADR-0017).

## 14. Uninstall

As root, on the node:

```sh
python3 <prefix>/deploy.py --uninstall
```

It reverses the install and is gated on root alone — reversing a control is
the safer direction and does not need the same ceremony as installing one
(ADR-0004). It disables and removes the timer and service, strips every
hook block and removes the fish drop-in whether or not that shell still
resolves (ADR-0008), and removes the prefix. The `<file>.walk-blocker.orig`
backups and the audit trail under `[install].spool_dir` are records; read
its output for what it left, and copy the trail somewhere before removing
it if the evidence is still wanted.

`install.sh --uninstall` exists too, but it refuses to run from anywhere but
the deployed copy under the prefix, for the same reason a writing run
does: a checkout is writable by the account that owns it (ADR-0005).
