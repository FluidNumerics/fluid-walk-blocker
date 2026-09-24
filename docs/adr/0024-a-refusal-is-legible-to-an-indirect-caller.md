# ADR-0024: A refusal is legible to a caller that reads neither stderr nor a pipeline's exit code

**Status:** accepted, 2026-09-21
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

Layer 1's refusal was written entirely inside one `{ … } >&2` group, and it
exited 2. Every actionable line was in that group: the `-xdev` suggestion, the
depth bound, `walk-job`, the escape hatch and the guide path.

So a caller that runs `2>/dev/null` saw nothing at all, and got an exit code it
could not distinguish from the wrapped tool's own failure. `grep` documents 2
as "an error occurred"; `find` documents "0 good, >0 bad". "Found nothing" and
"was not allowed to look" became the same observation — a confident wrong
answer, in a guard whose entire thesis is that a refusal teaches. `2>/dev/null`
on a `find` or `grep` invocation is not an unusual thing for a caller to write;
it is the standard reflex for suppressing permission-denied noise, and it
suppresses this too.

The rule that existed reasoned about exactly this failure mode and closed one
axis of it. It said a refusal exits 2, never 1, because `grep` uses 1 for "no
match" and no caller may read a refusal as an empty result. That closes the
collision with *no match* and leaves two axes open: the collision with the
wrapped tool's own *error* exit, and the caller who never sees the message
because stderr went to `/dev/null`.

The stream was never a decision. No record in this corpus mentions stderr or
stdout, the exit-code rule never named a stream, and nothing in the shim's
comments, the docs or the ADRs argues why the refusal is stderr-only. It was
inherited from the wrapped tools, whose diagnostics belong on stderr because
their *output* is on stdout — and a refusal is not a diagnostic about output,
it is the entire result of the call.

The sharpest form of the problem is that the guard's own founding example is a
caller it cannot reach. ADR-0001 records the originating command verbatim, and
cites it to show a *tokenization* blind spot: a text-level guard sees one
opaque token and cannot look inside it. That same command carries
`2>/dev/null` **and** a pipe to `head`. Replayed against the shim, its refusal
was discarded by its own redirect and its exit code was laundered by the
pipeline, because `$?` after a pipeline is the last command's status. Two
independent blind spots in one command, and nothing in the tree connected the
second.

The defect was reported against a reference deployment and reproduces from the
tree alone; it is binary — refused versus ran, plus an exit code — so no
quantity is involved.

Five alternatives were considered.

**Move the refusal text from stderr to stdout.** Its strongest form is that one
stream is simpler than two, and that the caller who most needs the text is
exactly the scripted one reading stdout. Rejected: the text is a screenful of
guidance written for a person at a terminal, and moving it there puts it in the
byte stream a pipeline consumes, so `find … | wc -l` would count guidance.
Adding a channel keeps every existing reader whole; moving one trades a silent
failure for a corrupted one. It would also break
`test_refusal_offers_walk_job_for_every_refusal`.

**A distinct exit code alone.** Its strongest form is the real cost of the
other half: an exit code touches no byte stream, so no programmatic consumer
can be corrupted by it, and a caller that discards *both* streams still learns
something. Rejected as a complete remedy, kept as half of one: `$?` after a
pipeline is the last command's status unless the caller sets `pipefail` or
reads `PIPESTATUS[0]`, and ad hoc scripts and agent-run snippets do neither. A
new code is therefore invisible in precisely the piped shape that most
motivates it.

**Documentation only** — state prominently that stderr must not be discarded
when scripting against a shimmed tool. Its strongest form is that it changes no
contract and risks nothing. Rejected: a fully automated caller reads no
documentation, and the shape of caller this guard exists for is the one least
likely to have read anything.

**Exit code 3 rather than 77.** Its strongest form is that 3 is the first code
free above the search family's 0/1/2 — adjacent, small, memorable. Rejected
because adjacency is the whole problem. The claim this half needs is "no
wrapped tool claims this code", and the wrapped list cannot be audited
exhaustively against every site: a code one step outside the family's range is
the most likely place an as-yet-unchecked tool would have grown a meaning.
`ugrep` was the one wrapped tool whose vocabulary could not be checked where
this was first decided, for lack of a reachable binary; checked since, against
a real one: 0 on a match, 1 on no match, 2 on a bad flag or a bad path — the
same 0/1/>1 shape as the rest of the family, no collision with 77. That closes
the one concrete gap this record had, and it does not touch the argument
against adjacency, which was never about this one tool: another site's tool,
not yet checked anywhere, is still the case 77 is chosen to survive. 77 is
`EX_NOPERM` from `sysexits.h` — "was not allowed to look", which is what a
refusal is — and it sits outside every band a caller could confuse it with:
0/1/2 for search outcomes, 126 and 127 for the shell and for this shim's own
not-found exit, 128 and up for signals, and 255 for `ssh`'s own error, which
matters because the founding incident arrives over `ssh`. Nothing else in this
tree cites `sysexits.h`; the existing precedent reasons about codes claimed
within the same process, not about a wrapped tool's vocabulary, so this is new
reasoning and is argued here rather than assumed.

**Print the stdout line only when stdout is a terminal.** Its strongest form is
that it spares the programmatic consumer entirely while keeping the interactive
case. Rejected because it inverts the fix: the non-terminal caller is precisely
the one that cannot see the refusal, and a guard that goes quiet exactly when
nobody is watching is the defect this record exists to close.

## Decision

A refusal prints one line on stdout, immediately before the stderr group and
after its audit record, naming `walk-blocker`, the verdict, and where the
guidance went. The full refusal text stays on stderr and nothing moves: this
adds a channel.

A refusal exits 77, replacing 2. One constant, `EXIT_REFUSED`, stamped into the
shim at build time.

Both halves land together, because neither closes the defect alone: the stdout
line survives the pipeline that launders the exit code, and the exit code
survives the caller that discards both streams.

## Consequences

A programmatic consumer of a wrapped tool's stdout now receives one line that
is not a path. This is the real cost of the sentinel and it is accepted rather
than mitigated: on a refusal there is no other stdout, so a `while read -r f`
loop iterates exactly once on an obviously bogus line and the command exits
77 — where before it iterated zero times and exited a code the tool itself
could have produced. The failure becomes loud instead of silent, which is the
trade this record is making.

A caller that tested for exit 2 must be updated. There is one constant to
change and the suite compares against the symbol, but two tests were *named*
for the old reasoning and were renamed with it, and fifteen assertions across
`tests/test_shim_agrees.py` and `tests/test_shim_seams.py` had the literal
inlined rather than the symbol; they now use `R.EXIT_REFUSED`.

`grep`'s own use of 2 — for a bad flag, as recorded in `search_rules.py`'s
flag-parsing comments — is the collision that motivated this and remains
exactly where it was. What changed is that the refusal no longer shares it.

The other exit 2s in this repository are unrelated and stay: `walk-job`'s and
`measure.sh`'s usage errors, `validate`'s and `build`'s CLI failures,
`deploy.py`'s `argparse`, and the reaper's `blind` record (ADR-0009). This
record is about the shim's refusal and nothing else.

`fzf` and `sk` stay unwrapped, and the new channel does not reopen that: they
are reached through a keybinding or an editor plugin, where stdout is consumed
as the command line being edited, so a sentinel there would corrupt an edit
buffer rather than teach anyone. Not wrapping them remains the remedy.

The fork and open counts are unchanged. `printf` is a shell builtin, so the
sentinel starts no process and opens no file, and a refusal is off both counts
by construction (ADR-0018) — the fast path is never reached by a call that
refuses, and the audit path already pays four forks to write its record.
`tests/test_shim_agrees.py`'s
`test_a_refusal_runs_exactly_the_programs_the_audit_path_documents` pins the
program count, and now also asserts the sentinel is present in the traced
stdout, so one test proves both halves of the claim.

Record-before-emit is preserved: the sentinel is printed after
`sg_audit_emit refused`, which `tests/test_shim_seams.py`'s
`test_the_record_is_written_before_the_message` pins.

The two counts in `CLAUDE.md`'s fork-budget non-negotiable are untouched, and
`test_a_refusal_survives_having_no_sink_at_all` still requires the stderr text
to begin with `REFUSED`.

Adding a channel is the design; moving the text is not. A later agent that
proposes relocating the refusal to stdout should read the first rejected
alternative above.

## Re-measure when

n/a: structural. Nothing a site measures changes the answer. The one thing that
could is a wrapped tool that claims 77 for an outcome of its own: check the
`EXIT STATUS` section of a tool's manual when adding its name to the rule
table, and note that 0, 1 and 2 remain unavailable whatever that check finds.

## Site config touched

none.
