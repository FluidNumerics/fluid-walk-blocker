# ADR-NNNN: <the decision, as one sentence>

**Status:** accepted, YYYY-MM-DD
**Narrows:** ADR-XXXX, "<the clause, quoted>" — *omit this line unless this record changes an earlier one*
**Evidence:** held privately by Fluid Numerics, keyed ADR-NNNN — see `docs/evidence.md`

<!--
Conventions (delete this comment in a real ADR):

- The title is the decision, not the topic. "Read PSI at the user slice" is a
  title; "PSI sampling" is not.
- A new ADR narrows an old one by naming the clause, in the **Narrows:** line
  and again in Context. The old ADR gets one line under its Status saying which
  clause is narrowed and by what, and the superseded wording moves out of the
  section a reader quotes and into that record's `## Superseded wording`
  section, so its Decision and Consequences state what holds now (ADR-0023).
  Two things do not move: a narrowing that clarifies how a clause should be
  read rather than replacing what it says, and a clause in `## Context`, which
  is what was true then and is not updated.
- An issue's decision may amend an accepted record without a record of its
  own, by the same move: the wording goes to `## Superseded wording`, tagged
  "Amended when issue #N was decided.", and a line under Status names the
  clause, the issue, and that the replaced wording is recorded there
  (ADR-0023).
- The sentence under `## Superseded wording` that says what holds instead is
  live, so when it stops being true it is corrected in place: the quotes are
  not touched, nothing moves, and a line under Status names the entry and the
  issue (ADR-0023).
- An edit that only adds a sentence or bullet, replacing nothing, moves
  nothing: a line under Status names what was added, the issue or pull
  request it came from, and that nothing moved, without the words
  "amended when issue #N was decided" or "corrected" (ADR-0023).
- Decisions, not evidence (ADR-0014). No hostnames, usernames, uids,
  partition or QoS names, customer or engineer names, neighbour timers,
  inode counts, capacities, percentages, user or core counts, vendor build
  strings, or dates of on-node measurements. Where a decision rests on a
  measurement, say the class of measurement and write "measured at a
  reference deployment", then fill in "Re-measure when".
- Filesystem type names (wekafs, nfs4, lustre, gpfs) are vocabulary and fine.
- A **Narrows:** line quotes a full clause, not a fragment, and the quote
  resolves inside the target's `## Superseded wording` section;
  `tests/test_adr_conventions.py` checks it and names the exempt narrowings.
- Adding an ADR, or giving one the closing paragraph, means updating the
  expected count and the settled set in `tests/test_adr_conventions.py`;
  its failure message says which.
- Quote a rejected alternative's strongest form. A strawman is re-proposed.
-->

## Context

What was true that made a decision necessary. Name the alternatives that were
on the table and why each was rejected. Where a fact came from a measurement,
say what was measured and at what class of deployment, not the figure.

## Decision

The decision, in the imperative or the indicative, and nothing else. If it
takes more than three paragraphs, it is two decisions.

## Consequences

What follows, including the costs. Every "do not fix this into agreement" and
every deliberate asymmetry goes here, with the test that pins it if one exists.

## Re-measure when

The condition under which a site should take the measurement again, and how —
or "n/a: structural" when nothing about a site can change the answer. Never
give the number; give the procedure and the bar.

## Site config touched

The `site.toml` keys this decision reads or that a re-measurement would change,
as a list. "none" is a valid answer and must be written.

<!--
Include the section below only where a later record narrowed wording in THIS
record, or an issue's decision amended it. Omit it entirely otherwise -- most
records never have one. It sits below `## Site config touched` and above the
closing paragraph, which stays the record's last words (ADR-0023). An
amendment on an issue decision is tagged "Amended when issue #N was decided."
instead, and the Status block gains the line naming the clause and the issue.

## Superseded wording

Narrowed by ADR-XXXX. The Decision above read:

> the superseded clause, quoted verbatim and moved out of the section it
> came from, not copied

What holds instead, in a sentence.
-->

<!--
Include the closing paragraph below only where the decision was settled
against repeated review pressure — the same proposal raised more than once,
each time with sound reasoning, and each time not the owner's conclusion.
Keep it verbatim. It is a citation target for the architect agent.
-->

**This is not to be "fixed" by a later agent.** The question is settled here
because it kept coming back. If it is revisited, revisit it with a new ADR
that names the clause above it narrows, and with a measurement that
contradicts this record — not with an implementation.
