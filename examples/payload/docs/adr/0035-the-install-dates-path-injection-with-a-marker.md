# ADR-0035: The install dates PATH injection with a marker in the spool

**Status:** accepted, 2026-10-13
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

Layer 1 covers a shell only if the shell started after the hook was written:
`docs/operating.md` lists "a session that started before the install" among
what goes around it, and ADR-0001 keeps Layer 1 advisory. A reader that wants
the share of a node's processes that predate the injection needs the moment
the first hook was written, and nothing on the node records it. Every
nearby marker dates something else:

- **The install prefix's birth time** dates the first `deploy.py --system`,
  which makes the prefix before `install.sh` writes any hook, so it is early
  by however long the install takes. An uninstall keeps the prefix, so a
  later install is dated by the first one.
- **A hook file's birth or ctime** moves on every install: each write is a
  temporary file renamed over the hook file, a new inode each time.
- **`wrapped_names.sh`'s ctime** dates the latest deploy.
- **`linked-names.state`** is rewritten by every relink and removed by the
  uninstall.

A reader built on any of them overstates or understates the pre-injection
share, and has to say so in a caveat.

The alternatives for the marker itself:

- **A line in an existing memory.** Its strongest form is that the spool
  would hold no new name. Rejected: every memory is rewritten wholesale by
  its own writer, so the line would have to be carried forward by two
  writers that otherwise share nothing, and the uninstall removes both.
- **The wall clock at the first successful hook verification.** Its
  strongest form is that it dates the moment Layer 1 was proven to work.
  Rejected: a hook is on disk, and fires for new shells, before the proof,
  so a session started in between is covered and would be counted as older.
- **Uptime and a boot id, as the relink's memories use.** Rejected: the
  marker is read across reboots, and a boot id cannot order two times on
  either side of one. The memories need the boot id because a volatile
  journal forgets; this one is read from the file.
- **`now`, whenever no marker is found.** Its strongest form is that it
  is always a time, never "unknown". Rejected: an install over hooks that an
  earlier release wrote, which kept no marker, would date the injection
  after sessions that were already covered. The reader would then call
  covered sessions older than the install and the number would be wrong in
  the direction nobody checks.

## Decision

`install.sh --system` writes `<spool>/first-hook.state`, one line:
`injected <epoch>`, where `<epoch>` is `date +%s` read before the first hook
is written; or `unknown`, where any hook of ours was already on disk when
the install began and no marker stood. It is a new inode renamed over the
name, mode 0640, from the pinned spool, as ADR-0025 writes everything there.
A marker that is a regular file is kept while a hook of ours stands. With
no hook on disk the run it dated has ended (an uninstall that could not reach
the spool leaves it behind), so the install replaces it. A link or any other
non-directory at the name is replaced, never followed; a directory there is
left as it is, and no marker is written.

`install.sh --relink` never writes it. `install.sh --uninstall` removes it,
with its write-and-rename file, because the injection it dates has ended: a
later install begins a new run and dates it anew. It is an installer-owned
spool name: `deploy.py` repairs a regular file there and refuses a link,
like the rest.

## Consequences

- A reader takes `injected <epoch>` as the instant before which a shell
  could not have carried the shim's PATH. Early by the length of the hook
  writes, never late. Where the line is `unknown` or the file is absent, as
  on a node whose payload predates this record, the reader falls back to
  what it had.
- The marker is not a proof that a session is covered. Layer 1 stays
  advisory (ADR-0001): a shell born after the marker can still bypass the
  shim.
- A node upgraded in place records `unknown` until it is uninstalled and
  installed again. That is the honest answer for a run whose start was not
  recorded, and it is the cost of refusing to guess.
- `date +%s` joins the installer's coreutils (`docs/node-requirements.md`).
  An unreadable answer records `unknown` and does not fail the install.
- Pinned by `tests/test_install.py` (first install, the date read before
  the hook writes, reinstall, install over existing hooks including a
  best-effort shell's alone, a stale marker with no hook on disk, relink,
  uninstall, a planted link, a planted directory) and by
  `tests/test_deploy.py` (the literal agrees across the two files).

## Re-measure when

n/a: structural.

## Site config touched

none
