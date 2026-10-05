# ADR-0032: The node has declared requirements, and a fault only below one is unsupported

**Status:** accepted, 2026-10-05
**Evidence:** n/a: upstream documentation and measurements on the tool; no site evidence — see `docs/evidence.md`

## Context

The node code calls programs it does not ship: the trusted binaries the
site names, the coreutils and systemd tools the installer and `deploy.py`
run through `PATH`, the scheduler client `walk-job` submits through, and
the kernel interfaces the reaper reads. Each call relies on a feature, and
some features have a first version or an implementation that lacks them.
A `logger` without `--size` drops every Layer 1 record. An `awk` whose
`gsub` reads a backslash before a quote as an escape writes a record that
does not parse. A `gawk` in POSIX mode under a comma-decimal locale prints
numbers `measure.sh` cannot read back.

Nothing said which of these the engine supports. A review that found one
had two moves: change code to accommodate the variant, or leave the issue
open. The first adds machinery for configurations no site has shown, each
with its own tests and its own failure modes; the second leaves a queue of
issues nobody can close. Neither answers the question a site asks before
it deploys: what must the node provide?

The alternatives, in their strongest form:

- **Probe `logger --size` at deploy time**, so a site whose logger lacks it
  learns before the first record is lost. Rejected: the probe runs the
  logger as root during the install, which the install does not do today,
  and writes a record to the journal to learn whether it can write one. It
  covers only the trusted path; the `PATH` fallback is resolved at run time
  and no install can check it. And it is machinery against a logger no site
  has shown. A site whose logger lacks the option reads the row before it
  deploys; a probe is worth building when one does not.
- **Pin each requirement by matching the code's argv**, a test that greps
  every `logger` call for `--size` and fails if a requirement row is
  missing. Rejected as fragile: it tests spelling, not reliance. A call
  reached through a variable, a heredoc or a different quoting is invisible
  to it, and a match proves only that the text is present.
- **State a version for every row**, so the file is uniform. Rejected: a
  version the code does not need is a figure a reader must trust and a
  reviewer must check, and a version that was observed somewhere rather
  than documented as a first version is a site fact (ADR-0014). Only a
  feature with a documented first version, where a node below it is
  plausible, earns the number.
- **Record observed versions here**, so a reader sees what the engine has
  run against. Rejected: an observed version describes a site, and ADR-0014
  keeps those out of this tree.
- **Remove the fallbacks below a requirement**: the pure-`sh` mount reader
  and the no-awk record. If `awk` is required, the code that runs without
  one is dead weight. Rejected here, and left to a separate decision: both
  are fail-safes for an `awk` that fails or is killed, not only for one
  that is absent, and a requirement does not stop a binary from failing.

## Decision

The engine declares what the destination node must provide, in
`docs/node-requirements.md`: one row per dependency, naming the feature the
code relies on, the floor, the source, and the files that rely on it. A
fault that occurs only below a declared requirement is unsupported. Its
issue is closed by citing the row, not by changing code.

A requirement comes only from upstream documentation, a measurement made
on the tool itself, or an existing ADR, and the row says which. It never
comes from a version observed at a site. A row states a version only where
the code relies on a feature with a known first version and there is a
clear need to say it: today `logger --size`, util-linux 2.27 by its man
pages, and Python 3.9 (ADR-0015). Every other row names the dependency and
the feature, with the floor "none stated".

Fallbacks below a requirement stay. Observed versions belong in each
site's own repository, and only for rows that state a version.

## Consequences

- **An issue below a floor closes with a citation.** The row is the
  answer; the issue does not stay open as a reminder, and no code is
  written for a configuration the file declares unsupported.
- **A new version figure is a deliberate change.**
  `tests/test_node_requirements.py` asserts that the rows stating a version
  are exactly `logger` and `python3`. Adding a third means editing the test,
  which makes the reason visible in review.
- **The table is checked against the code it describes**, not against
  argv: every `[trusted_binaries]` key has a row, every path a row names
  exists, and every source is one of the three allowed. Whether a row's
  feature is still relied on is a review question, not a test.
- **The file ships in every payload**, under `docs/`, so the operator on
  the node reads the same requirements the build was checked against.
- **A fallback below a requirement is still tested.** The no-awk record and
  the pure-`sh` reader keep their tests; a site below the floor gets
  whatever they do, and no promise about it.
- **A site keeps its own record.** The versions it observed for the rows
  that state one live beside its `site.toml`, outside this tree, as every
  other site measurement does (ADR-0014).

## Re-measure when

When the code begins to rely on a feature it did not, or upstream
documentation moves the first version of one it does: read the upstream
documentation, or measure on the tool, and update the row and the test in
the same change. Nothing about a site changes a row.

## Site config touched

- `[trusted_binaries]` — names which binary on the node each of its rows
  applies to. The keys and their meaning are unchanged.
