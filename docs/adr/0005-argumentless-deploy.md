# ADR-0005: `deploy.py` takes no path arguments; root-write sinks are literals

**Status:** accepted, 2026-09-16
The closing clause "a commit, not a flag" is narrowed by ADR-0013: the literals are generated from `site.toml`, so a location change is a `site.toml` change plus a build.
**Evidence:** held privately by Fluid Numerics, keyed ADR-0005 — see `docs/evidence.md`

## Context

`deploy.py` accepted four paths as CLI options:

```text
--prefix       the install prefix          (now [install].prefix)
--unit-dir     /etc/systemd/system         (now [install].unit_dir)
--bashrc-file  the system-wide shell rc    (now [hooks.<shell>].file)
--audit        the audit trail file        (now [install].spool_dir / [install].audit_filename)
```

They existed for tests. Every one of them is also a place this installer
writes **as root**, so each acquired validation as review found what an
arbitrary value could do:

- `--prefix` reached `chown -R root:root`, `chmod -R` and `rm -f
  $PREFIX/bin/<tool>`. `--prefix /` meant `rm -f /bin/find /bin/grep /bin/du`;
  `--prefix /usr/local` meant `rm -f /usr/local/bin/fd`, which is where a
  locally built `fd` plausibly lives. It grew an absolute-path check, a
  component-count check, a canonicalization step, a payload marker
  (`.walk-blocker-payload`), a trust-chain walk and a traversability check.
- `--prefix` and `--audit` are interpolated into `ExecStartPre=`/`ExecStart=`,
  which word-splits on whitespace and expands `%`-specifiers and `${VAR}`. A
  prefix of `/opt/walk blocker` produced a unit whose payload path was two
  arguments, and the install returned 0.
- `--prefix` and `--audit` are *also* interpolated into the shell-rc block
  that every login shell sources and that `verify_hook` re-execs as root.
  `--audit '/var/log/x;id>/tmp/pwn'` contains no whitespace, passed the
  systemd check, and ran `id` in every login shell on the node.
- `--bashrc-file` reached `strip_block()`, which reads the file and writes it
  back as a 0644 regular file. A symlink there republished a mode-0600
  target's contents to every user on the node.
- `--unit-dir` could not work at all. Units written anywhere else are not on
  systemd's `UnitPath`, while the install runs `systemctl enable --now
  walk-blocker.timer` — by **name**. A non-default value either failed after
  the payload and the shell rc had already been modified, or silently enabled
  an older unit that really was in `/etc/systemd/system`.

Many review rounds separated the first of those checks from the last, and
they arrived one flag at a time: `--prefix` was hardened again and again,
`--audit` once, `--unit-dir` never, and `--bashrc-file` was never looked at.
A five-column table (`ROOT_WRITE_PATHS`) was added to stop them drifting
apart, which helped, and did not change the underlying arithmetic: four
root-write sinks, each reaching two or three of {systemd unit line, sourced
shell block, recursive filesystem operation}, is a validation surface that
grows as the product of those sets.

`--unit-dir` is what settled it. A flag whose every non-default value can only
fail or do damage is not a flag with a validation gap; it is a flag.

## Decision

**The four options are removed from the CLI.** They are module constants, and
`main()` populates the args namespace from them. `argparse` rejects
`--prefix /tmp/x` with `unrecognized arguments` and exit 2.

`not_a_default()` re-checks all four inside `system_execute` and
`system_uninstall`, before any command runs. That is redundant with `main()`
by design: "the CLI does not offer it" and "the code will not do it" are
different guarantees, and the second is the one this record claims.

The test seam is the constants. `tests/test_deploy.py` has one autouse fixture
that points all four at the test's `tmp_path`, and `_args()` builds its
namespace from `deploy.default_paths()` so the two cannot drift.

## Consequences

**What this deletes is argument-shape validation.** `unsafe_prefix()`,
`unembeddable_in_unit()`, `shell_unsafe()` and `wrong_unit_dir()` are gone,
along with the `_UNEMBEDDABLE` and `_SHELL_UNSAFE` patterns, the five-column
`ROOT_WRITE_PATHS` table, the preview's per-value warnings, and the preview's
logic for forwarding non-default locations into the command it advertises.

Those rules were not discarded, they were relocated. Each is now asserted once
against the literals, in
`test_the_installer_paths_are_shaped_for_every_sink_they_reach` — and, per
ADR-0013, once more in the schema that validates `site.toml` before the
literals are generated. Asserting them there is strictly stronger than
checking them at runtime: a test cannot be satisfied by an operator who never
passes the flag, and it cannot cost a false refusal on a real deploy.

**What this does not touch is filesystem-state validation, and could not.**
Whether the prefix's parent is user-writable on a given node, whether any
ancestor of it is owned by someone other than root, whether ordinary users can
traverse it, whether the shell rc is a symlink under a dotfile manager,
whether the prefix already holds files this install did not create — none of
that is answerable by constant-folding. `untrusted_prefix_chain()`,
`untraversable_for_users()`, `unowned_by()`, `irregular_target()`,
`write_unit()`'s `O_NOFOLLOW` and the payload marker all remain, and
`validate_root_write_paths()` still runs on both the install and the uninstall
path. **A literal path is not a trusted path.**

**The preview can no longer diverge from the install.** Two separate defects
came from those two halves being handed values independently: an early review
found the preview relaying `install.sh`'s own standalone command, and a much
later one found it describing a `--unit-dir` the install would refuse. They
now read the same four constants.

**Tests can no longer install to a real location by forgetting a flag.** The
autouse fixture is autouse for that reason — previously a test that omitted
`--prefix` would have used the real prefix.

**`install.sh` keeps its own flags, but not every mode.** It is a POSIX shell
script whose own test suite drives it into a tmp tree through those flags, and
an authorized operator may still run it directly for a **preview** or an
unprivileged **`--relink`**. What it will not do any more is a writing run
-- an install, or a root `--relink` -- from anywhere but the deployed copy
under the prefix: `require_deployed_copy()` refuses those, because `link_farm` points
every shim at `$HERE/guard.sh` and a checkout is writable by the account that
owns it.

A root teardown out of a checkout is refused too, though by a different guard:
`load_wrapped_names()` will not source a `wrapped_names.sh` that is not
root-owned. The effect is the same and the reason is worth keeping distinct —
one is about where the shims will point, the other about what this script
executes.

Its own guards therefore still matter for the modes that remain: `shquote()`
around both assignments written into the sourced block, the symlink and
ownership refusals on the shell rc, and `load_wrapped_names()` validating
before it sources. Those are "the half that holds when `install.sh` is invoked
directly", and argumentless `deploy.py` does not cover that path — see
ADR-0004 for why the standalone path is nonetheless not the sanctioned one.

An earlier draft of this record claimed a broader standalone role for
`install.sh` than the installer enforced; review caught the contradiction and
the record, not the installer, was corrected.

**A future location change is a commit, not a flag.** If the payload ever has
to move, the constant changes and the tests move with it. That is the intended
cost: relocating a root-run installer's write targets is a decision worth a
diff and a review, not a command-line argument.

## Re-measure when

n/a: structural. The argument is about the product of sinks and value shapes,
and no site fact shrinks it.

## Site config touched

- `[install].prefix`, `[install].spool_dir`, `[install].audit_filename`,
  `[install].unit_dir`, `[install].staging_parent` — via ADR-0013, which
  generates the literals from them
- `[hooks.<shell>].file`

**This is not to be "fixed" by a later agent.** The question is settled here
because it kept coming back. If it is revisited, revisit it with a new ADR
that names the clause above it narrows, and with a measurement that
contradicts this record — not with an implementation.
