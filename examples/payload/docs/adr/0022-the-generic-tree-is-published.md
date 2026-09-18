# ADR-0022: The generic tree is published, and the term half of the gate runs only where the terms are

**Status:** accepted, 2026-09-18
**Evidence:** held privately by Fluid Numerics, keyed ADR-0022 — see `docs/evidence.md`

## Context

This tree was built to be publishable. ADR-0014 put the site's evidence outside
it, `tools/check_no_site_literals.py` made the absence checkable, and every
example, fixture and rendered page uses fictional values. Whether the repository
*could* be opened was answered by its design long before anyone asked. What was
open was whether it should be, and what publication changes about the rules that
kept it clean.

Three things decided it. A site's own repository needs to read this one to
validate its `site.toml` against the schema and to rebuild its payload; while
this tree is private that read costs a credential, and a credential in the path
of a gate is a gate that fails silent when the credential lapses. The customer
whose incident prompted this work should be able to read the thing that now runs
on their login node. And a shop that publishes the reasoning behind its
engineering is making a claim about that reasoning that a closed repository
cannot make.

Three alternatives were on the table.

**Publish a snapshot with no history.** Its strongest form is that history is
exactly where a leak hides — superseded drafts, review rounds, force-pushed
branches — and a single squashed commit is provably clean because there is
nothing behind it to audit. It was rejected because the record is a large part
of what is worth publishing. The decisions in `docs/adr/` are legible mostly
through the rounds that produced them, and discarding that to avoid an audit
trades away the thing being published for the convenience of publishing it. The
audit is finite, and it was run.

**Stay private and issue a read-only deploy key to the site's repository.** Its
strongest form is that it solves the concrete mechanical problem — one repository
needs to read another — with no disclosure whatsoever, and deploy keys are the
purpose-built answer to exactly that. It was rejected because it renews the
failure it is meant to fix. A scoped credential expires, is rotated, or is never
installed at all, and a validation job that cannot authenticate does not
typically fail; it skips, and reports green for skipping. Publication deletes the
credential rather than scheduling its renewal.

**Publish, and keep the term gate failing closed on every run.** Its strongest
form is that a gate which can be skipped is not a gate: make the customer-term
scan conditional and eventually a contribution merges that the scan never read.
It was rejected only in part, because it is right about this repository and wrong
about a fork. A fork holds no customer list, has nothing of this customer's to
protect, and cannot be given the secret by any mechanism the forge offers. Failing
its CI over a secret it neither has nor needs teaches its maintainer that a red
gate is normal, which is how a real finding gets waved through later. The
distinction that matters is not fork-versus-not; it is whether a run can reach a
protected branch here.

## Decision

The repository is published. The licence is unchanged. Site facts stay out under
ADR-0014's rule, which is not relaxed and whose cost is now higher: a literal
that reaches this tree is public permanently.

The gate splits along the line between what is committed and what is secret. The
structural half runs on every run in every repository, needs no secret, and fails
the build on a finding. The customer-term half runs only where the term list can
exist. It is skipped, quietly and by a job-level condition, when the repository
running it is not this one. When it runs here against a pull request whose head
is a fork, the forge withholds the secret and the job fails, naming that as the
reason; a maintainer re-runs the scan from a trusted ref before such a pull
request merges.

Before publication the repository's whole object graph was audited: every commit
reachable from the default branch and from every pull-request ref, and every
unreachable object the forge still served. One orphaned object, superseded by an
amendment before its branch merged, carried two of the customer's values in a
test fixture. Removing it needs the forge's own collection, because it is
reachable from no ref a clone controls; that was requested and had not completed
when the repository was published.

Publication did not wait for it. The owner judged the two values non-identifying
in isolation — neither names a customer, host, site or person — and reachable
only by exact object id, and weighed that against holding the release open on a
third party's queue. The request stands; whether and when it is actioned is the
forge's to decide, and this record does not promise an outcome it does not
control. The specifics are held privately, keyed to this record. A published ADR
that named the object would be a map to it.

## Consequences

- **A miss is permanent.** The gate stays a hard failure rather than a warning,
  the term list stays uncommitted, and a customer-term hit stays unwaivable. The
  one-line waiver remains available to structural patterns only, exactly as
  before.
- **A skipped job reports as a passing required check.** That forge behaviour is
  why the fork pull-request case fails loudly instead of skipping: a quiet skip
  plus a required status check is a green merge button over a scan that never
  ran, which is the precise failure this split exists to prevent. The job-level
  condition is therefore safe only for the not-this-repository case, where no
  protected branch sits downstream of it.
- **A force-push during review now orphans objects in public.** While the
  repository was private, an object stranded by an amendment was recoverable
  privately and purgeable before anyone outside could reach it. That grace is
  spent. The defences that remain run *before* the push — the pre-commit hook and
  the gate — because an after-the-fact collection on a public repository is a
  request to a third party rather than something this side can carry out, and it
  runs on their schedule, not the release's.
- **`git filter-repo` was the wrong instrument, and why is the durable part.** It
  rewrites what is reachable from refs the local clone controls. An object
  orphaned by an amendment and a force-push before merge is reachable from no such
  ref and is retained by the forge independently of them. A local rewrite would
  have reported success and removed nothing. The instrument for an object the
  forge holds outside every ref is the forge's own collection, which is verified
  by fetching the object afterwards and finding it gone. That verification is
  the step this record's own object is still waiting on.
- **The site's repository can stop holding a credential for this one.** Once
  publication lands, its cross-repository checkout needs no token and its
  schema-validation job can run unconditionally rather than skipping when a
  secret is absent, which turns a gate-shaped report back into a gate. That
  change belongs to that repository and lands there, after publication, not
  with this record.
- **Review still reads the diff.** ADR-0014's "the gate's coverage is partial by
  construction" is unchanged and matters more: structural patterns catch shapes
  and the secret catches names, and a site fact with neither shape nor listed term
  still passes both halves.

## Re-measure when

n/a: structural. No property of any site bears on whether this tree is published.

## Site config touched

none

## Superseded wording

Corrected 2026-09-18, after the repository was published. The Decision above
read:

> It was purged before the repository was made public, and the purge was
> verified rather than assumed.

That described the sequence as planned, not the one that happened: publication
went ahead with the collection still outstanding, deliberately and on the
owner's judgement. The sentence is recorded here because a decision record that
quietly acquires a true sentence in place of a false one teaches a reader the
record was right all along.
