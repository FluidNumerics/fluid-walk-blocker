---
applyTo: "node/deploy.py,node/shim/install.sh,src/walk_blocker/build.py,src/walk_blocker/stamp.py,tests/test_deploy.py,tests/test_install.py"
description: Reviewing the root-run deployer, the shell installer it invokes, and the build step that stamps both.
---

# This code runs as root on a shared node

`deploy.py --system` is run by an authorized sysadmin as
root. `install.sh` is also executed as root by the systemd timer on every
reconcile (`[timer].on_calendar`). Assume every path here is a root-write
sink. Every one of those paths is a literal stamped in by `walk-blocker build`
from `site.toml` (ADR-0013); the build is where a bad value is refused, and
`src/walk_blocker/build.py` and `stamp.py` are in scope here for that reason.

## Pairs that must stay in step

These diverged repeatedly in the predecessor. When a check is added to one,
check the other in the same review:

| Pair | What went wrong |
|---|---|
| install ↔ uninstall | Repeatedly: prefix validation, a late-read helper, the hook-file leaf check, and a flag reused for the wrong question |
| preview ↔ execute | More than once: the preview advertised a command the installer then refused |
| `deploy.py` ↔ `install.sh` | The shell side lacked an `S_ISREG` check the Python side had carried for a long time |
| `site.toml` ↔ compiled literal | A key added to the schema and not the generator, or the reverse, is the install↔uninstall divergence shape in a new place. `walk-blocker build --check` is the oracle; a stamped line without a `# GENERATED from` marker, or a marker without a schema key, is the finding |

A guard that a caller must remember to apply will be forgotten by the next
caller. Prefer putting it inside the function that needs it — "three of the
four callers had it" is the shape that keeps producing these.

## Path checks

State which of these a check covers, and cover or name the rest:

- the **ancestors** — and whether they are walked lexically or physically
  (`stat` follows symlinks, `dirname` does not)
- the final component's **type** — symlink, and also *regular file*; a `stat`
  that reads uid and mode answers happily for a FIFO or a device node
- the final component's **ownership and mode**
- the **contents**, when the thing is a directory

A literal path is not a trusted path. Shape validation happened at build;
filesystem-state validation can only happen here, and both install and
uninstall must run it.

## Preview must predict, not protect

A preview writes nothing, so a check there has nothing to guard — but it still
has a refusal to predict. Gate the *refusal* on whether the run will write;
never gate the *check* itself off entirely, or the preview advertises a command
that fails the moment the install is run. Report a clean run with no note: a
warning that fires every time is one the operator learns to skip.

## Hook files are config

A hook shell is a `[hooks.<shell>]` entry with `enabled`, `file`, `package`
and `gate`, plus a block function that writes the sourced block for that
shell's syntax and a verify function that re-execs the shell against the
installed block. A new shell is all three, never a hardcoded startup-file
path. Whether a shell is `required` or `best-effort` is the site's call from a
census of its login shells (ADR-0008), so a review may ask whether the block
and verify functions are correct for the shell and may not ask the site to
reclassify it.

## Do not suggest

- Reintroducing path flags to `deploy.py` (ADR-0005) or requiring a root-owned
  source tree (ADR-0006). Both are settled.
- Reading `site.toml`, or any configuration file, on the node (ADR-0013). The
  file in the payload is a record of what was built, not an input.
- Running a writing run -- an install, or a root `--relink` -- from anywhere but
  `$PREFIX/shim`.
