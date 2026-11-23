# ADR-0038: A disabled shell's hook counts toward the injection marker when its file proves it ours

**Status:** accepted, 2026-11-02
**Narrows:** ADR-0035, "`install.sh --system` writes `<spool>/first-hook.state`, one line: `injected <epoch>`, where `<epoch>` is `date +%s` read before the first hook is written; or `unknown`, where any hook of ours was already on disk when the install began and no marker stood."
**Evidence:** n/a, structural — see `docs/evidence.md`

## Context

ADR-0035's marker records `unknown` where any hook of ours was already on
disk when the install began and no marker stood. The scan that decided it
read only the hooks the stamped tables enable. A block or drop-in that an
earlier install wrote for a shell the site has since disabled was not
counted, yet it still puts the shim on PATH for every new shell of that
kind. With no marker standing, the install wrote `injected <now>`, a cutoff
after shells that were already covered, and a reader called them older than
the injection: the share read too high, in the direction nobody checks
(issue #246).

Counting a disabled hook is not as simple as counting an enabled one. An
enabled hook's file is walk-blocker's by the configuration's say-so. A
disabled hook's file may never have been ours: the block kinds sit in a shared,
administrator-owned startup file, and the drop-in's path is one the site
said not to use. Nothing in `--system` has walked its chain.

The alternatives:

- **Date only the shells the site enables now.** Its strongest form is that
  the marker would then answer a question the configuration can state, and
  a disabled shell is outside what the install claims to cover. Rejected:
  the reader wants the instant before which a shell could not have carried
  the shim's PATH, and a disabled shell's surviving hook carries it. A
  marker that ignores it is late, which ADR-0035 rules out.
- **Count a disabled hook as an enabled one is counted**: a block by its
  opening marker, the drop-in by its file alone. Its strongest form is one
  rule for every shell. Rejected: a file at the drop-in's path that is not
  ours would read as a hook, and so would a link, which the install never
  writes; and a file under a chain another account can write could be
  swapped for a FIFO between the type check and the read, which would hang a
  root install.
- **Call `uninstall_claim()` from `--system`.** Its strongest form is that
  it is the proof, already written and tested. Rejected: it is built for the
  uninstall, with a writing run that refuses (exit 3) and a dry run that
  prints notes, neither of which an install over a file it does not write
  should do.

## Decision

`install.sh --system` counts a hook the site has disabled toward "a hook of
ours on disk" only where its file proves it ours by the proof `--uninstall`
asks that a disabled hook's file is ours: a regular file and not a link,
under a directory chain no other account can write, carrying a walk-blocker
block for the block kinds or opening with the drop-in's generated header for
the drop-in. The uninstall's checks on a block file's own owner and mode
(root, no group or other write) are not part of the proof: they refuse a
write, and this install does not write the file. The proof is quiet: no output, no refusal, no exit. A file that
fails any part of it is not counted and is not read past the part it
failed. A disabled hook naming an enabled hook's file is skipped, as
`--uninstall` skips it.

## Consequences

- An install over a disabled shell's surviving hook, with no marker
  standing, records `unknown`, and a marker an earlier install of the run
  wrote is kept while that hook stands, as ADR-0035 keeps one for an enabled
  hook.
- A disabled shell's hook under an untrusted chain is not counted. The
  marker can then read `injected <now>` over a hook that is ours. That is
  the cost of never reading such a file as root, and it is rare where the
  sanctioned route, `deploy.py --system`, installs: before `install.sh`
  runs, it walks the chain above every hook file, enabled or not, and
  refuses one where another account could replace the file. Its walk
  accepts a sticky directory above the file's own directory, which this
  scan's walk does not.
- A disabled shell's block of ours under a trusted chain, in a file not
  owned by root or writable by group or other, counts toward the marker,
  while `--uninstall` refuses
  that file (exit 3) and leaves the block in place. The block still puts
  the shim on PATH, so leaving it out would make the marker late.
- `uninstall_claim()` and the new scan share the proof's type check and
  content check, `hook_file_plain()` and `hook_content_ours()`, so those
  two cannot drift apart, and both walk the chain with
  `require_trusted_chain()`. The uninstall keeps its own chain walk,
  refusals and notes unchanged.
- An enabled drop-in still counts by its file alone, as before: it is
  walk-blocker's by the configuration, and its file is never read.
- Pinned by `tests/test_install.py`: a disabled zsh block or fish drop-in
  of ours records `unknown`; a headerless drop-in, a block file with no
  block of ours, an absent file, and a symlink to a file carrying our block
  each record `injected`; a disabled drop-in naming the enabled bash hook's
  file, which opens with the drop-in's header, is skipped and records
  `injected`; and a block of ours under an untrusted chain is neither read
  nor refused.

## Re-measure when

n/a: structural.

## Site config touched

- `[hooks.<shell>].enabled` — which hooks the scan proves rather than
  counts.
- `[hooks.<shell>].file` — the file it reads.
