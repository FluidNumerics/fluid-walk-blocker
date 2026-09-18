# ADR-0021: Root is the authorization; the approval flag is removed

**Status:** accepted, 2026-09-18
**Narrows:** ADR-0004, "plus explicit authorization (`--i-have-approval`), it self-executes"
**Evidence:** held privately by Fluid Numerics, keyed ADR-0021 — see `docs/evidence.md`

## Context

ADR-0004 built the install gate out of two parts: real authentication, which
is `os.geteuid()` in `deploy.py` and `id -u` in the shell installer, and an
explicit authorization flag, `--i-have-approval`, which the operator had to
type on top of already being root.

The second part was written when the first deployment had not happened and
nobody had yet run the installer against a live node. It encoded a real
worry — that a script capable of rewriting system-wide shell startup files
and installing a root timer should not do so on a stray invocation — but it
answered that worry by asking permission from someone who already had it.

The person who runs this holds root on the target node. That is not a
credential they borrowed for the occasion; it is a standing position of
trust, granted by whoever operates the site, and it already carries the
authority to do everything this installer does and a great deal more by
hand. A flag that asks such a person to confirm they meant it does not add a
check. It relocates the judgement from the human who has the context to a
string comparison that does not, and it does so in the one place where the
human's judgement is least in doubt.

Three alternatives were considered.

**Keep the flag.** Its strongest form is that a second token makes an
accidental install impossible, and accidents on a shared login node are
expensive: the install rewrites `/etc/`-level shell startup files that every
account on the node reads. The answer is that the flag never prevented an
accident that root alone would not have prevented. Every path to running it
is deliberate — becoming root, changing to the payload, naming the script.
An operator who did all of that and did not mean to install it is not an
operator a flag saves.

**Replace it with an interactive confirmation prompt.** Rejected for two
reasons. It cannot run under the systemd unit or any non-interactive
invocation, so it would need a bypass flag, which is the flag again under a
name that sounds safer. And a prompt trains the answer: the operator who
sees it every deploy types `y` without reading, which is worse than no
prompt because it looks like a check.

**Keep the flag only for the first install at a site.** Rejected as
unimplementable without state, and state about whether a site has installed
before is exactly the kind of node-side configuration ADR-0013 forbids.

## Decision

The gate is root, and only root. `--i-have-approval` is removed from
`deploy.py` and from `install.sh`, and `--system` installs.

Inspection moves to `--dry-run`, which needs no privilege: it makes every
check the install makes, names any check it could not make as an ordinary
user rather than reporting it clean, prints the rendered units and the exact
command sequence, and writes nothing. `--uninstall` and `--verify` are
unchanged. `--verify --dry-run` is rejected rather than ignored, because a
caller who thinks a dry verify is the safer one has misunderstood which
command to run.

The reciprocal guarantee ADR-0004 rests on is unchanged and is now carried
entirely by the root check: the install still refuses to write a unit or
enable a timer over a payload it could not make root-owned, and `install.sh`
still refuses a writing run from anywhere but the deployed copy.

## Consequences

- **The safety property that remains is the one that was always doing the
  work.** Root ownership of what gets installed, asserted and re-checked, is
  what keeps the monitored account out of the guard. The flag never
  contributed to that.
- **This repository's own agent sessions are now the only thing standing
  between an automated session and a live install**, where previously the
  flag was a second barrier. ADR-0004's sentence "this repo's own agent
  sessions never pass `--i-have-approval` or otherwise act as root,
  unconditionally" survives with its first clause spent. `CLAUDE.md` states
  the rule as an effect — never run `deploy.py` or `install.sh` as root, in
  any mode — rather than as a token to withhold, because a rule phrased as
  "do not pass X" fails open the moment X is deleted.
- **`install.sh`'s internal flag is positive.** `WILL_WRITE` defaults to 1
  and `--dry-run` clears it. The variable it replaced was negative, and every
  call site tests it in the "is this run about to write" sense, so keeping
  the old name while inverting the default would have flipped each one. A
  dropped negation there is a dry run that writes as root.
- **`require_deployed_copy()` is gated on "this run writes", not on a mode.**
  It was keyed to the approval flag only because that flag was how a writing
  run announced itself; ADR-0005's guarantee that the installer refuses to
  run out of a checkout is unchanged, and would have weakened silently if the
  gate had been re-keyed to the wrong condition.
- **A dry run reaches the child installer, which must be told.** `deploy.py`
  invokes `install.sh --system --dry-run` for the relayed report. The flag is
  load-bearing: without it that call installs Layer 1 out of the checkout,
  which is the worst failure available to a command whose whole purpose is to
  not do anything.
- **The relay filter is keyed on `--system`, not on the removed flag.**
  `deploy.py` strips `install.sh`'s own standalone command out of the relayed
  output, because run directly it copies nothing and points every shim back
  at the source directory. Keying that filter on a string this tree no longer
  emits would have disabled it while leaving its tests passing over an empty
  list.
- **The test seam is unchanged and now carries the whole gate**: a stub `id`
  on `PATH` for `install.sh`, `monkeypatch.setattr(deploy, "_is_root", ...)`
  for `deploy.py`, and never an environment-variable override, which would be
  the unprivileged bypass ADR-0004 exists to close.
- **An asymmetry is shipped knowingly:** a dry install needs no root, but
  `--uninstall --dry-run` still does. Extending the symmetry was not required
  by this decision and changes who can enumerate a teardown on a shared node,
  so it is filed rather than folded in.
- ADR-0008's exit-code sentence and ADR-0005's and ADR-0006's prose refer to
  the flag by name. They are descriptions of the command's spelling, not
  decisions that rest on it, so they are reworded where they appear and
  neither record is narrowed.

## Re-measure when

n/a: structural. No property of a site changes whether asking a root operator
to confirm they are root adds a check.

## Site config touched

none
