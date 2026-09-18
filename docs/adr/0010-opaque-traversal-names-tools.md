# ADR-0010: `opaque_traversal` names unmodelled tools, not unmodelled waiting

**Status:** accepted, 2026-09-16
The clause "a tool this tree has not modelled, doing something in D that might be a walk" is narrowed by ADR-0020: the arm names a process observed in D at two consecutive polls, not one.
**Evidence:** held privately by Fluid Numerics, keyed ADR-0010 — see `docs/evidence.md`

## Context

The first days of audit data after a report-only deploy are the first real
reading a site has. At the reference deployment that reading covered a small
number of distinct processes, and every one of them carried the same verdict:
all but one were `tail -n0 -F` on a set of log files in a home directory on
the expensive filesystem, blocked in D state and reparented to init; the one
was the node's largest offender — the unbounded `ugrep` of `/` from ADR-0009's
table. Only the last is evidence of anything, and it was recorded beside the
log-followers under one verdict, in the trail the `--kill` decision is supposed
to be read off.

`tail -n0 -F` opens the fixed set of files named on its own command line and
blocks waiting for appends. GNU tail has no recursion option at all: `--help`
names none, and a directory operand produces nothing (measured on the tool). No
argv makes it a traversal.

### The fix as first proposed cannot work

The first proposal was to gate the arm on `on_expensive_mount`, matching the
plan document's description of it ("D state **on the expensive filesystem**,
past budget"). That gate makes the arm **unreachable**. `traversal_roots()`
returns early for a tool with no profile:

```python
profile = R.PROFILE_BY_NAME.get(R.tool_from_argv(proc.argv, proc.comm))
if profile is None or not proc.argv:
    return ([], False, None)
```

so `hits` is empty and `on_expensive_mount` is `False` on exactly the
processes this arm catches. Gating on it deletes the verdict rather than
narrowing it. The plan document's "on the expensive filesystem" was
aspirational when it was written; the code never checked it and structurally
cannot.

### And the premise it rested on was wrong

At the reference deployment the home filesystem *is* the expensive type, so
those `tail`s really were blocked on the filesystem this reaper is a backstop
for. They are not excludable on the grounds that they never touched it.

## Decision

The reason to drop them is narrower and better: **`tail -F` is not a
traversal.**

The rule table grows `STREAM_FILTERS`, a set of tools that cannot walk a tree
under *any* argv, and `is_stream_filter(argv)`. The `opaque_traversal` arm
consults it as one more conjunct, beside the kernel-thread guard it sits next
to.

**An excluded process leaves no record at all** — not a downgraded verdict and
not a suppressed-count line. The trail exists to be read, and re-encoding the
noise under another name is not removing it.

**Keyed on `argv[0]`, never on `comm`.** That is the rule `tool_from_argv()`
already states and that was measured when it was written. `argv[0]` is itself
forgeable (`exec -a tail …`), which is tolerable here and would not be in a
killing decision: `opaque_traversal` is in `NEVER_KILL`, so the cost of a
forged exclusion is one missing audit line, never a wrongful kill. Keying on
the narrower field also yields the narrower exclusion, which is the direction
this one should err.

**Membership is evidence-backed, one name at a time.** A tool goes in when it
has been measured to have no traversal grammar *and* has actually appeared in
the trail. `tail` is in because the trail was almost entirely `tail`. Siblings
— `less`, `cat`, `dd` — are not, and adding them by assumed symmetry is how an
exclusion list stops describing the node and starts hiding it. A site's own
additions go to `[reaper].stream_filters`, with the evidence for each held by
the site.

## Consequences

- The corpus stops mixing two different things under one verdict. On the
  reference data the trail became one finding, which is what it always
  contained.
- **This is the same *kind* of exclusion as the kernel-thread guard**
  (ADR-0009), on the same ground, and that is deliberate. This verdict exists
  to keep the corpus honest about which **tools** have not been modelled. A
  thread with no command line contributes nothing to that; neither does a
  process whose command line names a tool that cannot walk.
- `opaque_traversal` and `runaway_traversal` now differ in what they claim,
  not merely in how much detail they carry. The companion change that modelled
  `ugrep` (ADR-0011) moves the one real finding out of this verdict entirely —
  after both, `opaque_traversal` means a tool this tree has not modelled,
  observed in D at two consecutive polls (ADR-0020), and nothing weaker.
- **An exclusion list is a place defects hide, and this one is kept small on
  purpose.** The honest risk is not the `tail` entry; it is the next five added
  at once because they "obviously" cannot walk either. The one-at-a-time rule
  above is the mitigation, and a test pins the argv-not-comm keying so the
  cheaper version of it cannot be introduced by accident.
- The plan document's description of the arm was wrong on three counts
  independent of this change — "on the expensive filesystem", keyed on `comm`,
  and silent about the kernel-thread guard. It was corrected in the same
  commit. A verdict described by a document that does not match it is how the
  `on_expensive_mount` proposal came to be written in the first place.

## Re-measure when

A stream-only tool not in `STREAM_FILTERS` recurs in a site's trail under
`opaque_traversal`. Measure it on the tool before adding it: `--help` names no
recursion or directory-walking option, and a directory operand produces no
walk (no `getdents64` under `strace`, or equivalent). Then add that one name to
`[reaper].stream_filters`, and record the measurement with the site's
evidence. Never add a sibling by analogy.

## Site config touched

- `[reaper].stream_filters`

## Superseded wording

Narrowed by ADR-0020. The Consequences above read:

> a tool this tree has not modelled, doing something in D that might be a walk

The arm names a process observed in D at two consecutive polls, not one.
