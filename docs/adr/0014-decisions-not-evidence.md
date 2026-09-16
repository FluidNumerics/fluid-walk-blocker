# ADR-0014: The generic tree carries decisions, not site evidence

**Status:** accepted, 2026-09-16
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

The predecessor was work product for one customer, and it carried its
evidence inline. Its ADRs quoted mount censuses with capacities and inode
counts, timings taken on the login node, the partition and QoS the fallback
job ran under, user counts, vendor build strings, the dates of on-node
measurements, and the review rounds that produced each check. That was the
correct posture for a document whose only readers operated that node: the
number beside the decision is what made the decision checkable.

It is the wrong posture for a tree that more than one site will build from,
for three reasons.

The evidence belongs to the site. A capacity, an inode count or a partition
name is the customer's operational information; it describes their
infrastructure, not this tool. Generalizing the tool does not carry with it a
right to carry the customer's figures, and the disclosure boundary does not
move because the file sits in a different repository.

The evidence goes stale, and it goes stale silently. A figure measured at one
deployment on one day is true of that deployment on that day. Copied into a
generic ADR it reads as a property of the tool, and nothing in the tree can
tell a reader it is not.

The evidence displaces measurement. A second site that reads a ceiling
justified by a timing copies the ceiling instead of measuring its own; the
figure that justified one site's `maxdepth` becomes another site's default by
inertia. ADR-0007's method — stage a bounded walk, pre-register the criteria,
measure before raising — is worth more to a second site than ADR-0007's
result, and a record that carries the result invites skipping the method.

The alternative in its strongest form: keep the evidence inline and
generalize it — replace the hostname with "the login node", keep the figures
— because a decision without its number is unfalsifiable, and a reviewer
cannot tell a measured ceiling from a guessed one. It was rejected because the
figures are the disclosure, not the names; because a figure without its
conditions is not falsifiable either, only persuasive; and because the
falsifiability it wants is supplied by naming the class of measurement and the
procedure, which a second site can run, rather than the result, which it can
only copy.

## Decision

An ADR in this tree states the decision, the alternatives rejected in their
strongest form, and the consequences. Where a decision rests on a measurement,
it names the class of measurement, writes "measured at a reference
deployment", gives the condition under which a site should measure again and
the procedure for doing so, and names the `site.toml` key the result would
change. It carries no hostnames, usernames, uids, partition or QoS names,
customer, vendor or engineer names, neighbour timers, capacities, inode
counts, percentages, user or core counts, timings, vendor build strings, dates
of on-node measurements, or issue and review-round numbers. Filesystem type
names are vocabulary. The founding incident is described by its shape: an
automated agent ran an unbounded `find` over ssh against a very large network
filesystem; the client timed out and retried; the remote process ran for
days.

The evidence for each ADR is held privately by Fluid Numerics, keyed by ADR
number, and `docs/evidence.md` says what class of record is held for each and
how a site asks. A site that measures for itself keeps that record beside its
own `site.toml`, outside this tree. Fixtures, examples and `site.example.toml`
use fictional names. Issue and pull-request text describes shapes, not
identities.

A CI gate, `tools/check_no_site_literals.py`, enforces the absence of
literals. It must not itself contain the literals it hunts: structural
patterns — identifier prefixes, path shapes, figure-with-unit shapes — live in
the repository, and the customer term list is supplied to CI as a secret and
is never committed.

## Consequences

**ADRs read thinner, and that is correct.** A reader who wants the number asks
for it, citing the ADR, and receives it under the terms the customer set — or
measures. A request to "add more context" to an ADR is answered by citing this
record, not by adding the context.

**The method is what generalizes.** Every ADR that rests on a measurement has
a "Re-measure when" section that gives the procedure and the bar and never the
number. That section is the transferable part; a site that skips it and copies
a value has done what this record exists to prevent.

**The gate's coverage is partial by construction.** Structural patterns catch
shapes; the secret catches names; neither catches a figure written in prose
without a unit. Review still asks "did this diff put a site literal in the
tree" (`.github/copilot-instructions.md`), and the gate is a floor under review,
not a replacement for it.

**A record marked n/a has no evidence, and `docs/evidence.md` says so**,
rather than implying a file exists for every row.

**The predecessor is "the predecessor" or "a reference deployment"**, and
nothing about where or for whom it ran appears here.

## Re-measure when

n/a: structural.

## Site config touched

none
