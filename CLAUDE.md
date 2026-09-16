# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

`fluid-walk-blocker` is a generic, config-driven pair of guardrails for a
shared HPC login node: a PATH shim that refuses an unscoped filesystem walk
before it starts, and a periodic reaper that finds the ones that got past
it — with `walk-job`, the sanctioned alternative every refusal names, and an
argumentless `deploy.py`. Every fact about a particular site lives in
`site.toml`, is validated by `schema/site.schema.json`, and is compiled into
the node artifacts by `walk-blocker build`. The node never reads
configuration.

Copyright Fluid Numerics LLC. All rights reserved. This tree is private.

Read `README.md` first, then `docs/adr/0001` through `0016` in order, then
`docs/site-config.md` (once it exists) for what a site measures before it can
be deployed. This file is the part that is easy to get wrong.

## Non-negotiables

**No sudo, ever, for this repo's own sessions.** Claude Code sessions working
here never invoke sudo, never pass `--i-have-approval` to `deploy.py` or
`install.sh`, and never otherwise act as root — regardless of what those
scripts are capable of. Do not "just check" something with sudo. The
installer is written to be run by an already-root, authorized operator; that
is a property of the artifact, not a licence for an agent. See ADR-0004.

**No site literal in this tree, ever.** See "IP hygiene" below. This is a
rule about what the generic tree *is*, not a style preference, and the CI
gate exists because rules get forgotten. A default value, a fixture, a
docstring example or an ADR sentence that names a real site is a defect.

**The node never reads `site.toml`.** Site values are compiled into the
artifacts by `walk-blocker build`. Do not add a runtime config read, an env
var that supplies a site value, or a "convenience" path lookup on the node.
`WALK_BLOCKER_UNSCOPED` is the documented, audited escape hatch of an
advisory guard (ADR-0001), not a test seam. The other `WALK_BLOCKER_*`
variables are test seams, audited when they change the outcome, and they gain
no siblings. See ADR-0013.

**`deploy.py` takes no path arguments, and adding one back is a design
change.** Every location it writes as root is a literal compiled from
`[install]` and `[hooks.*]`; `argparse` rejects `--prefix` outright. What
that removed is argument-shape validation, which now lives in the schema
and one test over the compiled literals. The filesystem-state checks (trust
chain, traversability, ownership, symlinked hook file, payload marker)
remain and must — a literal path is not a trusted path. See ADR-0005 and
ADR-0013.

**The source tree is user-owned by design; do not add a check that refuses
it.** An operator copies a built payload into a folder that account owns and
runs `deploy.py --system --i-have-approval` as root; the installer puts the
*components* under root control and verifies it. Automated review has
proposed refusing a non-root-owned source three times and it was rejected
each time. The timing half was taken: `stage_payload()` reads the payload
once, before anything that can block. Treat this as settled. See ADR-0006.

**Layer 1 is advisory and the docstrings must say so.** A PATH shim is
bypassable by an absolute path, an ephemeral environment, a private `PATH`,
a container, a scheduler job script, a shell function. That is not a bug;
it is the reason Layer 2 exists. Describing the shim as enforcement invites
reliance it cannot support. See ADR-0001.

**The shim forks nothing before `exec` and reads nothing that can block.**
Before `sg_exec_real` there is no `$(...)`, no backtick, no pipeline; the
only file it opens is `/proc/mounts`. No `statfs`, no network, no config.
There is a test. See ADR-0015 and ADR-0016.

**A mount's class is decided from `/proc/mounts` fields and site config,
never from a measurement at run time.** Remote or listed types are expensive
by default; an unknown remote type is guarded; `[[filesystems.mounts]]` is
the only way to loosen. Measurement is `walk-blocker survey`, run by an
admin, out of band, proposing an override. See ADR-0016.

**Refusal exits 2, never 1.** `grep` uses 1 for "no match" and no caller
may read a refusal as an empty result.

**Table and shim must agree.** `guard.sh` and `wrapped_names.sh` are
generated from the rule table and `site.toml`; edit those and rebuild. A
shim that blocks what the reaper does not report is worse than either alone.

**Never claim a kill that did not land.** A process blocked in a filesystem
syscall does not die on `SIGKILL` until the syscall returns. After TERM →
grace → KILL, re-check, and record `signalled_but_wedged` if it is still
there. An audit log that reports success it did not achieve is worse than
no audit log.

**PSI totals are cumulative since boot.** Difference successive polls.
Reading totals means every finding is a finding forever. See ADR-0002.

**PSI corroborates; it does not gate.** Every poll scans and classifies the
whole process table. The thresholds select which findings carry
`stalling_slice: true`; they never suppress a record and never gate
`--kill`. `stalling_slice` has three states and the absent one is
load-bearing. See ADR-0009.

**A stalling slice is evidence about a user, not a process.** Never act on
the cgroup signal alone. The `/proc` step that names a specific process is
what makes an action defensible to the person whose work is being killed.

**Read PSI at `user-*.slice`, never per-scope.** A site may run more than
one inbound ssh transport, each registering sessions under a different
scope-naming scheme. The slice aggregates them all. Finer attribution is
the `/proc` step's job. See ADR-0003.

**A finding's `origin` is descriptive and never an input to `classify()`.**
A process is not more or less of a runaway because of how its owner logged
in. The label set is `[reaper].origins`; the rule is code.

**Hook shells are verified, not assumed, and their class comes from
config.** `[hooks.<shell>]` names each shell's system startup file and
whether it is `required` (a registered login shell at the site; the install
fails if the hook cannot be proven to fire for a remote command) or
`best-effort` (written and checked only when the shell is present; never
gates the install). Do not soften a required hook into a warning, and do not
fold a best-effort hook into the hard gate: the difference is whether that
shell's absence is an anomaly at the site, and only a census answers that.
The reconcile timer reports hook state and never repairs or fails. See
ADR-0008.

**Report before you kill.** The reaper's default is `--report`. Promoting
to `--kill` happens after reading real findings against real traffic at
the site, and it is a decision someone makes, not a default that drifts.

**Node code is stdlib-only Python 3.9 or POSIX `sh`.** Anything under
`node/`, and `deploy.py`, runs on a login node whose Python we do not
choose and which has no `uv`. A PEP 723 header there does not run; a
`match` statement there does not parse. Dash-clean `sh`. See ADR-0015.

## This node is shared

A login node carries other tenants' long-running processes and scheduled
work. Every design choice here affects people who did not ask for it:

- **The timer slot is config** (`[timer].on_calendar`) **and must be chosen
  against the live schedule of the target node** — `systemctl list-timers
  --all` and the site's cron — never copied from a doc, an example, or
  another site. Pick a minute and a second no neighbour uses; a per-minute
  collector makes the second field the only separation left.
- The deployed payload and its audit trail live under `[install].prefix`
  and `[install].spool_dir` on **local disk** — never under a home directory,
  which may be on the filesystem under investigation. A monitor that lives
  on what it monitors is unavailable exactly when it is needed.
- Reading another user's `io.pressure` is unprivileged and fine. Killing
  another user's process is not a thing this repo does without a human
  deciding it, per incident, at the site.
- Measuring the guardrail must not cause the incident. Depth allowances are
  measured one root at a time, capped, paused, off the login node, with the
  filesystem watched. Never run the deeper walk when the shallower one
  already fails the bar.

## IP hygiene: what may never appear in this tree

This tree is generic and Fluid Numerics' own. The sites it is deployed at
are customers, and their operational facts are theirs. None of the
following may appear anywhere in this repository — code, tests, fixtures,
docs, ADRs, comments, commit messages, issue or PR text:

- site hostnames, cluster names, tailnet names, node names;
- usernames, uids, gids, account names, engineer or customer names;
- partition, QoS, account or reservation names;
- neighbour timer slots, cron lines, or other tenants' unit names;
- measured figures from a site: inode counts, capacities, PSI percentages,
  CPU-hours, user or core counts, process counts, load averages, timing
  figures as measured;
- vendor build strings, kernel or package versions as observed at a site;
- dates of on-node measurements;
- paths that exist only at a site.

What is fine: filesystem type names (`wekafs`, `nfs4`, `lustre`, `gpfs`),
tool names and versions measured on the tool itself, kernel facts, the
shape of the originating incident ("an automated agent ran an unbounded
`find` over ssh"), and the fictional values in `examples/site.example.toml`.

Where a decision rests on a site measurement, write "measured at a
reference deployment; record held by Fluid Numerics privately" and give
the re-measure condition and the config key. See ADR-0014 and
`docs/evidence.md`.

**The CI gate** (`ip-hygiene` job in `.github/workflows/ci.yml`, driven by
`tools/check_no_site_literals.py`; how to run and rotate it is in
`tools/README-ip-gate.md`) is the backstop. It fails the build, not warns,
and **it must not itself contain the literals it hunts** — the customer term
list lives outside the repository, at `~/.config/walk-blocker/forbidden-terms.txt`
on the maintainer's workstation and as the `FORBIDDEN_TERMS` secret in CI;
only structural patterns are committed. A customer hit is reported by
position only. If the gate fires on a false positive, fix the wording or add
a one-line `# site-literal-ok: <reason>` waiver for a structural pattern; a
customer-term hit cannot be waived.

## Style

Python 3.9+ everywhere: the build tool, `src/` and `tests/` run on the whole
CI matrix (`tomli` stands in for `tomllib` below 3.11). Standalone `uv run`
scripts with PEP 723 headers are fine anywhere outside `node/` and
`deploy.py`.

Comment where a comment earns its place — chiefly where a simpler-looking
alternative is wrong. Don't annotate the obvious.

Prefer the shape of a thing to its identity in every written output: "a
long-lived `tail -F` on an expensive home" not a username and a path.

Trevor's pronouns are xe/xem/xyr; use them in commit messages, PR bodies,
issues and docs.

## Commands

```sh
uv run --group dev pytest tests/ -q                     # the suite
uv run walk-blocker validate --site site.toml           # schema + semantic checks
uv run walk-blocker build --site examples/site.example.toml --out examples/payload --check
shellcheck --shell=sh --severity=warning examples/payload/shim/*.sh   # walk-job joins at Milestone 5
uvx --from pymarkdownlnt==0.9.39 pymarkdown --config .pymarkdown scan README.md CLAUDE.md docs/*.md docs/adr/*.md
uvx yamllint==1.38.0 -c .yamllint .github/workflows/ci.yml
python3 tools/check_no_site_literals.py --require-terms --quiet   # IP hygiene, same as CI; see tools/README-ip-gate.md
```

Commands marked with a milestone do not work yet, or run only as a placeholder
that says so; they name the shape the tooling will take so the docs do not
have to be rewritten when it lands.

CI runs the suite on Python 3.9–3.12 in an enterprise-Linux container as a
non-root user, so that when the shell and permission tests arrive with the
node code they see both dash and bash-as-`/bin/sh` and are not vacuous under
root.

## Reading order

1. `README.md` — what it is, what is in and out of scope
2. `docs/adr/0001` … `0016`, in order — the decisions
3. `docs/evidence.md` — where the evidence is, and why it is not here
4. `CLAUDE.md` — this file: the non-negotiables and the parts that are easy
   to get wrong
