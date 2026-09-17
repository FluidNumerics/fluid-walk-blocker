# fluid-walk-blocker

Two guardrails for a shared HPC login node, generic and config-driven: a
PATH shim that refuses an unbounded filesystem walk before it starts, and a
periodic reaper that finds the walks that got past it. `walk-job` is the
sanctioned alternative every refusal names; `deploy.py` installs all of it
as root and takes no arguments.

Every fact about a particular site lives in `site.toml`, is validated by
`schema/site.schema.json`, and is compiled into the node artifacts by
`walk-blocker build`. The node never reads configuration.

Copyright (c) 2026 Fluid Numerics LLC. All rights reserved. This repository is
private; see `LICENSE`.

## Why

An automated agent ran an unbounded `find` over ssh against a very large
network filesystem. The client timed out and retried; the remote process was
never killed, and it ran for days. A guard that reads commands as text
cannot see inside the quoted string `ssh` carries, and a guard keyed on CPU
or memory would have watched that process run for days and reported the node
healthy — it was a metadata-I/O hog and nothing else. So there are two
layers, one in front of the command and one behind it, and the second does
not care how the process arrived.

## The two layers

| Layer | What it is | Catches | Bypassable by |
|---|---|---|---|
| 1, the shim | a POSIX `sh` dispatcher on every user's `PATH`, generated from the rule table and `site.toml` | the naive invocation, before it starts, in under a second, with a message naming the alternative | an absolute path, a private `PATH`, a container, a batch script, a second-level shell, a shell function — by design (ADR-0001) |
| 2, the reaper | a root-run systemd timer, stdlib Python, reading PSI at each user slice and classifying the whole process table | whatever reached the node regardless of how, once it is past budget on an expensive mount | nothing that changes how a process arrives; it reports by default and kills only when a human decides |

Layer 1 is advisory and the code says so. Layer 2 is the backstop, and its
default is `--report`.

## How a search gets intercepted

1. A shell starts. Its system startup file — `[hooks.<shell>].file`,
   verified to fire for `ssh host 'cmd'` at install — puts `<prefix>/bin`
   first on `PATH`.
2. The user runs `grep -r`, `find`, `du`, `rg` or another wrapped name. The
   symlink in `<prefix>/bin` resolves to `guard.sh`.
3. The shim reads its argv, its cwd and `/proc/mounts` — one file, no
   fork, nothing that can block. If the walk is bounded within the depth
   ceiling, or every root is on a cheap mount, or the root is deeper than
   `[filesystems].unscoped_depth`, it `exec`s the real tool.
4. Otherwise it exits 2 with a refusal that names the bound the tool
   accepts, the allowance on that mount, and `walk-job`. Exit 2, never 1:
   `grep` uses 1 for "no match" and a refusal must never read as an empty
   result.
5. Whether or not the shim ran, the reaper polls on `[timer].on_calendar`,
   differences PSI per user slice, walks `/proc`, and writes a finding for
   every process that is a known tool on an expensive mount past
   `[reaper].traversal_budget_s` — with `stalling_slice` saying whether PSI
   agreed.

## What to run instead

Every refusal prints the alternative for the tool that was refused. The
table, for reading before you are refused:

| Refused | Bound the walk with | Or |
|---|---|---|
| `find`, `bfs` | `-maxdepth N`; `-xdev` when the walk starts on a cheap mount and would descend into an expensive one | `walk-job -- find ...` |
| `rg` | `--max-depth N`, `--one-file-system` | `walk-job -- rg ...` |
| `fd` | `-d N`, `--one-file-system` | `walk-job -- fd ...` |
| `tree` | `-L N`, `-x` | `walk-job -- tree ...` |
| `du` | `-x` — its `-d` prunes output, not the walk, so no depth flag bounds it | `walk-job -- du -sh PATH`; a bounded walk, not a lookup, absent server-side accounting |
| `grep`, `egrep`, `fgrep`, `zgrep`, `rgrep` | no depth flag exists; change tool: `rg --max-depth N PATTERN PATH`, or `find PATH -maxdepth N -type f -exec grep ... {} +` | `walk-job -- grep ...` |
| `ugrep` | `--depth N` | `walk-job -- ugrep ...` |
| `fzf`, `sk` | not wrapped by default (`[shim].unwrapped_tools`): interactive finders bounded by the user's attention | — |

`N` is the depth ceiling the refusal prints: the global
`[filesystems].maxdepth_allowed`, or the mount's own `maxdepth` where the
site measured one. A root deeper than `[filesystems].unscoped_depth`
components below the mount point may be walked unbounded.

`walk-job` submits the command to `[slurm].partition` under a wall-clock
limit the scheduler enforces. It moves load off the login node, not off the
filesystem: the same metadata operations reach the same storage from a
different client. `walk-job -h` on the node has the options.

**Refusals are recorded.** Every refusal writes one `refused` record to the
journal under the `walk-blocker` tag, with the tool, the mount judgement and
the caller's uid, before the message is printed. An empty journal on a
node with the shim installed means nothing was refused, not that nothing was
looked at.

**The escape hatch.** `WALK_BLOCKER_UNSCOPED=1` turns a refusal into an
allow. Every use is recorded in the journal under the same tag,
with the tool, the mount judgement and a uid stamped by journald rather
than taken from the environment being audited:

```sh
journalctl -t walk-blocker -o json
```

It exists because a block with no override gets routed around with `\find`,
after which nothing is measurable.

## What is in scope

| Path | What it is |
|---|---|
| `src/walk_blocker/` | the workstation tool: `validate`, `schema`, `survey`, `build`; the config loader; the stamp and render machinery |
| `src/walk_blocker/search_rules.py` | the rule table both layers are built from |
| `schema/site.schema.json` | what a `site.toml` may say |
| `node/reaper.py` | Layer 2 |
| `node/shim/guard.sh.in`, `wrapped_names.sh.in` | Layer 1, as templates |
| `node/shim/install.sh` | the shell installer and the per-poll reconcile |
| `node/shim/measure.sh`, `measure-flags.sh` | the shim's performance gate; the flag-clustering probe for the rule table |
| `node/survey.py` | the mount survey, tier three of the mount policy |
| `node/walk-job` | the sanctioned alternative |
| `node/deploy.py` | the argumentless deployer |
| `examples/site.example.toml` | a fictional site with every key written out |
| `examples/payload/` | that site, built; a build product checked by CI |
| `docs/adr/` | the decisions, `0001` through `0017` |
| `docs/site-config.md` | what a site measures before filling `site.toml` |
| `docs/operating.md` | the operator's runbook |
| `docs/plan.md` | the architecture as built, on one page |
| `docs/evidence.md` | where the evidence is, and why it is not here |
| `tools/check_no_site_literals.py` | the IP-hygiene gate CI runs |
| `tests/` | the suite, including the shim-versus-table matrix |

## What is out of scope

- **Enforcement by Layer 1.** A PATH shim cannot bind; describing it as a
  sandbox invites reliance it cannot support (ADR-0001).
- **Killing by default.** The reaper reports. `--kill` is a decision made at
  the site after reading real findings, and `--kill-others` is a second
  explicit decision (ADR-0009).
- **Processes that predate the deployment.** The reaper will report them;
  nothing here retroactively wraps a shell that already started.
- **Sudo for agent sessions.** Automated agents working in this repository
  never act as root and never pass `--i-have-approval`. The installer is for
  an authorized human who already holds root (ADR-0004).
- **Restricting non-interactive ssh, or any transport.** Layer 2 is
  transport-agnostic on purpose (ADR-0003).
- **An index of the large filesystem.** Not at that scale, by this tree or a
  vendor; `walk-job` is the general answer (ADR-0007).
- **Site facts.** Hostnames, capacities, timers, measured figures — none of
  it lives here (ADR-0014). `docs/evidence.md` says how to ask.

## Working on it

```sh
uv run --group dev pytest tests/ -q
uv run walk-blocker validate --site examples/site.example.toml
uv run walk-blocker build --site examples/site.example.toml --out examples/payload --check
uv run walk-blocker survey --mounts /proc/mounts --site examples/site.example.toml
shellcheck --shell=sh --severity=warning examples/payload/shim/*.sh examples/payload/walk-job
uvx --from pymarkdownlnt==0.9.39 pymarkdown --config .pymarkdown scan README.md CLAUDE.md docs/*.md docs/adr/*.md
uvx yamllint==1.38.0 -c .yamllint .github/workflows/ci.yml
python3 tools/check_no_site_literals.py
```

`examples/payload/` is a build product: a finding about it is a finding
about the rule table, a template, the schema or `site.example.toml`, and
`--check` is the oracle. Never edit it by hand.

## Versions

`VERSION` at the root of this tree is the only hand-written version.
`walk-blocker build` stamps it as a literal into every node artifact that
answers `--version`, so one payload carries one number and the node has
nothing to read it from at run time. Ask a node four ways, and expect them to
agree:

```sh
cat <prefix>/.walk-blocker-payload
cat <prefix>/site.lock.json
python3 <prefix>/reaper.py --version
sh <prefix>/shim/install.sh --version
```

`walk-job --version` is the fifth. The lock file carries a hash per emitted
file and the hash of the `site.toml` that built it, so a deployed
configuration can be checked against a commit.

## Reading order

1. This file — what it is, what is in and out of scope
2. `docs/plan.md` — the architecture as built, on one page
3. `docs/adr/0001` through `docs/adr/0017`, in order — the decisions
4. `docs/site-config.md` — what a site measures before it can be deployed
5. `docs/operating.md` — the operator's runbook
6. `docs/evidence.md` — where the evidence is, and why it is not here
7. `CLAUDE.md` — the non-negotiables, and the parts that are easy to get wrong
