# ADR-0026: A site may grant the spool group read on the whole journal, and the deployer proves the grant landed

**Status:** accepted, 2026-09-28
**Evidence:** held privately by Fluid Numerics, keyed ADR-0026 — see `docs/evidence.md`

## Context

Layer 1 has two sinks. The audit file under the spool holds only what root
writes there. Everything the shim itself reports, including a `refused`
walk, an `escape_hatch` override and an `uncovered_mount`, goes to journald
through `logger -t walk-blocker`. ADR-0025 made the spool readable by one
site-named group, the people who decide `--kill`. That group still could
not read the journal.

journald's default `SplitMode=uid` files a message under the uid of the
process that logged it. A refusal lands in the refused user's own journal
file. That file is readable by its owner, by root, by the groups systemd's
shipped tmpfiles rules grant (`adm`, and `wheel` where it exists), and by
whatever ACL entries someone set on the node by hand. The spool group is
none of those. To that group, a journal holding refusals reads the same as
an empty one: `journalctl` prints nothing and exits 0. Measured at a
reference deployment: the reader who decides `--kill` saw none of a
morning's refusals, and a root pull of the same window found several.

Journal access is controlled by ACLs, not by group membership. A reader that
asked `id` whether it was in `adm` would miss a grant made any other way.

The alternatives:

**Copy a filtered summary of the `walk-blocker` records into the spool, from
the reaper.** Its strongest form: the group then reads only walk-blocker's
records, which is exactly the data scope ADR-0025 chose. Rejected for now,
not refuted. It adds a journal read to Layer 2's poll, and a journal that
stalls must not stall the backstop. It also needs a third writer into the
spool and its own trust story. The whole-journal grant is available today,
and a site that does not want it leaves it off.

**A sudoers rule for exactly `journalctl -t walk-blocker`.** Its strongest
form is that the scope is exact and the tree ships no ACLs. Rejected: it is
node configuration this tree would neither write nor check, and it hands an
escalation path to a command whose output format is not under this tree's
control.

**Add the readers to `adm`.** Its strongest form is that it is one command
and the site's own mechanism. Rejected: `adm` reads far more than the
journal on most distributions, and membership is managed wherever the site
manages accounts, invisible to this deploy.

## Decision

`[install].journal_readable` (default off) grants `[install].spool_group`
read on the systemd journal. It covers both of journald's storage roots and,
under each, this machine's directory (`%m`, from `/etc/machine-id`): every
journal file there, archived and per-user alike. Other directories under a
root, such as journal-remote's `remote/` or one an image was cloned with, are
neither granted nor where this node's journald writes.

The grant is a tmpfiles.d drop-in under `[install].tmpfiles_dir`, written by
`deploy.py` and applied with `systemd-tmpfiles --create`. systemd then
re-applies it at every boot. It names the group by numeric gid, resolved at
deploy. Directories get read and execute, plus a default entry so new files
inherit the grant. Existing files get read only.

`deploy.py` does not trust tmpfiles' exit status: tmpfiles skips a line it
cannot parse and still exits 0. After applying the drop-in it reads the ACL
of each root, of this machine's directory under it, and of every journal file
in that directory, and checks the gid can read each one. A file journald
rotates away mid-check is skipped, not an error. If any cannot be read, or
this machine has no journal directory at all, the deploy exits `10`. The
timer is already armed by then, so Layer 2 is installed either way: exit `10`
is a report after the install, not a refusal, and the dry run names the causes
it can know in advance (no machine id, no journal directory for this machine).

Turning the grant off, and `--uninstall`, remove the drop-in and revoke the
gids it records. The gids come from the drop-in, not from today's spool
group, so a site that changes both the group and the flag leaves no old
grant behind. The build refuses the grant for a group systemd already grants
the journal to, because revoking the drop-in would strip systemd's own entry.

## Consequences

- **The group reads everything journald holds**: every service's logs,
  authentication records, and every user's own journal. That is the price of
  an ACL, which cannot filter by identifier. The schema description, the
  deploy's dry run and its closing message all say "the whole journal" so
  nobody turns this on believing it is scoped to walk-blocker.
- **Membership decides the exposure, and the deploy does not check it.** The
  group resolves through NSS, often from a directory service, so anyone
  added there later reads the journal without a deploy running. This is the
  same property ADR-0025 accepted for the spool, applied to much more data.
- **Hand-set ACL entries on the journal are left alone.** The revoke removes
  only gids the drop-in names. Other named entries, including any a site
  set by hand, are neither checked nor removed.
- **Files get `r--`, never `r-x`.** tmpfiles rejects `X`. A recursive rule
  applying `r-x` to files would make execute effective, through the mask
  tmpfiles computes for a file that has none. The drop-in uses a glob for
  files, and a test runs the real tmpfiles to prove the mask stays `r--`.
- **A reader proves access by reading, not by group membership.** Anything
  that decides whether Layer 1's journal is visible must test the files, not
  `id`. A grant made through this record's ACL passes a file test, and a
  group test would miss it.
- **Exit `10` is new.** It means installed and reporting, but the group
  cannot read the journal. It says which paths failed the check.
- **This record narrows nothing, and has no Narrows line on purpose.**
  ADR-0025's "The journal is unchanged: an ordinary user still reads their
  own Layer 1 records there" still holds: ADR-0025 did not touch the journal,
  and this record adds the spool group as a reader where a site opts in and
  removes no reader.

## Re-measure when

The spool group's membership changes, or grows past the people who decide
`--kill`: read `getent group <group>` and decide again whether that set may
read the whole journal. Also when journald's `Storage=` or `SplitMode=`
changes (`systemd-analyze cat-config systemd/journald.conf`), and after any
change to the node's own journal ACLs (`getfacl` on the journal directories,
counting named entries). A deploy with the grant on re-proves the grant
itself.

Also when the node's journal retention changes (`SystemMaxFiles=`,
`SystemMaxUse=`, `MaxRetentionSec=`). The grant is to whatever the journal
holds, so a longer retention widens it: the spool group reads more of every
user's and every service's history, with nothing in this tree changed.
Weigh that before raising retention to keep Layer 1's records longer.

## Site config touched

- `[install].journal_readable`
- `[install].tmpfiles_dir`
- `[install].spool_group`
