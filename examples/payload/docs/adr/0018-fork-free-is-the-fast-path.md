# ADR-0018: The fork-free rule is the fast path's; the guarded path pays one documented fork to read the mount table

**Status:** accepted, 2026-09-17
**Narrows:** ADR-0015, "Before `sg_exec_real`, the shim forks nothing — no `$(...)`, no backticks, no pipeline"
**Evidence:** held privately by Fluid Numerics, keyed ADR-0018 — see `docs/evidence.md`

## Context

ADR-0015 states the shim's performance property in one sentence: before
`sg_exec_real` it forks nothing, reads exactly one file, and makes no call
that can block. `CLAUDE.md`, the architect agent and the Copilot
instructions restate that sentence, and reviewers cite it.

The code has never quite matched it, and says so in its own comment. The
shim has two paths. The **fast path** decides from argv alone — an
unrecursive `grep`, a depth-bounded `find`, a name the table does not wrap
— and exits or `exec`s without looking at a mount. The **guarded path** is
everything that reaches a mount judgement: a recursive walk whose roots
must be classified before they can be allowed or refused. To classify, the
shim reads `/proc/mounts`, and `sg_load_mounts` reads it through one
trusted `awk` rather than a `read` loop in the shell, because `dash`'s
`read` consumes the table one byte per `read(2)` so as not to over-read the
descriptor, while `awk` parses the same bytes in one process. Measured at a
reference deployment, on a table of many hundreds of lines, that fork is
the difference between hundreds of milliseconds and single digits; the
ratio, not the figure, is the argument. Under `bash` invoked as `sh` the
command substitution also costs a subshell, so the guarded path creates two
clones there and one under `dash`, and in both cases exactly one *program*
other than the shell runs before the real tool.

An execution-based test now counts this under `strace`, for both shells and
for one argv on each path. Read literally, ADR-0015's sentence is therefore
false for every refused call and for every allowed recursive call on a
cheap mount, and a probe that measures will keep finding it. A design
record that a measurement refutes on every reading teaches reviewers to
discount the records that are true.

Three ways to close the gap were on the table.

- **Remove the fork.** A pure-`sh` reader exists and runs when the trusted
  `awk` is absent, so the guarded path *can* be fork-free. It was rejected
  on the measurement: the reader's cost scales with the table's byte count
  in a way the fork's does not, and the guarded path is the one that runs
  with the largest tables, on the busiest nodes, exactly when the shim's
  own latency is least tolerable. The fast path is where a fork would be
  felt on every `ps aux | grep sshd`; the guarded path is already about to
  walk a filesystem.
- **Leave the wording and refute each probe.** Rejected because the
  sentence is the citation target. A claim the architect cites and a test
  contradicts is worse than either alone.
- **Edit ADR-0015's Decision in place.** Rejected by the convention in
  `docs/adr/TEMPLATE.md`: an accepted record is narrowed by a new one that
  names the clause, so the history of what was claimed stays readable.

## Decision

The fork-free property belongs to the fast path. **Before any mount
judgement, the shim creates no process**: no `$(...)`, no backticks, no
pipeline, no external program. That is what "the shim forks nothing" means
from this record on, and it is asserted two ways — by a text scan of the
fast-path region and by counting clones under `strace` on a fast-path argv,
under both `dash` and `bash` invoked as `sh`.

**The guarded path may create exactly one documented program before the
real tool**: the trusted `awk`, by absolute path from
`[trusted_binaries].awk`, run once over `/proc/mounts` by
`sg_load_mounts`. No other program, and no second read. Under a shell
whose command substitution forks a subshell, that is two clones and one
program; the test counts programs, because a shell builtin that forks in
one shell and not the other is precisely what a clone count would confuse
with a design change. Where the trusted `awk` is absent the pure-`sh`
reader runs and both paths create nothing; the test counts the `awk` execs
it finds rather than asserting one, so it stays honest in both
configurations.

The rest of ADR-0015's clause is unchanged and applies to both paths:
before `sg_exec_real` the shim reads exactly one file, `/proc/mounts`, and
makes no `statfs`, network or configuration call. Nothing it does before
the decision can block on the filesystem being judged.

## Consequences

**The sentence to cite changes.** `CLAUDE.md`, the architect agent and the
Copilot instructions say: the fast path forks nothing; the guarded path
pays one documented fork to read one file; nothing before `exec` reads
anything that can block. Prose that restates the old sentence in its short
form — ADR-0013's Context, the README's "one file, no fork", the plan — is
read under this record and corrected where it is not an accepted ADR's
body.

**A second program on the guarded path is a design change, not a tuning.**
The test fails on it by name. Moving work from the shell into that one
`awk` is fine; adding a `sed`, a `cut`, a `grep` beside it is not, and
neither is a second `awk` invocation on the same table.

**A fork on the fast path is still a defect**, and the strace row for the
fast path is what catches the one the text scan cannot see: a builtin that
forks under one shell.

**`measure.sh` is unchanged.** It times the fast path and the guarded path
against separate budgets and the guarded path against the fast path; this
record explains why the second budget is larger, it does not move either.

**The refusal path pays the fork too**, and that is accepted: a refusal
already costs the user a message and a retry through `walk-job`; the
milliseconds are not where that cost lies.

## Re-measure when

- On each deploy, with ADR-0015's procedure: run `measure.sh` from local
  disk against the site's recorded budgets, under representative load, and
  record the result beside `site.toml`.
- Before proposing to drop the fork: time the pure-`sh` reader against the
  trusted `awk` on the site's own mount table, from local disk, both under
  `dash` and under the site's `/bin/sh`. If the reader is within the
  guarded-path budget on that table, a new record can narrow this one; a
  smaller table at a different site does not.
- When a mount table grows past what the site measured — a node that gains
  a per-user autofs tree, a container runtime that mounts per job — since
  the table's byte count is what the fork buys back.

## Site config touched

- `[trusted_binaries].awk` — the one program the guarded path may run
  before the real tool.
