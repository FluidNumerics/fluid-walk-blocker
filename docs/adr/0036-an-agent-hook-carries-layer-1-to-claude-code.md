# ADR-0036: An agent hook carries Layer 1's rule table to Claude Code, compiled like the shim, and it denies but never rewrites

**Status:** accepted, 2026-10-29
**Evidence:** n/a, structural; the bypass is a property of the tool — see `docs/evidence.md`

## Context

ADR-0001 put the first line of defence on `PATH` because a shim is handed
`argv`, not text. Claude Code, the agent behind the founding incident, does
not reach that shim for most of what it walks:

- **Its Bash tool defines `find` and `grep` as shell functions** that run the
  claude binary under another name (`exec -a bfs`, `exec -a ugrep`). A
  function wins over `PATH`, so the shim is never looked up. Measured on the
  tool itself, Claude Code 2.1.312.
- **Its Grep and Glob tools run a bundled ripgrep in-process**, with no shell
  and no `PATH` lookup. `USE_BUILTIN_RIPGREP=0` makes it resolve `rg` through
  `PATH` instead.
- **A developer's session reaches the login node with `ssh host '...'`.** The
  remote half meets the shim, but the session that sent it is on another
  machine, and a local tool timeout kills the ssh client while the remote
  walk runs on, orphaned. That was the incident's shape.

So for this caller Layer 1 is absent rather than advisory, and Layer 2 sees
the walk only after it has started. ADR-0001 named a companion fix: a
session-level guard that re-enters the check on the remote half of ssh. It
placed that guard in Fluid Numerics' own tooling, which protects Fluid
Numerics' sessions. A site's own users run their own Claude Code, which that
guard never reaches.

A prototype was built in a site's repository: a Claude Code plugin whose
`PreToolUse` hook judges Bash, Grep and Glob calls with a byte-identical copy
of `search_rules.py`, plus a `SessionStart` hook that states the rules, a
skill naming the alternatives, and a managed-settings drop-in that
force-enables the plugin for every user of the node. It works, and it exposed
issue #249 on the way. It also takes four shortcuts that this tree has
already refused elsewhere:

- **It reads its policy from a JSON file at run time**, rendered from
  `site.toml` by a script of its own. Its strongest form: a JSON reader is
  stdlib, the file is generated and drift-checked, and the hook runs on
  developers' machines where `site.toml` is not. Rejected for the reason the
  reaper's JSON reader was rejected (ADR-0013): a parse failure has no right
  answer, and an empty mount list reads as a clean bill of health. A
  generated literal has no parse to fail.
- **It is installed by a hand copy as root**, to a path no config names.
  Rejected by ADR-0005: every root-write sink is a literal compiled from
  reviewed config.
- **Its command reader is tested by its own cases**, not by
  `tests/argv_cases.py`. Rejected by "table and shim must agree": a third
  consumer that is not held to the shared matrix drifts silently, and a guard
  that blocks what the reaper does not report is worse than either alone.
- **On the node it rewrites every allowed command** to begin
  `unset -f find grep;`, so that whatever its reader missed still meets the
  shim. Its strongest form: it is the only move that puts Claude's bundled
  `find` and `grep` back in front of the shim at all, and it catches what the
  text reader cannot. Rejected, for two reasons that do not depend on how well
  it works. The form the prototype uses, `updatedInput` with no permission
  decision, was observed to work and is not documented, so a Claude Code
  upgrade can drop it with no error. The documented form pairs `updatedInput`
  with `allow`, and `allow` skips the user's own permission rules: a guard
  that grants permission to every command it does not object to has widened
  access while claiming to narrow it.

Making the hook a site's own code was also on the table. Its strongest form:
the hook is specific to one tool, the engine has never carried code for a
particular client program, and a site that does not run Claude Code gains
nothing. Rejected, because everything in the prototype except its values is
generic, its correctness is a property of the rule table the engine owns, and
a copy per site is a copy per site to keep in agreement with that table.

## Decision

`walk-blocker build` emits an **agent hook** for Claude Code beside the shim,
when `[agent_hook].enabled` is true. The key has no default: whether a site's
users run Claude Code is a fact about the site, so the site states it. It is
part of Layer 1 and advisory like
the shim (ADR-0001): it reads commands as text, below the ceiling that ADR
names, and it exists only because this caller never reaches the shim. It
emits a Claude Code plugin, `agent/`, holding the `PreToolUse` and
`SessionStart` hooks, the playbook skill, the rules text, and the payload's own
`search_rules.py`, byte-identical and hashed in `site.lock.json`. Every site
value the hook uses is a literal stamped at build (ADR-0013): the mount table
and its depth allowances, the policy, the login host names and `walk-job`'s
defaults. The rules text and the skill are rendered from templates over the
same values. A site writes no prose for them.

The hook decides with `search_rules.check()` and the compiled policy, and with
the same rule table on the remote half of ssh to a login host, judged against
the compiled mount table, not the local one. `[agent_hook].login_hosts` lists
those hosts as exact host names or ssh aliases, compared with the host word
of the ssh command once any `user@` is removed. It holds no patterns: a
pattern that matched more than the site meant would judge a host the
compiled mount table does not describe. The list is optional, and when it is
empty the hook judges only local walks and leaves the remote half of every
ssh unjudged.

Two further rules were planned here, both ones the reaper enforces only after
the fact: a remote walk that is unbounded on an expensive mount must carry
its own `timeout` within the traversal budget, and walks are not fanned out
at or past the fan-out bar. They are deferred to issue #251, and the first
release of the hook ships without them. It **denies**, with the shim's
refusal text and the alternative named. `WALK_BLOCKER_UNSCOPED=1` turns a
denial into **ask**, so the user decides, not the agent. It **never rewrites a
command and never allows one**: a call it does not object to gets no decision,
and the user's own permission rules apply as before. A command it cannot read
is let through.

`deploy.py --system` installs the plugin under `[install].prefix` and writes a
managed-settings drop-in at `[agent_hook].managed_settings`, a literal
checked by the schema like every other root-write sink (ADR-0005). The key
is required and stamped even when `enabled` is false, as `[hooks.*].file`
is: a site that turns the hook off still has to name the drop-in, so that
`--system` and `--uninstall` remove one an earlier install wrote. The
drop-in force-enables the plugin and sets `USE_BUILTIN_RIPGREP=0`, so the Grep
and Glob tools' `rg` reaches the shim as well. `--uninstall` removes both.
Off the node, the same `agent/` directory is a plugin a site may publish to
its developers. Where it is published is the site's decision, and nothing here
assumes one.

## Consequences

- **The agent hook joins the agreement test.** The rows of
  `tests/argv_cases.py` run through the hook as a third consumer. The hook's
  own reading, of wrappers, substitutions, `cd X &&` and the remote half of
  ssh, gets rows of its own. A row the table and the shim agree on and the
  hook does not is a failure.
- **The hook's code is stdlib Python 3.9** (ADR-0015), for the node and for a
  developer's machine, whose Python the site does not choose either. With no
  `/proc/mounts` there, as on macOS, local walks are not judged and the ssh
  judgement is unaffected.
- **Claude's bundled `find` and `grep` still go around the shim on the node**
  for any command the hook's reader misses. That gap is Layer 2's, as every
  Layer 1 gap is. The rewrite that would close it is refused above, until
  Claude Code documents a way to change a command without granting it.
- **A hook denial is not a journal record.** Layer 1's refusal counts do not
  include it, and the reaper never sees the walk it stopped. Recording one
  is a new record source for every reader of the journal, and a decision of
  its own.
- **The engine now carries code for one named client program.** A change in
  Claude Code's hook protocol is a change here, and the version the hook was
  verified against is recorded in its docstring, measured on the tool.
- **A site that does not run Claude Code sets `enabled = false`** and its
  payload is unchanged, apart from the drop-in's path compiled into
  `deploy.py` so that an earlier drop-in can still be removed.
- **The remote `timeout` and fan-out rules wait for issue #251.** Until
  then a remote walk the rule table allows is let through whatever its
  timeout, and a fan-out is judged one command at a time.

## Re-measure when

Claude Code changes how its Bash tool resolves `find` and `grep`, how its
Grep and Glob tools find `rg`, or its `PreToolUse` contract: re-run the
hook's own tests against the new version, and re-check whether the bypass
still exists. If Claude Code documents a way to rewrite a command without
granting it, the refused rewrite is a new ADR, not an edit to this one.

## Site config touched

- `[agent_hook].enabled`
- `[agent_hook].login_hosts`
- `[agent_hook].managed_settings`
- read, not added: `[filesystems]`, `[install].prefix`, `[site]`,
  `[shim].unwrapped_tools` and `[slurm]`
- read once issue #251 lands: `[reaper].traversal_budget_s` and
  `[reaper].fanout_n`
