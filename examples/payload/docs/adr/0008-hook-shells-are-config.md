# ADR-0008: Hook shells are a config list with required and best-effort classes, verified not assumed

**Status:** accepted, 2026-09-16
**Evidence:** held privately by Fluid Numerics, keyed ADR-0008 — see `docs/evidence.md`

## Context

The shim reaches a process only if some startup file put the shim directory on
`PATH` before that process was exec'd. Which startup files fire for
`ssh host 'cmd'` — the incident shape — is a property of the shell, and it is
different for each shell in use.

**bash.** The installer's first justification, "the distribution's bash sources
the system rc file for `ssh host 'cmd'`", was true but not for the stated
reason, and the real reason has a bypass in it. Measured on the tool with a
throwaway `HOME`, the system bashrc is read non-interactively only when all
three hold: bash built with `SSH_SOURCE_BASHRC`, **and** `SSH_CLIENT` or
`SSH2_CLIENT` set (or stdin a socket), **and** `SHLVL < 2`. Three things
follow. `ssh host 'bash -c "find ..."'` runs the search in a second-level shell
that never reads the file — an ordinary bypass, joining the absolute-path,
private-`PATH`, container and batch-script bypasses already on Layer 1's list,
and one more reason Layer 2 exists. `/etc/profile.d` would not fire at all,
because a non-login non-interactive bash reads no profile. And the block must
sit **above** the interactivity guard the distribution's system bashrc
typically opens with, or the non-interactive shell returns before reaching it.

**zsh.** A user whose registered login shell is zsh is exec'd straight into zsh
by `ssh host 'cmd'`; bash never runs, so the bash hook never fires regardless
of its condition. Measured on the tool: zsh reads its global `zshenv`
**unconditionally** — login or not, interactive or not, and `-f`/`--no-rcs`
does not skip it, because that flag only unsets the `RCS` option, which gates
the *later* files. zsh's `$SHLVL` increments only on an actual exec, never on a
subshell, substitution or pipeline, so every genuine new zsh process re-reads
the file. Debian-family packaging relocates it from `/etc/zshenv` to
`/etc/zsh/zshenv`. This is the stronger of the two mechanisms, not a weaker
parallel: it closes the registered-login-shell gap because the file fires
unconditionally, and it closes the nested-shell gap — zsh typed by hand inside
an already-hooked bash inherits `PATH` *unless* the user's own rc rewrites it,
which cannot be checked without reading private dotfiles — because the hook
re-asserts the shim directory on every zsh start regardless of what the parent
handed down.

**fish.** Measured on the tool with `strace -e trace=openat fish -c true`: a
non-interactive, non-login fish opens `/etc/fish/conf.d/` and
`/etc/fish/config.fish` unconditionally, the same property zsh's `zshenv` has
and bash's system rc does not. fish's own documentation says so and names
ssh/scp/rsync non-interactive invocations as the case that bites. fish also
has an idiomatic drop-in mechanism — `conf.d/*.fish` snippets read in a defined
precedence order — specifically so an administrator can add configuration
without editing the shared `config.fish`. That changes the shape of the hook
entirely: `/etc/fish/conf.d/walk-blocker.fish` is a dedicated file this
installer generates in full, so there is nothing to preserve, nothing to back
up, and no markers to strip.

**Hook files may be package-managed conffiles.** A system rc file belonging to
the shell's package is protected configuration: installing a block makes it
*modified*, and the next upgrade of that package is a conffile conflict with
two outcomes, neither good and neither detected without a check — an
interactive upgrade prompts, and an administrator who takes the maintainer's
version silently deletes Layer 1's hook; an unattended security upgrade hits
the same conflict and defers the shell update, leaving the node on an
unpatched shell. This is a third-party dependency Layer 1 rests on and does not
control, and it fails in the direction noticed last: interactive shells keep
working, and `ssh host 'cmd'` goes quiet.

Which shells matter is not a property of the shells. At a reference deployment
a census of registered login shells (`getent passwd`) and a live census of
shell processes actually running found bash overwhelmingly registered, zsh
registered for a few accounts and reached by nesting for a few more, and fish
present only as a nested, per-user shell that was at points not installed as a
system package at all. Every `sh` process found was `sh -c '<command>'`
launched by other tooling — noise, not a personal interactive shell.

## Decision

**`[hooks.<shell>]` is a list, one table per shell, each with `enabled`, the
`file` the block is written to, the `package` that likely owns that file, and
a `gate` of `"required"` or `"best-effort"`.**

A **required** shell fails the install (`--system` exits
non-zero) if `verify_<shell>_hook` cannot prove the file fires under
remote-command conditions with the shim directory stripped from `PATH` — the
probe must not pass merely because the caller's own `PATH` already carries it.
For bash that is `SHLVL=0`, `SSH_CLIENT` set, stdin `/dev/null`, requiring a
wrapped name to resolve inside the shim directory. For zsh it is `ZDOTDIR`
blanked, so a stray per-user zshenv cannot supply a false pass, and **no** ssh
condition: the file fires unconditionally, and reproducing a condition that
does not exist would assume something about the shell — the mistake
verify-not-assume exists to prevent. A required shell's binary being absent is
a hard verify failure, because an automatic pass on "absent" silently converts
"unchecked" into "verified".

A **best-effort** shell is written and verified only when its binary resolves
at install time (the same "only wrap what already resolves" policy the link
farm uses), and its verification never gates: a failure prints a warning and
the install still reports success.

**The reconcile re-checks every hook and reports rather than repairs.** It
reads each hook file for the block markers (`# >>> walk-blocker >>>`) and,
where they are present, re-runs the verify. A bad state is recorded to journald
under the `walk-blocker` tag, beside the shim's escape-hatch records, so one
query answers both "what overrode Layer 1" and "when did Layer 1 stop being
installed". Rewriting a root-sourced file unattended is a larger action than a
reconcile has standing to take, and the install path's own refusals exist
because that write is dangerous. The reconcile **never fails**: a Layer 1
diagnosis must not be able to stop the Layer 2 backstop, whatever the unit
file says this week. Healthy is silent. Best-effort hooks are checked only
when the binary is on `PATH`, so a node without that shell does not get a
"file absent" report on every poll forever for a file it was never going to
need.

**Which class a shell gets is a census result at the site, not a property of
the shell.** A required shell's absence is an anomaly; a best-effort shell's
absence is ordinary. Do not soften one to match the other.

## Consequences

- **Block content and verification are per shell; file mechanics are shared.**
  `prepend_block()`, `strip_block()` and `require_plain_hook_file()` are
  parameterised over *which file*, because the one-time `.walk-blocker.orig`
  backup (written on first install, never overwritten by a later reconcile),
  the mktemp/chmod/mv race-avoidance, and the symlink and ownership refusals do
  not depend on which shell reads the result. The block text and its verify
  stay in separate per-shell functions so a prose change meant for one shell
  cannot silently land on another's file; a test pins that the blocks stay
  distinguishable. The bash block has an `SSH_CLIENT`/`SHLVL` story to tell
  and an interactivity guard to stay above; the zsh block has neither.
- **The fish hook's symlink refusal is weaker, deliberately.** `prepend_block()`
  refuses a symlink because `strip_block()` *reads* through it before
  rewriting, so a planted link would republish its target's contents into a
  new world-readable file. The fish writer never reads its file, only
  overwrites it via `rename()`, which replaces a destination symlink rather
  than following it — so the only thing to refuse is "something already at
  this path is not what the installer put there". Its directory is created
  with `install -d -m`, which sets the mode on every directory it creates,
  intermediates included, unlike `os.makedirs`.
- **The verify runs every hook without short-circuiting**, so a single failed
  install reports every problem it has rather than one-fix-one-discover.
- **Uninstall removes every hook file unconditionally**, whether or not the
  shell that would have read it still resolves. An uninstall cleans up what
  was written; it does not condition that on the reader still being around.
- **The `SSH_CLIENT` export is a third-party detail the bash hook rests on.**
  Where a site runs a second ssh transport beside OpenSSH, one bash hook covers
  both only because both export `SSH_CLIENT`. That is load-bearing and appears
  nowhere else in this tree. If the second transport stops exporting it,
  Layer 1 goes quiet for `ssh host 'cmd'` — exactly the incident shape — while
  continuing to work interactively, which is the failure noticed last.
- **`[hooks.<shell>].package` is what the reconcile names** as the likely
  conffile actor when it reports a hook gone missing, so the operator reading
  the journal knows which upgrade to look at.
- An installer reporting success it did not achieve is the same defect as an
  audit trail reporting a kill that did not land; the required/best-effort
  split exists so that "verified" and "not applicable" are never spelled the
  same way.

## Re-measure when

At first deploy, and whenever the account population changes. Two censuses,
both cheap and both unprivileged:

- **Registered login shells**: `getent passwd`, human uid range, grouped by
  shell. A shell that is anyone's registered login shell is exec'd directly by
  `ssh host 'cmd'` and must be `required`.
- **Shells actually running**: a live process census, distinct users per shell
  binary, with parent process noted. A shell reached only by nesting inside an
  already-hooked shell inherits `PATH` unless its rc rewrites it; this cannot
  be verified without reading private dotfiles, which is out of bounds, so an
  unconditional-read hook for that shell is the answer rather than an
  assumption about inheritance.

A shell that moves from nested-only to registered moves from `best-effort` to
`required` in the same change.

## Site config touched

- `[hooks.<shell>].enabled`
- `[hooks.<shell>].file`
- `[hooks.<shell>].package`
- `[hooks.<shell>].gate`
