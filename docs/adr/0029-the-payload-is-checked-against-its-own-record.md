# ADR-0029: The dry run and the install check the payload against its own record

**Status:** accepted, 2026-10-01
**Narrows:** ADR-0027, "The record reaches one exit code, and only this one."
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

`deploy.py --system --dry-run` never read the payload it would install. It
made every check `preflight()` makes, and those are all about the
destination: the root-write paths, the spool, the prefix and the staging
parent. A dry run takes no snapshot, so it never opened a payload file, and
it printed a root command to paste over a payload it had not looked at.

A runbook that copies a payload to the node, dry-runs it and then installs
expects the dry run to fail on anything the install would fail on. An
interrupted `scp` or `rsync` does not trip any of the destination checks.
Before this record, a bad copy had three outcomes, and the dry run predicted
none of them:

- **A missing entry exits 1**, passed through from the `install` or `cp`
  that builds the snapshot. That is before the first `systemctl`, but it is
  not the exit any refusal uses, and the dry run had shown a clean plan.
- **A directory entry that is a symlink exits 5**, after the timer has been
  disarmed and the payload entries under the prefix removed. `cp -a` copies
  the link as a link, and only the check before the recursive `chmod`
  catches it.
- **A truncated file installs, exit 0.** Nothing between the copy and the
  enable reads what a file holds.

The bytes a payload should hold are already written down beside them.
`walk-blocker build` writes `site.lock.json`, a sha256 for every file it
emits, and `--verify` already compares an installed tree against that
record. Checking a payload against it, before anything is touched, is a
check the code can make.

ADR-0027 records the clause this narrows: "The record reaches one exit code,
and only this one." That record also rejected, as settled, a comparison
against the payload's own `site.lock.json` for the uninstall: "A lock read
at uninstall is a late read of user-owned bytes, and the check would be one
its owner could steer." The architect's settled table carries the same
ruling. That rejection does not reach this check, for four reasons.

- **That rejection was about identity; this check is about integrity.** The
  uninstall asks which configuration an install holds, and a payload-owned
  lock is not a source its owner cannot choose. This check asks whether a
  copy arrived whole. Identity still comes from the program already loaded:
  the lock must name the `SITE_SHA256` and `__version__` compiled into
  `deploy.py`, and a lock that names anything else is a refusal, not a new
  answer.
- **The dry run writes nothing.** A lock its owner steered can only change
  what the dry run says. It cannot change what the install does, and the
  install makes its own check.
- **The install reads the root-only snapshot, so the payload is still read
  once, early.** The install checks the snapshot it is about to copy from,
  after `stage_payload()` has taken it and before the first `systemctl`. It
  never goes back to the payload directory, which would be a second, later
  read of user-owned bytes (ADR-0006). The bytes it checks are the bytes it
  installs.
- **The check catches accident, not the payload's owner.** Per ADR-0006,
  that owner can rewrite the files, the lock and `deploy.py` together, and a
  check shipped inside the payload cannot detect that. This is not a control
  against them, and nothing here describes it as one.

Three alternatives were on the table.

**Narrow the documented claim instead**, so a runbook stops leaning on the
dry run for source completeness. Its strongest form is that it costs no
code and keeps the record at one consequence. It was considered and not
taken: a dry run that cannot see a truncated copy leaves the failure in the
one place an operator cannot stop to investigate, the root install inside a
quiet window between two timer slots.

**Check the payload directory in the install as well as in the dry run.**
One subject for both would be simpler. It was rejected because it is the
late read ADR-0006 closed: the snapshot exists so that nothing after it reads
the payload directory.

**Reuse `--verify`'s hashing.** `_sha256_file()` opens with a plain `open`,
so it follows a symlink and blocks on a FIFO. A payload is exactly where
either might be, so the check lstats every name first and hashes through a
descriptor opened without following a link and without blocking. `--verify`'s
own hashing is unchanged.

## Decision

`deploy.py --system --dry-run` checks the payload directory it runs from, and
`deploy.py --system` checks its root-only snapshot, with one function,
`payload_blockers()`. Either refuses with exit 6 on any difference. The dry
run then advertises no command. The install refuses before its first
`systemctl`, having touched nothing on the node. Every `PAYLOAD_SOURCES`
entry must be present with its type: a file is a regular file, and a
directory is a directory and not a link. Every name under `shim/` and
`docs/` must be a regular file or a directory, and must be listed in the
lock, because `cp -a` installs whatever is there. Every file must hash to its
entry. The lock must parse, and must name this build's `SITE_SHA256`, as
`site_sha256` and as the `site.toml` entry, and this build's version. The
lock is not hashed against itself, and entries outside `PAYLOAD_SOURCES`,
such as `deploy.py`, are not this check's.

The lock is read for integrity and nothing else. No value in it reaches a
path, a threshold or any branch other than this refusal. A check this
process is not permitted to make, EACCES or EPERM, is reported under NOT
CHECKED by an unprivileged dry run, and is a refusal for anyone with
privilege. Mode bits are not compared, because the install sets them.

## Consequences

- **The record has a second consequence, and it is exit 6.** The uninstall's
  hash of the installed `site.toml` (ADR-0027) and this check of a payload
  against its own lock both reach the same exit, before anything is
  touched. Neither parses configuration on the node, neither is moved into
  `reaper.py` or `guard.sh`, and neither runs on a poll. A third consequence
  of the record is a new ADR.
- **The dry run and the install reach the same exit on a bad copy.** The
  snapshot's copies no longer raise on their own status, so a source the
  copy could not find becomes a `missing:` line from the check, exit 6, as
  the dry run predicted, rather than exit 1 passed through from `install`.
  The copy's own stderr is still relayed.
- **A FIFO where a top-level file entry should be is caught by the dry run,
  not by the install's copy.** `install` opens its source to copy it, so
  that copy can block before the check runs. The dry run's lstat names the
  FIFO first, which is one more reason to read the dry run before installing.
- **What the check establishes, and what it does not.** It proves the copy
  matches the record built with it. It does not prove the record is the
  reviewed one. That is `walk-blocker provenance`'s question, asked of the
  lock's `site_sha256` off the node.
- **The suite's own deployer runs from a directory holding `deploy.py`
  alone**, so the orchestration tests pass this check through a seam on the
  module, `pass_payload_checks()`, folded into the helpers that already stub
  the trust checks, and never through an environment variable. The check's
  own tests load a copy of `examples/payload/`'s `deploy.py`, so `REPO` is a
  real payload. They pin one case per way a copy can fail to be the build,
  including the FIFO case, which runs in a subprocess under a timeout so
  that a regression fails rather than hangs.

## Re-measure when

n/a: structural. Nothing about a site changes whether a payload should match
the record built with it.

## Site config touched

None read on the node. The check reads the payload's `site.lock.json` as a
record, and compares it against the build's compiled `SITE_SHA256` and
version.
