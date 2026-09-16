# ADR-0016: A mount's class defaults from remoteness, is overridden per mount in site config, and is measured only out of band

**Status:** accepted, 2026-09-16
**Narrows:** ADR-0007, "the shim keys on filesystem type"
**Evidence:** n/a, design discussion — see `docs/evidence.md`

## Context

ADR-0007 established that the shim decides which mounts are expensive by
filesystem type, that a per-mount depth ceiling is site configuration rather
than a derived value, and that `walk-job` is the general answer for anything
the ceiling refuses. It also recorded the first hardcoded path in the guard's
decision, and kept it acceptable on the grounds that it was data beside the
threshold it overrode and that its absence was safe. In a generic tree that
path becomes a `site.toml` entry, and the question this record settles is
what the rule is for a mount that has no entry.

Filesystem type is a weak proxy for cost. Two mounts of one type can differ by
orders of magnitude in inode count — ADR-0007's own reason for the per-mount
ceiling — and the difference runs the other way too: a small NFS export is
cheap to walk and a large one is not, and nothing in the string `nfs4` says
which. A type list is nonetheless the right *default*, because it is the only
signal available to a shim that reads one file, and it fails in the safe
direction: it guards a small remote mount, at the cost of a false refusal that
names `walk-job`, rather than allowing a walk of a large one.

The shim cannot measure. Three ways of deriving a mount's class at run time
were on the table, and each fails for a reason that does not go away.

- **`statfs` on the mount** answers the size question exactly, and answers it
  by blocking on the filesystem being judged, at precisely the moment the
  guard matters (ADR-0015). Network and parallel filesystems also report
  synthetic totals — a namespace quota, a tiered capacity, a per-client view —
  so the number is not reliably the number wanted even when the call returns.
- **Server congestion**, in any form — a latency probe, a client-side pressure
  reading, a queue depth — is dynamic. A verdict keyed on it changes between
  two invocations of the same command, which makes it untestable and, worse,
  teaches people that a refusal is something to retry until it passes. The
  founding incident is a client that retried.
- **An interactive prompt at install time**, asking the administrator to
  confirm the mount list, produces a decision that exists only in a terminal
  session: unreviewable, unrepeatable, and in direct contradiction of
  ADR-0005's position that a root-run installer's inputs are a diff someone
  read.

The alternative in its strongest form: key the default on type alone, as
ADR-0007 did, and treat anything not on the list as cheap — because a type
list is exhaustive for the filesystems a site actually deploys, an unknown
type is far more likely to be a `tmpfs` or a container overlay than a
petabyte store, and guarding unknowns produces a false refusal on every new
local mount. It was rejected because the *remote* unknown is the case that
matters: a new parallel filesystem type, a vendor's renamed client, a FUSE
gateway in front of an object store — each appears in the mount table with a
type no list anticipated, and each is exactly the resource an unbounded walk
hurts. Local unknowns are separated from remote ones by the remoteness test,
not by the type list, so the false-refusal cost the argument fears does not
arise for them.

## Decision

A mount's class is decided in three tiers, and only the first two run on the
node.

**Tier one: a compiled default from `/proc/mounts` fields alone.** A mount is
expensive by default when its type matches `[filesystems].remote_fstypes` — a
broad, glob-aware list of network and parallel filesystem types — or, when
`[filesystems].remote_proxy` is true, when its source matches `host:` or its
options contain `_netdev` or `addr=`. A remote mount whose type matches
nothing is guarded: the shim fails toward refusal, and the refusal names
`walk-job`. A local mount of an unknown type is cheap.
`classify_mount(entry, policy)` is one function in the rule table, generated
into the shim, and a test asserts that the two consumers agree on every row of
the fixture mount table, branch by branch.

**Tier two: per-mount overrides in `[[filesystems.mounts]]`**, each with a
`path`, a `class` of `expensive` or `cheap`, and an optional `maxdepth` that
replaces `[filesystems].maxdepth_allowed` for that mount, bounded above by
`depth_allowance_max`. Overrides are compiled (ADR-0013) and root-owned on the
node, and they are the only way to loosen: a small remote export is made cheap
here, in a diff, and nowhere else.

**Tier three: `walk-blocker survey`**, run by an administrator on the node and
shipped in the payload as `survey.py`. It takes a timeout-bounded `statfs` per
mount, each in its own subprocess so that one wedged mount cannot hang the
survey; prints type, remoteness, the tier-one default, capacity and inode
count; and proposes a `[[filesystems.mounts]]` block for the administrator to
review. It never writes configuration. The reconcile logs one journald line
per mount in the live table that no override covers, so a mount running on its
default is visible without anyone running the survey.

## Consequences

**A new resource is guarded the moment it appears in the live mount table**,
with no rebuild: tier one runs against `/proc/mounts` as it is, and an
unlisted remote mount is expensive until a diff says otherwise. This is the
deliberate asymmetry — the default is toward refusal, and the refusal names an
alternative.

**A false refusal on a small remote mount is a configuration omission**, fixed
by a `[[filesystems.mounts]]` override that is reviewed in git and shipped by
a build. It is not a reason to narrow `remote_fstypes` or turn `remote_proxy`
off, because both of those loosen every mount at once to fix one.

**Congestion is at most a Layer 2 corroborating field**, alongside PSI
(ADR-0009), and never a Layer 1 gate. The shim's verdict for a given argv, cwd
and mount table is a pure function, and the agreement test depends on that.

**The exact-path override table is data beside the threshold it overrides**,
as ADR-0007 required, not a branch in the logic. `classify_mount` reads the
policy; it does not contain a path. A proposal to add a path to the rule table
is a proposal to add a row to `site.toml`.

**Both consumers must agree on every mount-table row**, and the test that pins
it enumerates the branches: listed type, remote by source, remote by option,
local unknown type, cheap override, expensive override with `maxdepth`. A
change to `classify_mount` that does not touch that test has not been made in
both consumers.

**The survey is advisory and out of band.** A site can decline to run it and
still be guarded, at the cost of false refusals it has chosen not to
investigate; the reconcile's per-mount line is what makes that choice visible.

## Re-measure when

- The reconcile reports a mount no override covers. Run `walk-blocker survey`
  on the node, read the proposed block, and decide per mount. A mount left on
  its default should be left there on purpose, and the survey output kept
  beside `site.toml` is what says so.
- A site disputes a default in either direction. The survey gives type,
  remoteness, capacity and inode count; the decision is the site's, and the
  record is the site's.
- Before raising a `maxdepth`: ADR-0007's method. Stage a depth-bounded walk
  off the login node, one root at a time, capped and paused, with
  pre-registered criteria, starting from the depth already permitted. A walk
  at the next depth is a superset of the walk at the current one, so a
  baseline that already fails the criteria settles the question without
  running the deeper walk. Measuring the thing must not cause the thing.

## Site config touched

- `[filesystems].remote_fstypes`
- `[filesystems].remote_proxy`
- `[[filesystems.mounts]]` — `path`, `class`, `maxdepth`
- `[filesystems].maxdepth_allowed` and `[filesystems].depth_allowance_max`, as
  the bounds a `maxdepth` override sits between
