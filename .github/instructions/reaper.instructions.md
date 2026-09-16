---
applyTo: "node/reaper.py,tests/test_reaper.py"
description: Reviewing Layer 2, the root-run reaper that backstops the advisory shim.
---

# This is the backstop, so it must not fail quietly

Layer 1 is advisory: `/usr/bin/find` walks past it. A walk Layer 1 refuses is
*precisely* what this code exists to catch, so a miss here is not a redundancy
— it is the only detection gone.

## Never fail open

`return ([], False)` on an exception meant "no expensive roots, nothing
unresolved" — a clean bill of health indistinguishable from a healthy process,
and the subject then fell past every arm of `classify()` and left **no record
at all**.

`stalling_slice` has three states — stalling, not stalling, and *absent*
(the slice is gone, or its pressure file is unreadable). Collapsing absent
into false is the clean bill of health this code must never give (ADR-0009):
a user whose slice cannot be read is not a user who is fine.

Check any error path here against three questions:

1. Can the caller tell "clean" from "this code failed"?
2. Does the subject still reach the audit trail, or does it vanish?
3. Could the failure make something **killable** that should not be?
   `NEVER_KILL` must contain any verdict that names a defect in this code
   rather than a culprit — killing because the parser crashed is acting on our
   own failure.

Kernel threads are excluded by reading `/proc/<pid>/cmdline` and finding zero
bytes — not by `[ -s ]`, which is a size check on a file `/proc` reports as
size zero for everyone, and not by `ps`, which is a fork and a parse. A
user-space process with an empty cmdline is a real edge; a kernel thread
misclassified as a user process is a finding against the wrong owner
(ADR-0009).

## Layer 1 refuses ⇒ Layer 2 reports

Assert the **implication**, not equivalence. The converse is deliberately
false: the reaper does not consult `bounded()`, because Layer 1 judges a
command before it runs while Layer 2 watches a process already past budget,
and a bounded walk that is past budget is still worth naming. A number of
matrix rows sit in that state; a test asserting the layers *agree* would
encode a false claim about the design.

Both layers must resolve roots through the **shared** entry point
(`resolved_roots`). Applying a transformation in one caller is how the two
drifted; applying it twice is worse than forgetting it once, because a relative
`--base-directory` resolved against an already-changed base is wrong.

## Origin is descriptive

Origin labels come from `[reaper].origins`, compiled in. Origin describes how
a process arrived — which transport, which session scope — and it never
reaches `classify()`: the verdict is about what the process is doing, not how
it got there (ADR-0003). That rule is code with a test, not a convention. A
diff that passes origin into a classification arm, or branches on it before
one, is the finding.

## Findings are about a process, not a user

- A stalling cgroup is evidence about a *user*; the `/proc` step naming a
  specific process is what makes an action defensible.
- Never record a kill that did not land. After TERM → grace → KILL, re-check;
  a process blocked in a filesystem syscall does not die until that syscall
  returns.
- The inverse failure is just as real: never let a kill that DID land go
  unrecorded either. The audit trail dedupes by (finding key, action) so a
  standing `reported` finding is not re-appended every poll — but a REAL
  signal, sent fresh every poll against a genuinely wedged process, can
  legitimately keep returning the identical action string
  (`signalled_but_wedged`). Deduping on the string alone silently dropped
  every signal after the first — caught only on a later review round in the
  predecessor design. Any code path that can send a real signal has to bypass
  the dedup unconditionally, not just when the resulting action changes.
- An unreadable `/proc/<pid>/cwd` is the **normal** case for another user's
  process, so how unknown-cwd is handled decides most findings. A path still
  relative after resolution is genuinely unresolved — say so rather than
  guessing a cwd and putting someone else's directory in the log.
- PSI totals are cumulative since boot. Difference successive polls.
- Which mounts are expensive is compiled from `site.toml` (ADR-0013,
  ADR-0016), and the reaper must agree with the shim on every row of the
  mount table. A finding that a process is walking "an expensive mount" is
  only as good as that agreement.
