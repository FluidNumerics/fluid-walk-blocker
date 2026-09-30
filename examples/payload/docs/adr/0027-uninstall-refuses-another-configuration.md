# ADR-0027: The uninstall refuses an install that was not built from its own configuration

**Status:** accepted, 2026-09-30
**Narrows:** ADR-0013, "The payload carries `site.toml` and `site.lock.json` — schema version, tool version, `VERSION`, the config hash and a hash per emitted file; no timestamp and no hostname, so two builds of the same inputs are byte-identical — as a record, not as an input."
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

`deploy.py --uninstall` tears down the paths compiled into the payload it runs
from: the prefix, the two units in `[install].unit_dir`, the journal drop-in in
`[install].tmpfiles_dir`, and the hook blocks in every `[hooks.<shell>].file`
(ADR-0005, ADR-0013). Its one check that the prefix holds the install the
operator means was the payload marker, `<prefix>/.walk-blocker-payload`. The
marker says walk-blocker installed that directory. It does not say from which
configuration.

So a payload built from a different `site.toml` that names the same prefix
passes every check and tears down the install with its own paths. Those paths
are the install's only when the two configurations agree on them. Where they
differ, the teardown strips hook blocks from files the install never wrote,
misses the drop-in it did write and leaves that drop-in's journal grant in
place, and removes unit files from a directory that may hold none.
`not_a_default()` cannot see this: it compares the arguments against the
running build's own literals, so it never fires for a payload that was simply
built from something else. PR #83 documented a manual `site_sha256`
comparison for the operator to make before uninstalling (#85). A manual check
that the code could make is a check left to memory.

ADR-0013 records `site.toml` and `site.lock.json` in the payload "as a record,
not as an input", and CLAUDE.md states that nothing the node-side read of the
record finds reaches "a decision, a path, a threshold or an exit code
anywhere else". A refusal is an exit code, so the check cannot be made
without narrowing that clause. ADR-0013 is settled against review, and its
closing paragraph asks that a revisit bring evidence that contradicts it. The
evidence here is structural rather than measured: the teardown's write
targets come from the payload, and whether they match the install is a fact
about the install that only the installed record can answer.

Three ways to make the comparison were on the table.

- **Read the running payload's own `site.lock.json`** and compare it with the
  installed one, as #85 first suggested. The payload is a directory its owner
  can rewrite after the operator starts, and `stage_payload()` reads it once,
  early, for exactly that reason (ADR-0006). A lock read at uninstall is a late
  read of user-owned bytes, and the check would be one its owner could steer.
- **Compare against the installed `site.lock.json`'s `site_sha256`.** This
  needs a JSON parse on the node, and a lock that does not parse needs an
  answer of its own. It also checks the record rather than what the record
  describes: a lock that disagrees with its `site.toml` is the condition
  `--verify` exists to report.
- **Write the digest into the payload marker** at install and compare the
  marker's content. This changes the marker's format and `--verify`'s check on
  it, and it leaves every marker already installed without the field, so each
  existing install would have to be special-cased.

## Decision

`deploy.py --uninstall` refuses, with exit 6 and before it issues a single
command, unless the installed `<prefix>/site.toml` hashes to the
`SITE_SHA256` compiled into the running build. `walk-blocker build` stamps
that constant from the same bytes, through the same function, that it
records as the lock's `site_sha256`, on a line marked
`# GENERATED from SITE_SHA256`. The uninstall opens the installed file only to
hash it. It never parses it, and no value in it reaches a path, a threshold
or a branch other than this refusal. The comparand is the installed file,
because the running build's constant is part of the program already loaded,
and the payload is read once, early (ADR-0006).

A `site.toml` that is missing, a symlink, not a regular file, not
root-owned or unreadable refuses in the same way, because an install that
cannot show its configuration cannot be shown to be this one. The dry run
makes the same comparison and reaches the same answer. The build's `VERSION`
is not compared: an uninstall run from a newer build of the same
configuration is supported. There is no override. The refusal guards against
the wrong payload, not against root. It prints the payload's digest, and the
installed one when there is one to print. The recovery on a mismatch is a
build of the installed configuration: `walk-blocker provenance --sha256`
names the reviewed commit to rebuild, or a copy of the installed
`site.toml` builds a payload whose digest matches by construction. An install
whose `site.toml` is missing or cannot vouch for it has no digest to rebuild
from, so for that case the recovery is to re-run the install from its
payload, which rewrites `site.toml` and is never refused, and then to
uninstall from the same payload.

The install is unchanged. Installing a different configuration over the same
prefix is the redeploy, and it is never refused. One install per node is the
supported shape: the timer and service have fixed names, so a second install
replaces the first's units rather than running beside them.

## Consequences

**The record reaches one exit code, and only this one.** `--verify` still
hashes the installed tree against the installed lock and decides nothing
elsewhere. The uninstall's hash reaches exit 6, before anything is touched.
Neither read parses configuration on the node, neither is moved into
`reaper.py` or `guard.sh`, and neither runs on a poll. A second consequence of
the record, anywhere, is a new ADR.

**Identity is bytes.** A comment-only edit to `site.toml` is a different
configuration: it changes `site_sha256`, which is what `provenance` searches
for, and the refusal uses the same identity. An operator who edited the
site's comments since the install rebuilds from the installed bytes, which the
refusal names.

**The installed `site.toml` must stay a verbatim copy of the build input.**
Nothing may rewrite it at install. A root hand-edit on the node makes the
uninstall refuse, and `--verify` reports the same file as `differs`.

**What it establishes, and what it does not.** The check proves which
configuration the prefix holds. It does not prove who wrote the units in
`unit_dir`: under one install per node that is the same install, and the
uninstall still stops `walk-blocker.timer` by its fixed name. Supporting two
installs on one node would need unit ownership checked as well, and is a new
decision.

**Existing installs are covered.** Every released version installs
`site.toml`, so an install made before this record can be uninstalled by a
build of its own configuration from this release on.

The cases are pinned in `tests/test_deploy.py`, one per way the installed file
can fail to vouch for the install, each asserting that no command ran. The
suite derives the digest from `site.example.toml` rather than writing it
down.

## Re-measure when

n/a: structural. Nothing about a site changes whether a teardown should
confirm that it is undoing its own configuration.

## Site config touched

None read on the node. All of `site.toml`, as bytes: its digest is the
constant the build stamps and the uninstall compares.
