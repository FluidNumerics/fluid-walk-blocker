#!/usr/bin/env python3
"""walk-blocker survey: tier three of the mount policy (ADR-0016).

Run by an administrator on the node, out of band, to measure what the shim
cannot: capacity and inode count per mount. It reads the mount table, applies
the same remoteness test the shim compiles (type list, `host:` source,
`_netdev`/`addr=` option), takes a timeout-bounded `statvfs` per mount in a
child process so that one wedged filesystem cannot hang the survey, and
proposes a `[[filesystems.mounts]]` block for review. It never writes a file.

Stdlib-only Python 3.9 (ADR-0015): this file is shipped in the payload and
runs on an interpreter this tree does not choose.
"""
import argparse
import datetime
import fnmatch
import json
import re
import subprocess
import sys

# Kept in step with the schema default for [filesystems].remote_fstypes; the
# tool's `--site` mode passes the site's own list instead.
DEFAULT_REMOTE_FSTYPES = [
    "lustre", "wekafs", "beegfs", "gpfs", "ceph", "nfs4", "nfs", "cifs",
    "smb3", "glusterfs", "panfs", "fuse.*", "9p", "sshfs", "s3fs", "daos",
]

# Kernel-provided or container plumbing; nobody walks these, and several
# report meaningless sizes. `--all` includes them.
PSEUDO_FSTYPES = frozenset([
    "proc", "sysfs", "cgroup", "cgroup2", "devtmpfs", "tmpfs", "devpts",
    "securityfs", "pstore", "bpf", "tracefs", "debugfs", "configfs", "fusectl",
    "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "rpc_pipefs", "nsfs",
    "overlay", "squashfs", "efivarfs", "selinuxfs", "ramfs",
])

REMOTE_SOURCE = re.compile(r"^[^/]+:")

# What the child runs. It is a separate process on purpose: `statvfs` on a
# wedged network mount blocks in the kernel and cannot be interrupted from
# inside the interpreter, only abandoned from outside.
STATVFS_SNIPPET = (
    "import json, os, sys\n"
    "s = os.statvfs(sys.argv[1])\n"
    "print(json.dumps({'capacity_bytes': s.f_blocks * s.f_frsize,"
    " 'free_bytes': s.f_bavail * s.f_frsize,"
    " 'inodes': s.f_files, 'inodes_free': s.f_favail}))\n"
)


def default_statvfs_command():
    return [sys.executable or "python3", "-c", STATVFS_SNIPPET]


def _unescape(field):
    """/proc/mounts writes space, tab, newline and backslash as octal."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mount_table(text):
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        rows.append({
            "source": _unescape(parts[0]),
            "mountpoint": _unescape(parts[1]),
            "fstype": parts[2],
            "options": parts[3],
        })
    return rows


def remote_reason(entry, remote_fstypes=None, remote_proxy=True):
    """`type`, `source`, `option`, or None -- the tier-one remoteness test,
    in the order the shim applies it."""
    fstypes = DEFAULT_REMOTE_FSTYPES if remote_fstypes is None else remote_fstypes
    for pattern in fstypes:
        if fnmatch.fnmatchcase(entry["fstype"], pattern):
            return "type"
    if not remote_proxy:
        return None
    if REMOTE_SOURCE.match(entry["source"]):
        return "source"
    for opt in entry["options"].split(","):
        if opt == "_netdev" or opt.startswith("addr="):
            return "option"
    return None


def measure(mountpoint, timeout, statvfs_command=None):
    """statvfs in a child, bounded by `timeout`. Never raises."""
    command = list(default_statvfs_command() if statvfs_command is None
                   else statvfs_command) + [mountpoint]
    result = {"measured": False, "capacity_bytes": None, "free_bytes": None,
              "inodes": None, "inodes_free": None, "error": None}
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    except OSError as exc:
        result["error"] = "cannot start child: %s" % exc
        return result
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Not `subprocess.run`: its timeout path kills and then *waits*, and
        # a child blocked in a filesystem syscall does not die on SIGKILL
        # until the syscall returns. Kill, give it a moment, and move on
        # either way -- the point of the survey is not to hang.
        proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        result["error"] = "timeout after %gs" % timeout
        return result
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()
        result["error"] = tail[-1] if tail else "exit %d" % proc.returncode
        return result
    try:
        stats = json.loads(out.decode("utf-8", "replace"))
        for key in ("capacity_bytes", "free_bytes", "inodes", "inodes_free"):
            result[key] = int(stats[key])
    except (ValueError, KeyError, TypeError) as exc:
        result["error"] = "unreadable child output: %s" % exc
        return result
    result["measured"] = True
    return result


def survey(mount_table="/proc/mounts", timeout=2.0, include_all=False,
           statvfs_command=None, remote_fstypes=None, remote_proxy=True):
    """One dict per mount: the table fields, the tier-one verdict and the
    measurement. `statvfs_command` is a test seam and nothing else."""
    with open(mount_table, encoding="utf-8", errors="replace") as fh:
        entries = parse_mount_table(fh.read())
    rows = []
    for entry in entries:
        if not include_all and entry["fstype"] in PSEUDO_FSTYPES:
            continue
        reason = remote_reason(entry, remote_fstypes, remote_proxy)
        row = dict(entry)
        row["remote"] = reason is not None
        row["remote_reason"] = reason
        row["default_class"] = "expensive" if reason else "cheap"
        row.update(measure(entry["mountpoint"], timeout, statvfs_command))
        rows.append(row)
    return rows


def human_bytes(n):
    if n is None:
        return "-"
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return "%.1f %s" % (value, unit) if unit != "B" else "%d B" % n
        value /= 1024.0
    return str(n)


def human_count(n):
    if n is None:
        return "-"
    for unit, size in (("G", 10 ** 9), ("M", 10 ** 6), ("k", 10 ** 3)):
        if n >= size:
            return "%.1f%s" % (n / size, unit)
    return str(n)


def render_table(rows):
    headers = ["mountpoint", "fstype", "remote", "default_class",
               "capacity", "inodes", "measured"]
    with_override = any("override" in r for r in rows)
    if with_override:
        headers.append("override")
    table = [headers]
    for r in rows:
        line = [
            r["mountpoint"], r["fstype"],
            ("yes (%s)" % r["remote_reason"]) if r["remote"] else "no",
            r["default_class"],
            human_bytes(r["capacity_bytes"]), human_count(r["inodes"]),
            "yes" if r["measured"] else "unmeasured: %s" % r["error"],
        ]
        if with_override:
            ov = r.get("override")
            if ov is None:
                line.append("-")
            elif r.get("override_maxdepth") is not None:
                line.append("%s maxdepth=%d" % (ov, r["override_maxdepth"]))
            else:
                line.append(ov)
        table.append(line)
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
                     for row in table)


def render_toml(rows, today=None):
    """The proposed override block: one entry per remote mount, on its
    default. Deleting an entry keeps the default; changing it is a decision
    that belongs in a diff with a reason (ADR-0016).

    A measured mount's figures are proposed as `inodes`, `capacity_bytes`
    and `surveyed`, so the record flows survey -> diff -> the users' page
    (`docs/what-to-run-instead.md`) without anyone retyping a number.
    `today` is the survey date; a test passes one, the node uses the clock.
    """
    if today is None:
        today = datetime.date.today().isoformat()
    out = ["# Proposed by walk-blocker survey. Review, then paste into site.toml.",
           "# A mount left on its default should be left there on purpose."]
    for r in rows:
        if not r["remote"] or r.get("override") is not None:
            continue  # cheap by default, or a site override already decides it
        facts = "%s, remote by %s" % (r["fstype"], r["remote_reason"])
        if r["measured"]:
            facts += ", capacity %s, inodes %s" % (
                human_bytes(r["capacity_bytes"]), human_count(r["inodes"]))
        else:
            facts += ", unmeasured (%s)" % r["error"]
        out.append("")
        out.append("# %s: %s" % (r["mountpoint"], facts))
        out.append("[[filesystems.mounts]]")
        out.append("path = %s" % json.dumps(r["mountpoint"]))
        out.append('class = "expensive"  # default; delete this entry to keep the '
                   'default, or set class = "cheap" with a reason')
        if r["measured"]:
            out.append("inodes = %d" % r["inodes"])
            out.append("capacity_bytes = %d" % r["capacity_bytes"])
            out.append('surveyed = "%s"' % today)
    return "\n".join(out) + "\n"


# The maze on the README, printed by --help so the payload on a node
# introduces itself the way the repository does.
BANNER = """\
  ■═╦═════════╦═════╦═════╦═══╦═════════════╦═══════╗
  ◆·║  ·······║  ···║     ║   ║        ·····║       ║
  ║·║ ║·╔════·║ ║·║·║ ║ ══╝ ║ ║ ╔═╦═══╗·╔═╗·╚═══╗ ║ ║
  ║·║ ║·║·····║ ║·║·║ ║     ║   ║ ║···║·║ ║·····║ ║ ║
  ║·╚═╣·║·══╦═╝ ║·║·║ ╚═════╩═══╝ ║·║·║·║ ╚════·║ ║ ║
  ║···║·║···║   ║·║·║             ║·║···║·······║ ║ ║
  ╠══·║·╚═╗·╚═╦═╝·║·╠═════════════╣·╠═══╣·══╦═══╩═╝ ║
  ║···║···║···║···║·║FluidNumerics║·║   ║···║       ║
  ║·══╣ ║·╚═╗·║·╔═╝·║             ║·║ ══╬══·║ ║ ╔══ ║
  ║···║ ║···║···║···║ 𝐰𝐚𝐥𝐤𝐛𝐥𝐨𝐜𝐤𝐞𝐫 ║·║   ║···║ ║ ║   ║
  ╠══·╠═╩══·╠═══╣·══╣             ║·║ ║ ║·══╣ ║ ╚═╗ ║
  ║···║·····║   ║···║ stops slow  ║·║ ║ ║···║ ║   ║ ║
  ║·══╣·════╣ ══╩══·║ filesystem  ║·╚═╣ ╚═╗·╠═╩══ ║ ║
  ║···║·····║·······║ traversals  ║···║   ║·║     ║ ║
  ╠═╗·╚════·║·══╦═══╩═══╦═════════╝ ║·║ ║ ║·║ ════╩═╣
  ║ ║·······║···║·······║           ║·║ ║···║       ║
  ║ ╚═══╦═══╬══·║·╔═══╗·╚═══╦═══════╣·╚═╣·╔═╝ ════╗ ║
  ║     ║   ║···║·║   ║·····║·······║···║·║       ║ ║
  ║ ║ ══╝ ║ ║·══╝·║ ══╩════·║·╔════·╚══·║·╚═══════╝ ║
  ║ ║     ║  ·····║        ···║    ·····║···········◆
  ╚═╩═════╩═══════╩═══════════╩═════════╩═══════════■
"""


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=BANNER + "\n\nMeasure the mounts on this node and propose a "
                    "[[filesystems.mounts]] block. Writes nothing.")
    parser.add_argument("--mounts", default="/proc/mounts", metavar="FILE")
    parser.add_argument("--timeout", type=float, default=2.0, metavar="SECONDS",
                        help="per-mount statvfs budget (default 2.0)")
    parser.add_argument("--all", action="store_true",
                        help="include pseudo filesystems")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    args = parser.parse_args(argv)
    rows = survey(args.mounts, timeout=args.timeout, include_all=args.all)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    print(render_table(rows))
    print()
    sys.stdout.write(render_toml(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
