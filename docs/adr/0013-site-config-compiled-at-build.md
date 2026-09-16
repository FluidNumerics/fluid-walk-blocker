# ADR-0013: Site configuration is compiled at build time, not read at run time

**Status:** accepted, 2026-09-16
**Narrows:** ADR-0005, "a future location change is a commit, not a flag"
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

The predecessor was built for one site, and its site facts were module
constants: the mount paths, the install prefix, the audit path, the hook file,
the partition and QoS the fallback job used, the trusted binary paths.
ADR-0005 removed the CLI flags that had shadowed four of those constants, on
the argument that every one of them is a root-write sink, and that a value
reaching a systemd unit line or a sourced shell block needs shape validation
that grows as the product of sinks and interpolation contexts. Its closing
clause was that "a future location change is a commit, not a flag".

A generic tool cannot keep site facts as constants in its own source, because
then every site is a fork. So they move to `site.toml`, validated by
`schema/site.schema.json`. The question this record settles is *when* that
file is read: on the workstation, at build time, or on the node, at run time.

Three consumers on the node cannot read it at run time, each for a different
reason.

- **The shim** is POSIX `sh` on the hot path of every `grep`, `find` and `du`
  on the node. Before it decides, it forks nothing and opens one file,
  `/proc/mounts` (ADR-0015). Parsing TOML in `sh` without a fork is not
  possible, and parsing it with one puts an interpreter start in front of
  every search on the node. A shim that reads a configuration file has
  abandoned the constraint that justified writing it in `sh`.
- **The reaper** is stdlib Python 3.9 (ADR-0015). There is no `tomllib`
  before 3.11, and vendoring a parser into a root-run timer is the dependency
  the node runtime rule exists to forbid. It could read JSON — see the
  rejected alternative below.
- **`deploy.py`** writes as root into unit lines, a sourced shell block and a
  recursive filesystem operation. ADR-0005 established that every value
  reaching those sinks must be a literal, so that its shape can be asserted
  once in a test rather than validated on every run. A runtime configuration
  file is one more root-write sink; it is one more parse-failure mode, at run
  time, forced to choose between failing open and failing closed; and if it is
  addressable by environment or by path it is exactly the unprivileged
  override surface ADR-0004 closed.

The alternative in its strongest form: build one artifact for every site, and
have the node read a root-owned JSON file that `deploy.py` installs under the
prefix. JSON is in the 3.9 stdlib; the file is root-owned and world-readable,
so the override surface stays closed; the build validates it, so the node
never sees a malformed one; and one artifact means one build, one version, one
thing to sign. It was rejected on four counts. The shim still cannot read it,
so the mount table would exist twice — once compiled into the shim and once in
the JSON the reaper reads — and the two consumers disagreeing on a mount is
the condition this repository treats as worse than a defect in either. A
build-validated file can still be edited, truncated or replaced on the node,
so the reaper keeps a parse-failure branch, and there is no right answer for
it: an empty mount list is a clean bill of health, and a refusal to scan is
the backstop gone. The hook files, wrapped tool names and unit files differ
per site anyway, so the artifact set is per-site regardless and "one artifact"
is not gained. And the property the alternative was really after — knowing
which configuration a node is running — is delivered by the lock file below
without a runtime reader.

A second alternative, keeping the constants and asking each site to edit them,
is the fork described above and was not seriously considered.

## Decision

`walk-blocker build` reads `site.toml` on the workstation, validates it
against `schema/site.schema.json`, and emits the node artifacts with every
site value baked in as a literal: whole-file `@@KEY@@` templates for
`shim/guard.sh` and `shim/wrapped_names.sh`, and stamped constant lines marked
`# GENERATED from site.toml:<key>` or `# GENERATED from VERSION` in
`reaper.py`, `deploy.py`, `shim/install.sh` and `walk-job`. No code that runs
on the node opens a configuration file.

The schema carries ADR-0005's shape rules as patterns, so a value is refused
at build rather than at root: absolute, at least two components, and no
whitespace, `%` or `$` for values that reach a unit line; no shell
metacharacters for values that reach the sourced block. One test asserts the
same rules over the compiled literals, so the schema and the generator cannot
drift silently. `walk-blocker build --check` rebuilds into a scratch directory
and fails when the checked-in artifacts differ; CI runs it, so a stale
artifact is a red build, not a stale node.

The payload carries `site.toml` and `site.lock.json` — schema version, tool
version, `VERSION`, the config hash and a hash per emitted file; no timestamp
and no hostname, so two builds of the same inputs are byte-identical — as a
record, not as an input. The node can be asked which configuration is
deployed and the answer checked against a commit. The artifacts checked in
under `examples/payload/` are compiled from `site.example.toml`, a fictional
site.

## Consequences

**A site value change is a rebuild and a redeploy.** ADR-0005 said a location
change is a commit; it is now a `site.toml` change plus a build, and the
review that ADR wanted for a root-run installer's write targets still happens,
in the site's own repository beside its own configuration. The cost is
deliberate: nothing about where root writes can change without a diff someone
read.

**The node has no configuration parser**, so it has no parse failure to
handle and no choice to make between failing open and failing closed. The
only files node code opens for policy are `/proc/mounts` (the shim) and
`/proc` and the cgroup tree (the reaper).

**The runtime `WALK_BLOCKER_*` seams stay test seams.** `_MOUNTS`, `_FSTYPES`,
`_DEPTH_BY_MOUNT` and `_SHIM_DIR` exist so the suite can drive the shim
against a fixture mount table without a real one; `_UNSCOPED` is the
documented escape hatch of an advisory guard (ADR-0001). A test asserts that
the set of names the shim reads is exactly this list, so a new one is a test
failure and a decision, and none of them gains a sibling that carries a site
value. What is audited for divergence is the rule table against the generated
shim, not the environment.

**`deploy.py` stays argumentless** (ADR-0005). Its constants are now stamped
rather than hand-edited, which removes the one remaining way to change a
root-write target without the build seeing it.

**`examples/payload/` is a build product and is never edited by hand.** A
finding about it is a finding about `search_rules.py`, a template, the schema
or `site.example.toml`; `--check` is the oracle.

## Re-measure when

n/a: structural. Nothing about a site changes whether its node should parse
configuration.

## Site config touched

All of `site.toml`. Every key is compiled, and this record is the reason none
is read anywhere else.

**This is not to be "fixed" by a later agent.** The question is settled here
because it kept coming back. If it is revisited, revisit it with a new ADR
that names the clause above it narrows, and with a measurement that
contradicts this record — not with an implementation.
