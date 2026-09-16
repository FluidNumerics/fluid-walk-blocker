# ADR-0004: Deployment is root-owned only; a per-user mode was rejected

**Status:** accepted, 2026-09-16
The Decision's clause "none of it writable by the account being monitored" is narrowed by ADR-0012: writable is excluded, readable is not.
**Evidence:** held privately by Fluid Numerics, keyed ADR-0004 — see `docs/evidence.md`

## Context

The first working deployer had two modes. `deploy.py --user` needed no sudo:
it rsynced the payload to a per-user directory under `/var/tmp`, ran
`install.sh --user` to prepend a PATH block to `~/.bashrc`, and installed the
reaper as a `systemd --user` timer under `~/.config/systemd/user/`.
`deploy.py --system` printed the equivalent root commands and refused to run
them, gated behind an inert `--i-have-approval` flag, waiting on the site
owner's sign-off to be handed to whoever administers the node.

Both layers were built and validated against this design, and nothing was
ever deployed with it — but the design itself had a property worth naming
before it shipped: **everything `--user` mode installed lived entirely under
the monitored account's own control.** Both the shim (`~/.bashrc`) and the
reaper (the `systemd --user` timer) were files and units that account could
edit or disable with ordinary, unprivileged commands. `deploy.py`'s own
`--uninstall`, at the time, was exactly that command:
`systemctl --user disable --now walk-blocker.timer`, plus removing the two
unit files and stripping the `~/.bashrc` block — nothing here needed root.

That is not a hypothetical concern specific to a malicious user. Anything
running as the monitored account — a shell script, a cron job, an agent
carrying out an instruction it was given, deliberately or by being steered
into it — has exactly the same access to those same commands. A guardrail
whose backstop layer can be turned off by the account it is meant to guard,
using nothing more than the permissions that account already has, is not a
backstop against that account.

## Decision

Delete `--user` mode. `--system` becomes the only deployment path, and it
stops being print-only: gated on real authentication (the operator already
has root — checked with `id -u` in the shell installer and `os.geteuid()` in
`deploy.py`, never reimplemented) plus explicit authorization
(`--i-have-approval`), it self-executes — root-owned shims and payload under
`[install].prefix`, the PATH block above the guard clause in the system-wide
shell rc named by `[hooks.<shell>].file`, the audit trail under
`[install].spool_dir`, and the reaper as a root-run `systemd` timer under
`[install].unit_dir` — none of it writable by the account being monitored.
`--uninstall` reverses it, gated on root alone: reversing a control is the
safer direction, so it does not need the same ceremony as installing one.

This repo's own agent sessions never pass `--i-have-approval` or otherwise act
as root, unconditionally. That rule does not change. What changes is that the
artifact those sessions hand off is now one an authorized human can actually
run, rather than one that only prints instructions for them to retype by hand.

## Consequences

- Root-owned artifacts are out of reach of the account being monitored.
  Disabling either layer now requires the same authority that installing
  them did, which is the property `--user` mode did not have.
- **"Root-owned" is asserted at install time, not inferred from the copy.**
  This decision was, for a while, only as true as `cp -a` — which preserves
  the *source's* ownership when run as root, so a deploy from an ordinary
  user's checkout (the documented workflow) left the shim script and the
  installer under the prefix owned by that account. Since the systemd unit
  runs the installer as root on every poll and every user on the node execs
  the shim script as the first thing in every shell, that was strictly worse
  than the `--user` mode this record deleted: it reached every *other*
  account too. `deploy.py` now reasserts `root:root`, strips group and other
  write, and calls `unowned_by()` to refuse to write the systemd unit if
  anything under the prefix still fails the test. The property this record
  rests on is checked, in the same spirit as `verify_hook()` below.
- Clean cutover, not a migration: nothing was ever deployed under `--user`
  mode at the reference deployment, so there was no live state to reconcile.
- Testing the root+approval gate needed dependency injection rather than
  trusting environment state — a stub `id` on `PATH` for `install.sh`, and
  `monkeypatch.setattr(deploy, "_is_root", ...)` for `deploy.py` —
  deliberately not an environment-variable override, which would itself be
  exactly the kind of unprivileged bypass surface this record exists to close
  off. The fixtures in `tests/test_install.py` and `tests/test_deploy.py` are
  the only sanctioned way to make the gate pass without root.
- `verify_hook()`'s "prove it, don't assert it" discipline (ADR-0008) carries
  over unchanged: it was always driven by `$BIN` and the hook-file variable,
  not hardcoded to `~/.bashrc`, so pointing it at the system-wide rc needed no
  logic changes.
- Real system bash reads a hardcoded path (`/etc/bash.bashrc` or
  `$HOME/.bashrc`), never one named by a flag, so the positive "the hook
  really fires" test cannot exercise the real binary against a sandboxed
  file the way the old `--user` tests could, by pointing `$HOME` at a
  fixture. The test suite substitutes a `bash` stub that mirrors the same
  documented `SHLVL`/`SSH_CLIENT` gate against an env-controlled path
  instead — a test-only technique, invisible to `install.sh` itself, not a
  production escape hatch.

## Re-measure when

n/a: structural. No property of a site makes a control that the monitored
account can disable into a backstop against that account.

## Site config touched

- `[install].prefix`
- `[install].unit_dir`
- `[install].spool_dir`
- `[hooks.<shell>].file`
