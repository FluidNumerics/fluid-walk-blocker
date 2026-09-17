# ADR-0015: Node code is POSIX `sh` or stdlib-only Python 3.9, and the shim is the `sh` part

**Status:** accepted, 2026-09-16
The clause "Before `sg_exec_real`, the shim forks nothing — no `$(...)`, no backticks, no pipeline" is narrowed by ADR-0018: the fork-free rule is the fast path's, and the guarded path pays one documented fork — the trusted `awk` over `/proc/mounts` — before the real tool.
**Evidence:** held privately by Fluid Numerics, keyed ADR-0015 — see `docs/evidence.md`

## Context

Login nodes run an interpreter this project does not choose. Enterprise
distributions ship Python 3.9 as the system interpreter and keep it for the
life of the release; there is no `uv` on the node, no virtual environment, and
no expectation that an administrator will install one for a guardrail.
Everything that executes on the node has one Python available, and anything
newer than that Python's stdlib is a dependency the node does not have.

The shim is different in kind from the rest of the node code. It sits in
front of `grep`, `find`, `du` and the other wrapped tools, so it runs on every
invocation of those tools in every pipeline on the node, and its cost is paid
by every user whether or not the guard ever refuses anything. At a reference
deployment the start cost of `python3 -S` — the interpreter with site
initialization disabled, the cheapest way to start it — was measured against
the start cost of `sh`, and the interpreter cost roughly an order of magnitude
more. That ratio, not any absolute figure, is the argument: a guard that adds
an interpreter start to every `grep` in a build script is a guard people
notice, and a guard people notice is a guard people alias around. ADR-0001
says a false refusal is as damaging as a bypass because of what it teaches;
a slow allow teaches the same lesson.

The second constraint on the shim is not speed but liveness. The shim judges a
command against the mount table before the command runs, and the thing it
guards against is a filesystem that is wedged or about to be. Any call that
touches the judged filesystem — `statfs`, a `stat` of the root, a `df` —
blocks exactly when the filesystem does. A guard that hangs when the
filesystem hangs is worse than no guard: it turns every `grep` on the node
into a hung process at the moment the node can least afford one. The same
holds for a network call, and for a configuration read off a filesystem that
might be the one in trouble (ADR-0013).

The alternative in its strongest form: write the shim in Python with `-S -E`,
precompile it, and accept the start cost, because Python gives a real argv
parser, tests in the same language as the rule table, and no second
implementation of the grammar to keep in agreement. It was rejected on the
measurement — the interpreter start is the cost, and precompilation does not
remove it — and it would be rejected without the measurement, because a Python
shim cannot be fork-free before exec: the interpreter *is* the fork. The
agreement problem it solves is solved instead by generating the shim from the
rule table and testing the two against a shared matrix.

## Decision

Everything under `node/` — `deploy.py`, `reaper.py`, `walk-job`, `survey.py`,
the shell installer, the shim — is either `sh` that runs clean under `dash`
or Python that runs on the 3.9 stdlib with no third-party import. The shim is
the `sh` part, and the shim is generated (ADR-0013), so its grammar is written
once, in Python, in the rule table.

Before `sg_exec_real`, the shim forks nothing — no `$(...)`, no backticks, no
pipeline — reads exactly one file, `/proc/mounts`, and makes no `statfs`,
network or configuration call. Everything it needs to decide is in its argv,
its environment, its cwd and that one file. Both properties are tested: the
fast path is asserted fork-free, and the set of files it opens is asserted.

CI runs the Python under a matrix that includes 3.9, and drives every shell
tool under both `dash` and `bash` invoked as `sh`. A construct that passes on
the workstation's interpreter and fails on the node's is the failure the
matrix exists to catch.

## Consequences

**The node Python is written to 3.9.** No `match`, no `X | Y` in annotations,
no parenthesised context managers, no `tomllib`. The workstation tool under
`src/walk_blocker/` runs under `uv` and may import third-party packages, but
is held to 3.9 syntax because one package and one suite run across the whole
CI matrix (`tomli` stands in for `tomllib`); stdlib-only is the `node/`
boundary, and a helper both sides need is written to the node's standard.

**`measure.sh` gates performance, in two ways.** It times the fast path (an
allowed command) and the guarded path (a command that reaches a mount
judgement) against absolute budgets, and it times the shim against itself so
the guarded path's cost relative to the fast path is bounded. The budgets are
arguments to `measure.sh`, recorded in the site's own evidence, not constants
in this tree (ADR-0014). Measure from local disk, never from the expensive
mount: the measurement must not depend on the thing it is measuring the cost
of avoiding.

**A Python shim is rejected**, with the condition for revisiting stated:
re-measure only if a site's interpreter start is within its shim budget, and
note that it still fails the fork-free requirement, which no interpreter start
can satisfy. A faster interpreter changes the first argument and not the
second.

**The shim reads `/proc/mounts` and nothing else, so it cannot know a mount's
size** — which is why a mount's class is site configuration and not a runtime
measurement (ADR-0016).

**Two implementations of one grammar exist** — the rule table and the
generated shim — and their agreement is tested row by row rather than assumed.
A disagreement between them outranks the parsing question underneath it.

## Re-measure when

On each deploy, and whenever the node's load class changes — a login node that
has acquired a build farm, or lost one, is a different machine. Run
`measure.sh` from local disk against the site's recorded budgets and record
the result beside the site's `site.toml`. A budget calibrated on a quiet
machine reports on the hour of the day, not on the code: measure under
representative load, and if the result moves between runs, record the load
with it before deciding anything.

## Site config touched

none. The budgets are `measure.sh` arguments and live in the site's own
evidence. `[trusted_binaries].sh` and `.python3` name which interpreters the
node runs but do not change the rule.
