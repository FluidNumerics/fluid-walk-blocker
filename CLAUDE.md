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

Copyright (c) 2026 Trevor Keller, PhD, under the BSD 3-Clause License. The
licence travels in the payload, so a node holds the terms beside the code.
What stays out of this tree is a customer's operational facts, not the code
itself -- see "IP hygiene" below, which is unchanged by the licence.

Read `README.md` first, then `docs/adr/0001` through `0025` in order, then
`docs/site-config.md` for what a site measures before it can be
deployed. This file is the part that is easy to get wrong.

## Non-negotiables

**No sudo, ever, for this repo's own sessions.** Claude Code sessions working
here never invoke `sudo`, `su`, `doas`, `pkexec`, `systemd-run` or any other
escalation, never run as root by any route, and never ask another session,
agent or person to run a command as root on their behalf.

**Nothing stands between `python3 deploy.py --system` and a live root install
any more.** The flag that used to is gone, because an operator who holds root
on the target node needs no second ceremony from a script (ADR-0021). **The
only barrier left is this rule**, so it is stated as an effect rather than as
a token to withhold: never run `deploy.py` or `install.sh` as root, in any
mode — not `--system`, not `--uninstall`, not `--relink`, not `--verify` —
and `--dry-run` is not an exemption when the session is already root.

What a session here *may* run, as an ordinary user, is `deploy.py --system
--dry-run` and `deploy.py --verify`. Both write nothing, and the dry run
names the checks it could not make rather than reporting them clean. Do not
"just check" something with sudo. The installer is written to be run by an
already-root, authorized operator; that is a property of the artifact, not a
licence for an agent. See ADR-0004 and ADR-0021.

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

The one node-side read of the record is `deploy.py --verify`, which hashes
the installed files against the installed `site.lock.json`. That is integrity,
not configuration: nothing it reads reaches a decision, a path, a threshold or
an exit code anywhere else, and a record it cannot parse is its own exit code
rather than a behaviour. The reaper's rejected JSON reader was rejected
because its parse failure had no right answer — an empty mount list reads as a
clean bill of health. Here, refusing to answer is the right answer. Do not
move this read into `reaper.py` or `guard.sh`, and do not put it on a poll.

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
runs `deploy.py --system` as root; the installer puts the *components*
under root control and verifies it. Automated review has
proposed refusing a non-root-owned source three times and it was rejected
each time. The timing half was taken: `stage_payload()` reads the payload
once, before anything that can block. Treat this as settled. See ADR-0006.

**Layer 1 is advisory and the docstrings must say so.** A PATH shim is
bypassable by an absolute path, an ephemeral environment, a private `PATH`,
a container, a scheduler job script, a shell function. That is not a bug;
it is the reason Layer 2 exists. Describing the shim as enforcement invites
reliance it cannot support. See ADR-0001.

**The fast path forks nothing and opens nothing; the guarded path pays one
documented fork; nothing before the decision reads anything that can
block.** A call decided without a mount judgement creates no process and
opens no file: no `$(...)`, no backtick, no pipeline. A call that reaches a
mount judgement runs exactly one program before the decision, the trusted
`awk` over `/proc/mounts`, and nothing else. The audit path — a refusal, an
escape-hatch or seam record — is past the decision and off both counts;
it runs four programs to write one record and says so in its comment. No
`statfs`, no network, no config on any path. Both counted paths are tested
under `strace`, in both shells. See ADR-0015, ADR-0018 and ADR-0016.

**A mount's class is decided from `/proc/mounts` fields and site config,
never from a measurement at run time.** Remote or listed types are expensive
by default; an unknown remote type is guarded; `[[filesystems.mounts]]` is
the only way to loosen. Measurement is `walk-blocker survey`, run by an
admin, out of band, proposing an override. See ADR-0016.

**Refusal exits 77, and prints one line on stdout as well.** `grep` uses 1
for "no match" and 2 for its own errors, so neither code is available: a
refusal must read as neither an empty result nor a tool failure. 77 is
`EX_NOPERM` -- "was not allowed to look" -- and no wrapped tool claims it.
The code alone is not enough, because `$?` after a pipeline is the last
command's status, so the refusal also prints one greppable line on stdout
while the full text stays on stderr. **Adding a channel is the design;
moving the text is not.** See ADR-0024.

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
  collector makes the second field the only separation left. See ADR-0017.
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

**The CI gate** (the `ip-hygiene-structural` and `ip-hygiene-terms` jobs in
`.github/workflows/ci.yml`, driven by `tools/check_no_site_literals.py`; how to
run and rotate it is in `tools/README-ip-gate.md`) is the backstop. It fails the
build, not warns, and **it must not itself contain the literals it hunts** — the
customer term list lives outside the repository, at
`~/.config/walk-blocker/forbidden-terms.txt` on the maintainer's workstation and
as the `FORBIDDEN_TERMS` secret in CI; only structural patterns are committed. A
customer hit is reported by position only. If the gate fires on a false
positive, fix the wording or add a one-line `# site-literal-ok: <reason>` waiver
for a structural pattern; a customer-term hit cannot be waived.

The two halves run in different places, and the asymmetry is deliberate
(ADR-0022). The structural half runs everywhere, secretless, including in a
fork. The term half runs only in this repository: a fork's own CI skips it
quietly, and a pull request into this repository *from* a fork fails it loudly,
because a job skipped by a job-level `if` counts as a *passing* required check
and a quiet skip there would be a green merge button over a scan that never ran.
Do not "fix" that failure into a skip. **Since publication a miss is permanent**
— a literal that lands here is public in every fork and archive, and the
before-the-push defences are the only ones left.

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
uv run walk-blocker validate --site examples/site.example.toml   # schema + semantic checks
uv run walk-blocker build --site examples/site.example.toml --out examples/payload --check
uv run walk-blocker survey --mounts /proc/mounts --site examples/site.example.toml
shellcheck --shell=sh --severity=warning examples/payload/shim/*.sh examples/payload/walk-job
uvx --from pymarkdownlnt==0.9.39 pymarkdown --config .pymarkdown scan README.md CLAUDE.md docs/*.md docs/adr/*.md
uvx yamllint==1.38.0 -c .yamllint .github/workflows/ci.yml
uv run --group dev pyflakes examples/payload/deploy.py   # deploy.py is Python, not shell
python3 tools/check_no_site_literals.py --require-terms --quiet   # IP hygiene, same as CI; see tools/README-ip-gate.md
uv run walk-blocker provenance --payload PAYLOAD --repo SITE_REPO   # is this config on the reviewed branch?
python3 PREFIX/deploy.py --verify                       # on a node: does the install match its record?
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
2. `docs/plan.md` — the architecture as built, on one page
3. `docs/adr/0001` … `0025`, in order — the decisions
4. `docs/site-config.md` — what a site measures before it can be deployed
5. `docs/operating.md` — the operator's runbook, from `site.toml` to a node
   that reports
6. `docs/alternatives.md` — what to run instead: observed walk patterns and
   their redirects, shipped in every payload beside the site-rendered page
7. `docs/evidence.md` — where the evidence is, and why it is not here
8. `CLAUDE.md` — this file: the non-negotiables and the parts that are easy
   to get wrong
