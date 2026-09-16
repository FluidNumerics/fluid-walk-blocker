# ADR-0001: Two layers, and only one of them enforces

**Status:** accepted, 2026-09-16
**Evidence:** held privately by Fluid Numerics, keyed ADR-0001 — see `docs/evidence.md`

## Context

The founding incident has a simple shape: an automated agent ran an unbounded
`find` over ssh against a very large network filesystem; the client timed out
and retried, the remote process was never killed, and it ran for days.

The obvious response is a wrapper: put something in front of `find` that
refuses an unbounded walk. That is worth doing and it is not sufficient, for a
reason that is easy to discover the expensive way.

### Text-level guards cannot see through `ssh`

A guard that inspects a command *as a string* before it runs — a pre-execution
hook, a shell function, a lint over a script — has a structural blind spot.
Tokenize this with a POSIX-correct lexer:

```sh
ssh login-node 'find /data /home -type f -name "job-*.out" -print 2>/dev/null | head -500'
```

and you get three tokens: `ssh`, `login-node`, and **one opaque token** that
happens to contain an entire pipeline. A guard scanning for a token whose
basename is `find` does not find one; the last path component of that third
token is `null | head -500`.

This was measured, not assumed. Fluid Numerics already ships a session-level
guard for AI coding agents — a pre-execution hook that refuses `find` on
protected subtrees, backed by a shared shell parser whose every rule was added
after a specific miss. A text-level guard at a reference deployment was run
against synthetic payloads: it caught every local form and missed the
ssh-quoted form, which was the incident shape. It also had no view of a walk
split across `xargs -P` children, each individually bounded and collectively
a full traversal.

The fix is not to teach the guard to parse inside quoted strings. Down that
road lie nested quoting, `bash -c`, `sbatch --wrap`, base64'd payloads and
variables holding paths — an unbounded parsing problem in service of a
heuristic. The lesson is that reading commands as text has a ceiling, and the
ceiling is below where we need to be.

### A PATH shim is above that ceiling, and has its own

A shim placed on `PATH` is not handed a string. It is handed `argv`, after the
shell has tokenized, expanded, resolved variables and substituted. The quoting
that defeats a text guard never reaches it. A remote command arrives at the
shim on the node as plain arguments.

It also closes the `xargs` case: `xargs -I{} find {}` calls `execvp` with `{}`
already replaced by a real path, so the shim sees a real root where a text
guard sees a literal brace.

What a shim cannot do is bind. An absolute path — `/usr/bin/find` — skips it.
So does a tool resolved out of an ephemeral environment, `uvx rg` being the
canonical example. So does any script that sets its own `PATH`, any container,
any scheduler job script. And nothing in `argv` reveals that this is the
sixteenth concurrent child of an `xargs -P16`, each individually bounded and
collectively a full walk.

So does a **shell function**, which is the bypass class this record first
listed in the abstract and which then arrived as live traffic at the
reference deployment. An AI coding agent's shell snapshot defined `grep()`,
`rg()` and `bfs()` as functions that run `( exec -a <name> <bundled binary>
… )`. A function beats `$PATH` outright, so those three shims are never
consulted inside such a session, and the heaviest users of a login node are
increasingly working inside one. `exec -a` forges `argv[0]` as well, so the
bundled binary it reaches that way never causes a PATH lookup for the name at
all — there is no name for a shim to be installed under. Nothing in a PATH
shim can outrank a shell function, so this is recorded rather than fixed.

## Decision

Build both, and be explicit about which is which.

**Layer 1, the shim** — a PATH shim in front of the traversal tools. Its job
is to make the naive invocation fail in under a second with an error that
names the cheap alternative. It is advisory. It has a documented, audited
escape hatch (`WALK_BLOCKER_UNSCOPED`), because a block with no override gets
routed around with `\find` and then nothing is measurable.

**Layer 2, the reaper** — a periodic poll. Its job is to find what reached the
node regardless. It does not care how the process got there, which is
precisely why it cannot be evaded by changing how the process gets there.

## Consequences

- The docstrings must say Layer 1 is bypassable. A guardrail described as a
  sandbox invites reliance it cannot support, and the first person to discover
  otherwise will reasonably conclude the whole thing is theatre.
- Layer 2 needs a signal that does not depend on recognising a program by
  name. See ADR-0002.
- Aggregate fan-out gets an explicit verdict (`fanout_traversal`) rather than
  being left to per-invocation rules that are arithmetically defeatable by
  splitting one walk into sixteen.
- **The wrapped set has to be wider than `find`.** Of the unbounded traversals
  sampled at the reference deployment, only a minority were `find` at all;
  recursive `grep` dominated, and `rg`, `fd`, `tree` and `du` walk the same
  metadata just as hard. A guard scoped to `find` alone addresses the shape
  that named the incident and misses most of what actually runs.
- **Neither layer had named the ssh transport it depends on.** More than one
  may serve a node. Layer 2 turns out to be unaffected by which one does;
  Layer 1 works, but through a mechanism with one more bypass in it than this
  record lists — a nested shell. See ADR-0003 for Layer 2 and ADR-0008 for
  Layer 1.
- A companion fix belongs upstream in Fluid Numerics' own session-level
  guard: teach it to treat `ssh <host> <token containing a search tool>` as a
  remote command and re-enter its check on that token's contents, with cwd
  unknown. Same for `srun` and `sbatch --wrap`. That is work in a different
  repo, but it is the only layer that stops the walk before it reaches the
  node at all, and it is worth having even though it, too, can be evaded.
- **Every runtime seam the shim reads is another way around it.**
  `WALK_BLOCKER_MOUNTS` is a test seam over the mount-table loader: pointed
  at an empty or unreadable table it sees no expensive mounts and has no
  opinion, which reads as "allow" — silently, unlike `WALK_BLOCKER_UNSCOPED`.
  `WALK_BLOCKER_SHIM_DIR` is narrower (it only picks which directory the
  resolver skips while finding the real binary) but has the same property.
  `WALK_BLOCKER_FSTYPES` (which filesystem types count as expensive at all)
  and `WALK_BLOCKER_DEPTH_BY_MOUNT` (the per-mount depth ceiling) joined them
  once both moved from literals compiled in at generation time to live
  runtime seams. All of them are audited the same way the escape hatch is,
  and only when they actually change the outcome — a seam that was set but
  did not alter the verdict is not a record worth writing.

## Re-measure when

The ssh-quoting blind spot and the binding limits of a PATH shim are
structural; nothing about a site changes them. The wrapped set is not. When a
site's audit trail shows Layer 2 naming a traversal tool that Layer 1 does not
wrap, add a profile for it. Procedure: over a period of ordinary use, list the
distinct executables Layer 2 findings name against remote mounts; any that is
not in the wrapped set is the candidate. The bar is that it appears at all —
one unwrapped tool in the trail is one the shim will keep missing.

## Site config touched

none
