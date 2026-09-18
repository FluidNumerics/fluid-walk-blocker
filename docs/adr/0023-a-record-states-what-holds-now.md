# ADR-0023: A record states what holds now, and narrowed wording moves below it

**Status:** accepted, 2026-09-18
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

The corpus is append-only. `docs/adr/TEMPLATE.md` handles a correction by
leaving the old text exactly where it is and adding a line under the target's
Status saying which clause a later record narrowed. That is the right instinct
for provenance: it makes the history of what was claimed readable, and it stops
a record being quietly rewritten into agreement with whatever came later.

It reads badly, though, and it reads worst in the sections written to be
quoted. A reader meets the superseded sentence first, in `## Decision` or
`## Consequences`, and the correction second — or not at all, if they quote the
sentence without scrolling back to the Status block. Eight clauses across seven
records are in that state.

The live exhibit is ADR-0004. Its Decision still describes the install as gated
on root "plus explicit authorization (`--i-have-approval`), it self-executes".
ADR-0021 removed that flag. A reader who reaches ADR-0004's Decision and stops
there has read a command that does not exist, in the section most likely to be
copied.

This was raised on the pull request that removed the flag. Both moves available
at the time were bad: preserve the sentence verbatim, and a reader's first
encounter with the install command is a command that fails; or edit it in place,
which the convention forbids and which an earlier governance round had already
caught. That pull request settled one file — ADR-0008, where the superseded
command spelling was corrected and the old spelling recorded under Status — but
a per-file patch is not a rule, and the next narrowing recreates the problem.

Four alternatives were considered.

**Move superseded records to a `docs/adr/deprecated/` directory**, leaving the
live directory showing only what holds. Its strongest form is that a reader
scanning a directory should not have to open a file to learn it is historical.
It was rejected on the evidence: every narrowing in this corpus is clause-level,
and no record is wholly superseded. Externalising would retire seven records
that are substantially current because one clause in each was narrowed, and it
would break the dense numbering the corpus relies on.

**Leave the convention and keep patching per file**, as the flag-removal pull
request did. Its strongest form is that eight clauses is a small number and a
rule generalised from one case is a rule invented ahead of its evidence. It was
rejected because the cost is paid by every future narrowing, and because the
inconsistency is itself the defect: ADR-0008 already carries its superseded
wording in one shape while seven other records carry theirs in another.

**Replace `Narrows:` with a "superseded by" block inside the record it applies
to**, dropping the pointer from the narrowing record. Its strongest form is that
one mechanism is simpler than two. It was rejected because `Narrows:` is what
makes a narrowing discoverable from the record that performed it; deleting it
would mean a reader of the new decision cannot see what it changed without
searching the whole corpus.

**Reverse the corpus so the newest record is met first.** Rejected: later
records build on earlier ones, so ascending order is how the corpus actually
reads, and reversing it churns the reading-order prose without touching the
per-record problem this record exists to fix.

## Decision

A record's `## Decision` and `## Consequences` state what holds now. Where a
later record narrowed a clause there, the superseded wording moves verbatim into
a `## Superseded wording` section, placed after `## Site config touched` and
before the closing paragraph where a record has one, and tagged with the record
that narrowed it. The clause does not stay behind in the section it came from.

`## Context` is exempt. It is defined as what was true when the decision was
necessary, and its value is precisely that it was not updated; a Context a
reader cannot date is worth less than a Context carrying a sentence a later
record refined. A narrowed clause that lives in Context stays where it is, and
the Status note carries the correction.

`Narrows:` survives, and so does the reciprocal note under the target's Status.
The quote in a `Narrows:` line resolves against the target's
`## Superseded wording` section, not against its Decision or Consequences, and
`tests/test_adr_conventions.py` is what holds that to be true.

This record is the licence for editing the bodies of accepted records, and it
exists before any of them is edited. Each such edit is a verbatim move of the
superseded wording plus the minimal correction that makes the vacated sentence
true, and nothing else — no rewording, no re-wrapping, no tidying in passing.

## Consequences

- **ADR-0018 rejected "edit ADR-0015's Decision in place" and that ground still
  holds.** It cited `docs/adr/TEMPLATE.md`, and the reason underneath it — that
  the history of what was claimed stays readable — is honoured here rather than
  overturned. What changes is where that history lives: a named section in the
  same file instead of the sentence's original position. ADR-0018 is not
  narrowed by this record and its own wording is unchanged.
- **The narrows test stops being satisfiable by doing nothing.** It previously
  resolved a `Narrows:` quote anywhere after `## Context`, so it passed whether
  or not the clause had moved, and a record could carry the quote in two places
  at once. Resolving it inside `## Superseded wording` makes the assertion mean
  what its name says. Context-resident clauses are exempt by a named constant
  rather than by a loosened matcher, so the exemption is countable.
- **A reader of the one Context-resident clause still meets it before the
  correction.** That is the cost of the exemption above, accepted deliberately.
  It is not a defect awaiting a fix, and a later reviewer who re-raises it
  should be answered with this paragraph.
- **A "verbatim" move is not checkable by the test.** The comparison normalises
  whitespace and strips emphasis and code spans, so a move that silently
  re-wraps a line or drops a backtick passes. Review is the only thing that
  catches it, which is why the move is specified as verbatim rather than as
  faithful.
- **ADR-0005 carries the closing paragraph**, which `TEMPLATE.md` puts last and
  which is a citation target quoted verbatim elsewhere. The new section goes
  above it, and the closing paragraph stays the file's final words.
- **`docs/` ships verbatim into the payload**, so restructuring records moves
  lock digests. A rebuild is part of the change and `build --check` is the
  oracle.

## Re-measure when

n/a: structural. No property of a site bears on how this corpus is arranged.

## Site config touched

none
