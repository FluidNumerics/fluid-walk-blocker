# ADR-0011: Layer 2 reads stdin from `/proc`; it does not assume a terminal

**Status:** accepted, 2026-09-16
**Evidence:** held privately by Fluid Numerics, keyed ADR-0011 — see `docs/evidence.md`

## Context

`traversal_roots()` called into the rule table with `stdin_is_tty=True`
hardcoded, and that was defensible for as long as it lasted: the reaper cannot
see another process's terminal the way a shell can see its own, and no profile
except `fzf`/`sk` cared. Neither is wrapped and neither sits in D state on the
nodes this has run on, so the assumption never produced a record.

Modelling `ugrep` ends that. Two facts collide:

- **`ugrep` with no FILE operand decides what it walks by asking whether stdin
  is a terminal.** Measured on ugrep 7.x against a multi-level tree: at a
  terminal it recursed the working directory to the bottom — an implicit `-R`,
  unbounded. On a pipe it printed `(standard input)` and walked nothing.
- **A login node runs a number of long-lived piped log filters** of the shape
  `ugrep --line-buffered -E …`, no operand, on the end of a pipe, with the
  working directory in a home on the expensive filesystem.

With the profile added and the terminal assumed, every one of those filters
would take the cwd fallback, land on an expensive mount, and be classified
`runaway_traversal` on the first poll after deployment. That verdict is **not**
in `NEVER_KILL`. The change meant to make the node's one real runaway killable
would have made every innocent log filter killable alongside it, which is the
precise inversion this tree's rules about the audit trail exist to prevent.

### The fallback is not the thing that is wrong

The tempting narrow fix is to give `ugrep` no default root, so a no-operand
invocation charges nothing in either layer. It removes the false positives and
it is wrong about the tool: `ugrep PAT` typed at a prompt in a home on the
expensive filesystem really does walk that home, unbounded, and that is exactly
the shape Layer 2 exists to catch when someone has gone around Layer 1.
Declining to model it buys quiet by giving up the case.

### `/proc` can answer the question

`/proc/<pid>/fd/0` names the file stdin is open on. Measured:

| stdin | link reads | device |
|---|---|---|
| ssh session | `/dev/pts/N` | char, major 136 |
| console | `/dev/ttyN` | char, major 4 |
| pipe | `pipe:[inode]` | not a device |

And, measured the same way, it sits behind the **same permission gate as
`cwd`** — both `/proc/<pid>/fd` and `/proc/<pid>/cwd` are `PTRACE_MODE_READ`.
For every process owned by another user, an unprivileged reader gets
`PermissionError` on both; as root it gets both.

**That shared gate is not the whole story, and the code does not rest on it.**
It covers the PERMISSION cause of an unreadable stdin and not the other one. A
process that simply closed fd 0 has no `/proc/<pid>/fd/0` at all — `ENOENT`,
not `EPERM` — while its `cwd` reads perfectly. Measured: a child calling
`os.close(0)` shows `fd/0 -> FileNotFoundError` and a readable `cwd` at the
same instant. So the two really can disagree, and the decision below needs a
second leg that does not depend on them agreeing.

## Decision

`Proc` gains `stdin_tty`, read in `read_proc()` beside the existing `cwd`
readlink and with the same `except OSError: pass` discipline.
`traversal_roots()` passes the measured value instead of `True`.

**Three states, and the third is not the second.** `True`, `False`, and `None`
for "not readable". The code acts on `proc.stdin_tty is True`, so `None`
charges no fallback root. Two independent reasons make that right, and the
second is the load-bearing one:

1. Where the cause is permission, the shared gate applies: a process whose
   stdin we cannot see is one whose `cwd` we cannot resolve either, so the
   fallback it would have taken had nothing resolvable to charge.
2. Where the cause is a CLOSED descriptor, `cwd` is readable and the gate
   argument says nothing — but a process with no stdin is not at a terminal
   either. `ugrep` there reads a closed descriptor and walks nothing, so
   charging no root is still correct, for a reason of its own.

`is not False` is wrong under (2) and only under (2): it reads `None` as a
terminal, charges the cwd, and manufactures a traversal finding against a
daemon sitting in a directory on the expensive filesystem — a finding about a
walk that is not happening. Pinned by
`test_a_closed_stdin_is_not_a_terminal_even_with_a_readable_cwd`, which is
the only test in the suite that distinguishes the two spellings.

**Read by device number, not by matching the link text.** A terminal is a
character device whose driver is a tty: major 136–143 for pts, 4 for the
virtual consoles and serial lines, 5 for `/dev/tty` and `/dev/console`.
Matching `/dev/pts/*` and `/dev/tty*` as strings is a list of spellings rather
than a fact about the file, and a site may run more than one ssh transport
(ADR-0003) precisely because spellings are not something this tree controls.

## Consequences

- **The live log filters produce no record**, and `ugrep PAT` at a terminal in
  a home on the expensive filesystem produces `runaway_traversal`. Both are
  pinned, in one test, on the same argv and the same cwd — only the stdin
  differs.
- **`fzf`/`sk` are fixed by the same change**, incidentally. They carried the
  identical assumption and would have started reporting the cwd of every piped
  `fzf` the moment one wedged.
- **Cost: one `os.stat` per process per poll.** Measured at a reference
  deployment as small against the whole-table scan it joins (ADR-0009), on the
  poll timer. Named rather than waved away, because "it is only a stat" is how
  a per-process cost gets added four times.
- **Layer 1 and Layer 2 now answer the same question by different means** —
  `[ -t 0 ]` in the shim, `/proc/<pid>/fd/0` in the reaper — and that is a
  divergence to keep an eye on rather than a symmetry to celebrate. It is
  unavoidable: the shim IS the process and the reaper is looking at someone
  else's. The rule matrix cannot cover it either, because `run_shim` supplies a
  pipe while `R.check` defaults to a terminal, so the no-operand rows live in a
  dedicated test that drives both values explicitly and says why.
- The assumption was safe for as long as no profile read it, which is the part
  worth remembering: it was not a latent bug that happened not to fire, it was
  a modelling choice that stopped being true when the model grew. The next
  profile that senses something about a process rather than its argv will need
  the same treatment, and the `is True` spelling is where to look.

## Re-measure when

n/a: structural. The device majors are kernel facts, and the `PTRACE_MODE_READ`
gate on `fd/` and `cwd` is a kernel fact. The one site-dependent figure is the
per-poll cost of the added `os.stat`, which scales with the process table; if
the scan's wall time is ever re-measured for ADR-0009, measure it with and
without this read and record both.

## Site config touched

none
