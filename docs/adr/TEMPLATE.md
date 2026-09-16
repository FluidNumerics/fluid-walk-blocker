# ADR-NNNN: <the decision, as one sentence>

**Status:** accepted, YYYY-MM-DD
**Narrows:** ADR-XXXX, "<the clause, quoted>" — *omit this line unless this record changes an earlier one*
**Evidence:** held privately by Fluid Numerics, keyed ADR-NNNN — see `docs/evidence.md`

<!--
Conventions (delete this comment in a real ADR):

- The title is the decision, not the topic. "Read PSI at the user slice" is a
  title; "PSI sampling" is not.
- A new ADR never edits an old one's Decision. It narrows it by naming the
  clause, in the **Narrows:** line and again in Context. The old ADR gets one
  line added under its Status saying which clause is narrowed and by what.
- Decisions, not evidence (ADR-0014). No hostnames, usernames, uids,
  partition or QoS names, customer or engineer names, neighbour timers,
  inode counts, capacities, percentages, user or core counts, vendor build
  strings, or dates of on-node measurements. Where a decision rests on a
  measurement, say the class of measurement and write "measured at a
  reference deployment", then fill in "Re-measure when".
- Filesystem type names (wekafs, nfs4, lustre, gpfs) are vocabulary and fine.
- A **Narrows:** line quotes a full clause of the target's body, not a
  fragment; `tests/test_adr_conventions.py` checks the quote resolves.
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
Include the closing paragraph below only where the decision was settled
against repeated review pressure — the same proposal raised more than once,
each time with sound reasoning, and each time not the owner's conclusion.
Keep it verbatim. It is a citation target for the architect agent.
-->

**This is not to be "fixed" by a later agent.** The question is settled here
because it kept coming back. If it is revisited, revisit it with a new ADR
that names the clause above it narrows, and with a measurement that
contradicts this record — not with an implementation.
