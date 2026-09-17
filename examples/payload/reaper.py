#!/usr/bin/env python3
"""Layer 2: find the unbounded filesystem walks that reached this node anyway.

Stdlib-only Python 3.9 (ADR-0015): a login node runs an interpreter this tree
does not choose, so there is no PEP 723 header here and nothing is imported
that is not in the standard library. `search_rules.py` is deployed beside this
file for the same reason, and every site value below is a literal compiled in
by `walk-blocker build` (ADR-0013) -- this file opens no configuration.

Scan every poll; let PSI corroborate.
------------------------------------
Each poll reads `io.pressure`, `cpu.pressure` and `cpu.stat` for every
per-user slice -- one cheap read each, no privilege, all world-readable -- and
DIFFERENCES them against the previous poll. PSI totals are cumulative since
boot; reading totals rather than differences means every finding is a finding
forever (ADR-0002). It then walks `/proc` in full and classifies every
process, recording on each finding the differenced pressure for its user and
whether that slice crossed the threshold.

This once said "signal first, scan second", and only stalling slices got the
walk. A metadata walk of a parallel filesystem accrues almost no PSI I/O --
measured at a reference deployment, orders of magnitude under any plausible
threshold -- so the gate hid the largest finding on the node while the walk it
was meant to avoid was already being paid on every poll. PSI now corroborates
a finding and never gates one (ADR-0009).

Why I/O pressure and not CPU: the originating incident ran for days at a small
fraction of one core and held essentially no memory. It was never a CPU hog
and never a memory hog. It was a metadata-I/O hog, and that axis is absent
from the mature tools aimed at this problem (ADR-0002).

A stalling slice is evidence about a USER, not a PROCESS. This tool never acts
on the cgroup signal alone: the `/proc` step that names a specific offending
process is what makes an action defensible to the person whose work is being
killed.

Each finding also records HOW the process reached the node, read from its leaf
cgroup and mapped through the site's `[reaper].origins` table: one label per
ssh transport, container runtime or unit shape the site runs, `other` for a
shape the table has not seen, `unknown` when /proc raced the scan. That is
descriptive only -- never an input to classify() -- and it is what lets the
audit log tell a traversal Layer 1 was in front of from one it never applied
to (ADR-0003).

Default is --report. Promoting to --kill is a decision someone makes after
reading real findings against real traffic, not a default that drifts.
"""

import argparse
import errno
import json
import os
import re
import signal
import sys
import time
# The NAME, not the module: read_proc() already has a local `stat` holding the
# contents of /proc/<pid>/stat, and `import stat` shadows into an AttributeError
# there rather than a NameError somewhere obvious.
from stat import S_ISCHR

# The payload is flat: search_rules.py sits beside this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import search_rules as R  # noqa: E402

# --------------------------------------------------------------------------
# Compiled site values (ADR-0013). Every line marked GENERATED is rewritten
# by `walk-blocker build`; the values here are in-tree placeholders (the
# schema defaults, where one exists) so the module imports on its own.
# --------------------------------------------------------------------------

__version__ = '0.1.0'  # GENERATED from VERSION
# Same number as deploy.py and the shim: one payload, one version. Stamped,
# not read from a file. append_audit() adds it to every record it writes,
# including the blind-state ones no Finding builds, which is what makes a row
# self-describing about the build that wrote it.

# Where the per-user slices live, and how a slice directory is spelled: the
# uid sits between prefix and suffix. PSI is read at the SLICE, never at a
# scope beneath it (ADR-0003).
CGROUP_USER_SLICE = '/sys/fs/cgroup/user.slice'  # GENERATED from site.toml:reaper.cgroup_root
SLICE_PREFIX = 'user-'  # GENERATED from site.toml:reaper.slice_prefix
SLICE_SUFFIX = '.slice'  # GENERATED from site.toml:reaper.slice_suffix

# Ordered `{pattern, label}` rows: a process's leaf cgroup name is matched
# against each pattern in turn and the first match supplies its origin label.
# Compiled once, below. Which leaves a site's transports and runtimes produce
# is a fact about the site, so the table is config (ADR-0003).
ORIGINS = ({'pattern': '^session-\\d+\\.scope$', 'label': 'ssh_seated'}, {'pattern': '^session-c\\d+\\.scope$', 'label': 'ssh_seatless'}, {'pattern': '^docker-[0-9a-f]+\\.scope$', 'label': 'container'}, {'pattern': '^[^/]+\\.service$', 'label': 'systemd_service'}, {'pattern': '^init\\.scope$', 'label': 'init'})  # GENERATED from site.toml:reaper.origins

# Classification thresholds. Age and fan-out are process-table facts; the two
# stall fractions select which findings carry `stalling_slice: true` and
# never suppress a record (ADR-0009).
TRAVERSAL_BUDGET_S = 900  # GENERATED from site.toml:reaper.traversal_budget_s
FANOUT_N = 4  # GENERATED from site.toml:reaper.fanout_n
IO_STALL_FRACTION = 0.01  # GENERATED from site.toml:reaper.io_stall_fraction
CPU_STALL_FRACTION = 0.5  # GENERATED from site.toml:reaper.cpu_stall_fraction

# Action budget under --kill: TERM, wait this long, KILL; at most this many
# processes signalled per poll; and how long to wait for a second PSI sample
# when there is no previous poll to difference against.
KILL_GRACE_S = 10  # GENERATED from site.toml:reaper.kill_grace_s
MAX_KILLS = 10  # GENERATED from site.toml:reaper.max_kills
SETTLE_S = 10.0  # GENERATED from site.toml:reaper.settle_s

# One rotation, not a generation count: the audit file is bounded by this
# threshold either way, and a second knob for "how many backups" is not worth
# having for a file logrotate never touches. The dedup below means growth
# tracks distinct findings rather than polls times population; the real rate
# at a site is one of the things reading its trail establishes.
AUDIT_MAX_BYTES = 10485760  # GENERATED from site.toml:reaper.audit_max_bytes

# Tools that cannot walk a tree under ANY argv, consulted by the
# opaque_traversal arm so that waiting is not mistaken for walking
# (ADR-0010). Membership is evidence-backed, one name at a time, at the site.
STREAM_FILTERS = ('tail',)  # GENERATED from site.toml:reaper.stream_filters

# The mount policy (ADR-0016), compiled field by field and assembled into one
# Policy below. The shim carries the same values, generated from the same
# site.toml, and a test asserts the two consumers agree on every row.
REMOTE_FSTYPES = ('lustre', 'wekafs', 'beegfs', 'gpfs', 'ceph', 'nfs4', 'nfs', 'cifs', 'smb3', 'glusterfs', 'panfs', 'fuse.*', '9p', 'sshfs', 's3fs', 'daos')  # GENERATED from site.toml:filesystems.remote_fstypes
REMOTE_PROXY = True  # GENERATED from site.toml:filesystems.remote_proxy
MAXDEPTH_ALLOWED = 2  # GENERATED from site.toml:filesystems.maxdepth_allowed
UNSCOPED_DEPTH = 2  # GENERATED from site.toml:filesystems.unscoped_depth
DEPTH_ALLOWANCE_MAX = 8  # GENERATED from site.toml:filesystems.depth_allowance_max
# `[[filesystems.mounts]]` as the build emits it: a tuple of `{path, class,
# maxdepth?}` rows, converted to Policy's `{path: (class, maxdepth)}` below.
MOUNT_OVERRIDES = ({'path': '/home', 'class': 'expensive', 'maxdepth': 4}, {'path': '/opt/site-tools', 'class': 'cheap'})  # GENERATED from site.toml:filesystems.mounts
# The mount table the reaper reads. Changed only for a test fixture.
MOUNT_TABLE = '/proc/mounts'  # GENERATED from site.toml:filesystems.mount_table

POLICY_DEFAULTS = R.Policy(
    remote_fstypes=REMOTE_FSTYPES,
    remote_proxy=REMOTE_PROXY,
    maxdepth_allowed=MAXDEPTH_ALLOWED,
    unscoped_depth=UNSCOPED_DEPTH,
    depth_allowance_max=DEPTH_ALLOWANCE_MAX,
    mounts={row["path"]: (row["class"], row.get("maxdepth"))
            for row in MOUNT_OVERRIDES})
# The two audited runtime test seams applied, as the shim applies them; this
# is the only environment the policy reads (ADR-0013).
POLICY = R.Policy.from_env(POLICY_DEFAULTS)

CLK_TCK = os.sysconf("SC_CLK_TCK")

# Verdicts that this tool will never kill, whatever the flags say.
#
# `unparsed_traversal` is here for a reason worth stating: it means this
# reaper could not parse the process's argv. Killing on that basis would be
# acting on our own failure rather than on evidence about the process --
# precisely the inversion CLAUDE.md forbids when it says a stalling slice is
# evidence about a user, not a process, and that the /proc step naming a
# specific offender is what makes an action defensible.
NEVER_KILL = frozenset((
    "orphan_idle", "opaque_traversal", "unparsed_traversal"))

# What the process exit status means, and therefore what `systemctl --failed`
# is tracking. Four codes, because meanings that once shared one were split:
#
#   0  nothing new, or nothing new that anyone could act on
#   1  a new ACTIONABLE finding -- a verdict outside NEVER_KILL
#   2  BLIND: no user slice, or /proc unreadable. The tool cannot see.
#   3  a real signal was sent this poll under --kill and did not land:
#      `signalled_but_wedged` or `kill_error`. Every poll it recurs.
#
# Measured at a reference deployment, unactionable findings outnumbered
# actionable ones by a wide margin, and every one of them failed the unit,
# because a new finding of any kind exited 1 -- so `systemctl --failed` was
# driven almost entirely by verdicts that are in NEVER_KILL and can never be
# acted on. An alert nobody can act on trains people to stop reading the
# alert (ADR-0009).
#
# A ratio and not a count, deliberately: a standing process is re-logged
# every poll, so rows run several times findings. Re-derive by counting
# distinct (verdict, pid, starttime) in the trail -- `starttime` is on every
# record for exactly this reason.
#
# NEVER_KILL is the partition used rather than a new severity axis, because
# it already exists, it already means "cannot be acted on", and that is the
# question `systemctl --failed` asks. Nothing about REPORTING changes: every
# finding still reaches the audit trail, the table and the NEW marker. This
# is the alerting channel only -- the distinction ADR-0009 draws between
# suppressing a record and ranking one.
#
# 2 was carved out of 1 rather than the reverse so that a node whose cgroup
# layout changed under the tool stops being indistinguishable, in
# `systemctl --failed`, from someone running a long grep. Both still fail the
# unit -- a non-zero exit fails the Type=oneshot unit by default, and nothing
# lists 1 or 2 as success -- but they are no longer the same event.
#
# 3 is the one code that is NOT latched. The others fail the unit for a NEW
# fact and go quiet while the fact stands, because a months-old finding
# failing the unit forever is an alert nobody reads. A kill that did not
# land is not a standing fact: every poll sends the process a fresh TERM
# and KILL, every one is a real signal, and every one fails to end it. The
# audit trail already refuses to dedup those rows ("never claim a kill that
# did not land"); the exit code has to agree, or the unit reads green in
# `systemctl status` while the trail fills with signals that changed
# nothing -- the shape the predecessor's trail showed under --kill. This
# only ever fires under --kill, which a site promotes to by decision; a
# --report unit cannot exit 3.
EXIT_QUIET = 0
EXIT_ACTIONABLE = 1
EXIT_BLIND = 2
EXIT_KILL_FAILED = 3

# The terminate() outcomes that mean a real signal was sent and the process
# is still there, or that whether it landed cannot be known. One set, so the
# exit code and the audit trail's no-dedup rule cannot name different lists.
KILL_DID_NOT_LAND = frozenset(("signalled_but_wedged", "kill_error"))


# --------------------------------------------------------------------------
# PSI
# --------------------------------------------------------------------------

def read_psi(path):
    """{'some': {...}, 'full': {...}} from a pressure file, totals in usec."""
    out = {}
    try:
        with open(path, "r") as fh:
            for line in fh:
                parts = line.split()
                if not parts:
                    continue
                fields = {}
                for item in parts[1:]:
                    key, _, value = item.partition("=")
                    try:
                        fields[key] = float(value)
                    except ValueError:
                        continue
                out[parts[0]] = fields
    except OSError:
        return {}
    return out


def read_cpu_usage_usec(path):
    try:
        with open(path, "r") as fh:
            for line in fh:
                if line.startswith("usage_usec"):
                    return float(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return None


def sample_slices(cgroup_root=CGROUP_USER_SLICE):
    """One poll: every user slice's cumulative pressure and CPU totals."""
    sample = {"ts": time.time(), "slices": {}}
    try:
        names = os.listdir(cgroup_root)
    except OSError:
        return sample
    for name in names:
        if not (name.startswith(SLICE_PREFIX) and name.endswith(SLICE_SUFFIX)):
            continue
        uid_text = name[len(SLICE_PREFIX):len(name) - len(SLICE_SUFFIX)]
        try:
            uid = int(uid_text)
        except ValueError:
            continue
        base = os.path.join(cgroup_root, name)
        io_psi = read_psi(os.path.join(base, "io.pressure"))
        cpu_psi = read_psi(os.path.join(base, "cpu.pressure"))
        if not io_psi:
            continue
        sample["slices"][str(uid)] = {
            "io_full_total": io_psi.get("full", {}).get("total", 0.0),
            "io_some_total": io_psi.get("some", {}).get("total", 0.0),
            "cpu_some_total": cpu_psi.get("some", {}).get("total", 0.0),
            "cpu_usage_usec": read_cpu_usage_usec(os.path.join(base, "cpu.stat")),
        }
    return sample


def stalling_slices(previous, current):
    """[(uid, io_fraction, cpu_fraction, cpu_seconds)] over the differenced interval.

    Returns every slice, ranked; the caller decides which cross a threshold.
    Fractions are of wall time in the interval, which is what PSI measures.
    """
    if not previous:
        return []
    elapsed = current["ts"] - previous["ts"]
    if elapsed <= 0:
        return []
    window_usec = elapsed * 1e6

    ranked = []
    for uid, now in current["slices"].items():
        before = previous["slices"].get(uid)
        if before is None:
            # First sighting. A slice's cumulative total is not a finding --
            # it is history, most of it from before this process started.
            continue
        io_delta = now["io_full_total"] - before["io_full_total"]
        cpu_delta = now["cpu_some_total"] - before["cpu_some_total"]
        if io_delta < 0 or cpu_delta < 0:
            # Counters went backwards: the node rebooted, or the slice was
            # recreated between polls. Neither is a finding.
            continue
        cpu_secs = None
        if now["cpu_usage_usec"] is not None and before["cpu_usage_usec"] is not None:
            cpu_secs = (now["cpu_usage_usec"] - before["cpu_usage_usec"]) / 1e6
        ranked.append((
            int(uid),
            io_delta / window_usec,
            cpu_delta / window_usec,
            cpu_secs,
        ))
    ranked.sort(key=lambda row: row[1], reverse=True)
    return ranked


# --------------------------------------------------------------------------
# /proc
# --------------------------------------------------------------------------

# The origin table compiled once at import: (regex, label), in site order.
# logind names a seated session `session-<N>.scope` and a seatless one
# `session-c<N>.scope`; which transports, container runtimes and scheduler
# adapters produce which other leaves is the site's to say (ADR-0003).
_ORIGIN_RULES = tuple((re.compile(row["pattern"]), row["label"])
                      for row in ORIGINS)


def classify_origin(leaf):
    """How a process got onto the node, from its leaf cgroup name.

    Coarse on purpose. The full cgroup path carries another user's session id,
    and the audit log has to stay a document that can be shown to the person
    whose process is in it.

    First matching `[reaper].origins` row wins; no match is `other`, which is
    the honest answer for a shape the table has not seen rather than a guess
    that reads as certainty; an unreadable cgroup file is `unknown`.

    Separating a daemon's unit from a person's session is ANNOTATION, not a
    filter. Every record that would exist without the label still exists; a
    reader can drop daemons with one expression instead of by recognising
    names. Excluding them outright would rest on who launched a process rather
    than on whether it can walk a tree, and suppressing trail noise before the
    trail has been read is re-introducing a gate on a guess (ADR-0009).

    This is descriptive only. It is never an input to classify(): a process is
    not more or less of a runaway because of how its owner logged in.
    """
    if not leaf:
        return "unknown"
    for pattern, label in _ORIGIN_RULES:
        if pattern.match(leaf):
            return label
    return "other"


class Proc(object):
    __slots__ = ("pid", "ppid", "uid", "state", "comm", "argv", "cwd",
                 "cpu_s", "age_s", "starttime", "leaf_cgroup", "stdin_tty")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))

    @property
    def key(self):
        """Stable identity across polls. PID alone is reused; PID plus the
        kernel's starttime is not."""
        return "%d:%s" % (self.pid, self.starttime)


def _uptime(proc_root="/proc"):
    try:
        with open(os.path.join(proc_root, "uptime"), "r") as fh:
            return float(fh.read().split()[0])
    except (OSError, IndexError, ValueError):
        return None


def read_proc(pid, uptime, proc_root="/proc"):
    base = os.path.join(proc_root, str(pid))
    try:
        with open(os.path.join(base, "stat"), "r") as fh:
            stat = fh.read()
    except OSError:
        return None
    # comm is parenthesized and may itself contain spaces and parentheses, so
    # the split has to be anchored on the LAST ')' rather than tokenized.
    close = stat.rfind(")")
    open_paren = stat.find("(")
    if close < 0 or open_paren < 0:
        return None
    comm = stat[open_paren + 1:close]
    fields = stat[close + 2:].split()
    if len(fields) < 20:
        return None
    try:
        state = fields[0]
        ppid = int(fields[1])
        utime = int(fields[11])
        stime = int(fields[12])
        starttime = int(fields[19])
    except (IndexError, ValueError):
        return None

    try:
        with open(os.path.join(base, "cmdline"), "rb") as fh:
            raw = fh.read()
        # Drop ONE trailing empty, not every empty. cmdline is NUL-TERMINATED,
        # so the split always yields a final b"" that is an artifact of the
        # format -- but an interior empty is a real argv element, and `find
        # "$dir"` with dir unset produces one. Filtering all of them made
        # Layer 2 blind to exactly those commands: strip the empty from
        # `grep -r "" /big` and /big slides into the pattern slot, so the
        # reaper reads the walk as the cwd while Layer 1 refuses it against
        # /big. The table handles empties itself (_drop_empty_operands), so
        # passing them through is what makes the two layers answer the same
        # question.
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        argv = [a.decode("utf-8", "replace") for a in parts]
    except OSError:
        argv = []

    # /proc/<pid>/status carries the real uid explicitly. st_uid of the
    # directory says the same thing on a live /proc, but reading it from
    # status is what lets a synthetic /proc in the tests describe another
    # user's process without needing root to create one.
    uid = None
    try:
        with open(os.path.join(base, "status"), "r") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                    break
    except (OSError, IndexError, ValueError):
        uid = None
    if uid is None:
        try:
            uid = os.stat(base).st_uid
        except OSError:
            return None

    # Another user's /proc/<pid>/cwd is not readable without ptrace access, so
    # a relative root in someone else's argv cannot be resolved. That is
    # recorded honestly rather than guessed at: see classify().
    cwd = None
    try:
        cwd = os.readlink(os.path.join(base, "cwd"))
    except OSError:
        pass

    # THREE states, and None is not False. Some tools decide what they walk by
    # asking whether stdin is a terminal: `ugrep pat` at a prompt recurses the
    # working directory, the same argv on the end of a pipe filters the pipe
    # and walks nothing. Layer 1 answers that with `[ -t 0 ]` for free; here it
    # has to be read off /proc, and it was hardcoded True until a profile
    # existed that cared -- which would have turned every long-lived piped log
    # filter with a cwd on an expensive mount into a traversal finding
    # (ADR-0011).
    #
    # Same OSError discipline as cwd above, and a SHARED permission gate with
    # it: /proc/<pid>/fd and /proc/<pid>/cwd are both PTRACE_MODE_READ, so on
    # another user's process they fail together.
    #
    # That gate is why None is safe to read as "no cwd fallback" when the cause
    # is permission -- the fallback had nothing resolvable to charge anyway.
    # It is NOT the only cause: a process that closed fd 0 has no fd/0 at all
    # (ENOENT, not EPERM) while its cwd reads fine. There the gate argument
    # says nothing and a second one carries it -- a process with no stdin is
    # not at a terminal, so nothing is walked and charging no root is still
    # right. See ADR-0011 and
    # test_a_closed_stdin_is_not_a_terminal_even_with_a_readable_cwd.
    #
    # os.stat, not a readlink match on the TEXT: the link reads /dev/pts/N for
    # an ssh session and /dev/ttyN on the console, and a name test is a list
    # of spellings rather than a fact about the file. A terminal is a character
    # device whose driver is a tty -- major 136-143 for pts, 4 for the virtual
    # consoles and serial lines, 5 for /dev/tty and /dev/console.
    stdin_tty = None
    try:
        st = os.stat(os.path.join(base, "fd", "0"))
        major = os.major(st.st_rdev)
        stdin_tty = bool(S_ISCHR(st.st_mode)
                         and (major in (4, 5) or 136 <= major <= 143))
    except OSError:
        pass

    # cgroup v2 puts the only entry on a line starting "0::". Read for the leaf
    # name alone -- which scope or service holds this process -- so a finding
    # can say whether Layer 1 was ever in its PATH. Same OSError discipline as
    # everything else here: a process that exits mid-scan is a None, not a
    # traceback.
    leaf_cgroup = None
    try:
        with open(os.path.join(base, "cgroup"), "r") as fh:
            for line in fh:
                if line.startswith("0::"):
                    leaf_cgroup = line.strip().rsplit("/", 1)[-1] or None
                    break
    except OSError:
        pass

    age = None
    if uptime is not None:
        age = uptime - (starttime / float(CLK_TCK))

    return Proc(pid=pid, ppid=ppid, uid=uid, state=state, comm=comm, argv=argv,
                cwd=cwd, leaf_cgroup=leaf_cgroup, stdin_tty=stdin_tty,
                cpu_s=(utime + stime) / float(CLK_TCK),
                age_s=age, starttime=starttime)


def scan_procs(proc_root="/proc"):
    # Read from the same root the process entries come from: a synthetic /proc
    # whose ages are computed against the real machine's uptime describes
    # processes that appear to be a fortnight old.
    uptime = _uptime(proc_root)
    procs = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return procs
    for name in names:
        if not name.isdigit():
            continue
        proc = read_proc(int(name), uptime, proc_root)
        if proc is None:
            continue
        procs[proc.pid] = proc
    return procs


def effective_parent(proc, procs):
    """The first ancestor that is not a transparent wrapper.

    `xargs -P16 find` reparents the accounting question: sixteen finds under one
    xargs are one decision, not sixteen. GENERIC_WRAPPERS is the same list the
    session guard uses, ported for exactly this walk. `bash` is deliberately
    not in it: a remote command attributes to its session shell under every
    transport, never to the daemon that spawned the shell (ADR-0003).
    """
    seen = set()
    current = procs.get(proc.ppid)
    while current is not None and current.pid not in seen:
        seen.add(current.pid)
        if current.comm not in R.GENERIC_WRAPPERS:
            return current
        current = procs.get(current.ppid)
    return None


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def traversal_roots(proc, mounts, policy=None):
    """(roots_on_expensive_mount, unresolved, error) for a process's argv.

    unresolved is True when the tool's root is relative and this process's cwd
    could not be read -- which is the normal case for another user's process.

    error carries the reason the argv could not be parsed at all, or None.
    It is a THIRD state on purpose: returning ([], False) for an internal
    failure told the caller "no expensive roots, nothing unresolved" -- a
    clean bill of health indistinguishable from a process that really is
    fine. This is the backstop layer, so it is the last place that should
    answer a question it did not manage to ask.

    `policy` defaults to the compiled site policy; the suite passes its own.
    """
    policy = POLICY if policy is None else policy
    # argv[0], not comm. See R.tool_from_argv(): comm is 15 bytes the process
    # may overwrite, and on a login node the thing overwriting it is usually
    # the WRAPPER -- a coding agent's shell functions set it to the agent's
    # own version string for every search tool they launch. Keyed on comm,
    # PROFILE_BY_NAME.get() returned None for live traversals and this
    # function answered "no expensive roots, nothing unresolved" -- the clean
    # bill of health the docstring above says is the one answer it must never
    # give.
    profile = R.PROFILE_BY_NAME.get(R.tool_from_argv(proc.argv, proc.comm))
    if profile is None or not proc.argv:
        return ([], False, None)
    if not R.is_traversal(profile, proc.argv):
        # `grep -E --line-buffered pattern` reading a pipe walks nothing.
        # Without this check its roots fall back to the cwd, an unreadable cwd
        # falls back to /, and every harmless orphaned pipe reader gets
        # classified as a traversal rooted at the filesystem root.
        return ([], False, None)
    try:
        # resolved_roots, not roots: it applies the --base-directory
        # coordinate change, which this function used to skip. Layer 1
        # refused `fd --base-directory /big pat` from a cheap cwd while this
        # reported no expensive root -- the backstop blind to exactly the walk
        # a user who bypasses the advisory shim would perform, which is the
        # one thing Layer 2 exists for. Sharing the entry point is what stops
        # the two from drifting again.
        #
        # "" for an unreadable cwd, not "/". Passing "/" made every relative
        # operand look resolvable-against-root, so the old code had to reject
        # it separately by testing the RAW value -- and that discarded
        # operands an absolute --base-directory had already made absolute.
        # `fd --base-directory /big pat sub` resolves to /big/sub with no cwd
        # at all, and was reported as nothing. With the sentinel,
        # resolvability is a property of the RESULT: a path that is still
        # relative after the coordinate change is the one that genuinely
        # needed the cwd we could not read.
        #
        # `is True`, so an UNREADABLE stdin (None) reads as "not a terminal"
        # and charges no cwd fallback. Honest rather than merely safe: None
        # here means /proc/<pid>/fd/0 was not readable, and the same ptrace
        # gate makes proc.cwd None too, so the fallback would have had nothing
        # resolvable to charge either way. Assuming a terminal instead -- what
        # this did until a stdin-sensitive profile existed -- would invent a
        # walk of a cwd we cannot read for every piped filter on the node.
        pairs = R.resolved_roots(profile, proc.argv, proc.cwd or "",
                                 stdin_is_tty=proc.stdin_tty is True)
        # Noted here, acted on AFTER the roots are judged below: an operand
        # scan that charged nothing fell back to the default root, which is
        # right for `find -name foo` and wrong for a leading option this table
        # does not model, and an absolute path left over in argv is what tells
        # those apart. It must not SUPPRESS the fallback's hits -- Layer 1
        # judges that same cwd, and the two agreeing is an invariant with a
        # test on it. So it only speaks up when the fallback found nothing,
        # which is exactly the case that would otherwise be silence.
        unclaimed = R.unclaimed_absolute_root(profile, proc.argv)
        # Inside the same try: this is another argv scan, and the third state
        # exists so a parser failure is reported rather than returned as a
        # clean bill of health.
        #
        # bounded() is still deliberately NOT consulted -- see
        # test_layer2_agrees_with_layer1_on_every_matrix_row. A DEPTH bound
        # says a walk is cheap, and a shallow walk of an expensive mount that
        # is past budget right now is still worth naming. A DEVICE bound says
        # something different: that the walk is on another filesystem
        # entirely, which is a claim about where the process IS rather than
        # about what it costs.
        device_bound = R.device_bounded(profile, proc.argv)
    except Exception as exc:
        # Reported, not swallowed. A parser bug here used to delete the
        # process from the audit trail entirely -- see classify().
        return ([], False, "%s: %s" % (type(exc).__name__, exc))

    hits = []
    unresolved = False
    for _raw, path in pairs:
        if not os.path.isabs(path):
            unresolved = True
            continue
        # judge_root, not offending_mount: a device-bounded walk cannot cross
        # onto a mount below its root, so reporting `find / -xdev` as walking
        # /big names a filesystem the process provably never enters. A finding
        # that cannot survive being shown to the person whose work is in it is
        # not one this layer makes.
        hit = R.judge_root(path, mounts, policy, device_bound)
        if hit:
            hits.append((path, hit[0], hit[1], hit[2]))
    if not hits and unclaimed is not None:
        # The default root was cheap AND the scan never charged the absolute
        # path sitting in argv. Reported as a parse failure, because that is
        # what it is: `bfs -S dfs / -name x` from a cheap cwd walks the whole
        # filesystem and this code cannot see it. Silence here is the clean
        # bill of health this function must never give.
        return ([], unresolved,
                "operand scan charged no path; argv names %s" % unclaimed)
    return (hits, unresolved, None)


class Finding(object):
    def __init__(self, proc, verdict, detail):
        self.proc = proc
        self.verdict = verdict
        self.detail = detail
        self.action = "reported"

    def record(self, io_fraction=None, stalling_slice=None):
        proc = self.proc
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "layer": "reaper",
            "verdict": self.verdict,
            "action": self.action,
            "pid": proc.pid,
            # The other half of `_finding_key`. Without it the trail cannot
            # reproduce its own dedup key, so anyone reading it counts ROWS --
            # and a standing process is re-logged every poll. Distinct
            # (verdict, pid, starttime) is the finding count.
            "starttime": proc.starttime,
            "ppid": proc.ppid,
            "uid": proc.uid,
            "comm": proc.comm,
            "state": proc.state,
            "cpu_s": round(proc.cpu_s, 2) if proc.cpu_s is not None else None,
            "age_s": round(proc.age_s, 1) if proc.age_s is not None else None,
            "cmdline": " ".join(proc.argv),
            # How it arrived, not what it did. A traversal from a login shell
            # is one Layer 1 was in front of and did not stop; one from a
            # scheduler step or a container is one Layer 1 never applied to.
            # Without this the audit log cannot tell those apart, which is
            # most of what reading it for a few days is meant to answer.
            "origin": classify_origin(proc.leaf_cgroup),
            "leaf_cgroup": proc.leaf_cgroup,
        }
        entry.update(self.detail)
        if io_fraction is not None:
            entry["io_pressure_delta"] = round(io_fraction, 4)
        if stalling_slice is not None:
            # Whether this process's user slice crossed the PSI threshold this
            # poll -- corroboration, not a precondition. False is a real and
            # common answer, not a missing one: a metadata walk of a parallel
            # filesystem accrues almost no PSI io, so the finding this reaper
            # most exists for arrives with False here.
            #
            # None is the THIRD state and the caller must use it: a process
            # whose uid has no differenced reading at all -- a slice that
            # appeared since the last poll, or a uid with no slice -- is not
            # one PSI disagreed about, it is one PSI never measured. Writing
            # False for both would let the field the --kill decision is read
            # against say "no stall here" about a user nobody sampled, which
            # is the clean bill of health this code must never give.
            entry["stalling_slice"] = bool(stalling_slice)
        return entry


def classify(procs, mounts, budget_s=None, fanout_n=None, policy=None):
    """Findings for one poll, over every process handed in.

    Every process, not a subset: PSI no longer narrows the candidate list
    (ADR-0009), and there is no seam for re-narrowing it -- one whose only
    remaining effect would be to make re-narrowing look supported.
    """
    budget_s = TRAVERSAL_BUDGET_S if budget_s is None else budget_s
    fanout_n = FANOUT_N if fanout_n is None else fanout_n
    all_procs = procs

    findings = []
    traversals = []

    for proc in sorted(procs.values(), key=lambda p: p.pid):
        # From argv, with comm as the fallback -- keying this on comm alone is
        # what made a self-renaming tool invisible to every arm below.
        known_tool = R.tool_from_argv(proc.argv, proc.comm) is not None
        hits, unresolved, root_error = traversal_roots(proc, mounts, policy)
        on_expensive_mount = bool(hits)
        past_budget = proc.age_s is not None and proc.age_s > budget_s
        orphaned = proc.ppid == 1

        detail = {"root": hits[0][0] if hits else None,
                  "mount": hits[0][1] if hits else None,
                  "fs": hits[0][2] if hits else None,
                  "reason": hits[0][3] if hits else None}
        if unresolved:
            # Honest about what could not be determined rather than assuming
            # the cwd was somewhere convenient.
            detail["root_unresolved"] = True
        if root_error:
            detail["root_error"] = root_error

        if known_tool and root_error:
            # A known traversal tool whose argv this reaper could not parse.
            # Emitted rather than skipped, because `hits` is empty on a parse
            # failure -- so without this the process falls past every arm
            # below and leaves NO record at all.
            #
            # This names a defect in THIS code, not in the user's command,
            # which is exactly why it belongs in the trail that is meant to
            # justify promoting to --kill. A trail that cannot tell "clean"
            # from "the parser crashed" is the wrong evidence for that.
            findings.append(Finding(proc, "unparsed_traversal", detail))
            continue

        if known_tool and on_expensive_mount:
            traversals.append(proc)
            if orphaned:
                # Nothing reads its output: the writer's reader is gone. This
                # is the originating incident's exact shape -- the `head` at
                # the other end of the pipe never got its lines, so it never
                # sent SIGPIPE.
                findings.append(Finding(proc, "orphan_traversal", detail))
            elif past_budget:
                findings.append(Finding(proc, "runaway_traversal", detail))
            continue

        if orphaned and known_tool and proc.cpu_s is not None and proc.cpu_s < 1.0:
            # A leak, not a load. Orphaned pipe readers cost almost nothing
            # and will never exit on their own.
            findings.append(Finding(proc, "orphan_idle", detail))
            continue

        if ((not known_tool) and any(proc.argv)
                and not R.is_stream_filter(proc.argv, STREAM_FILTERS)
                and proc.state == "D" and past_budget):
            # python3 in os.walk, rsync, tar. Counted, because otherwise the
            # corpus reports the tool list complete when it has only ever
            # looked for itself.
            #
            # `is_stream_filter` excludes tools that cannot walk a tree under
            # any argv -- `tail -F` is the canonical member. Same kind of
            # exclusion as the kernel-thread one below and on the same
            # grounds: this verdict exists to keep the corpus honest about
            # which TOOLS have not been modelled, and a log-follower blocked
            # on an append is not an unmodelled traversal tool, it is not a
            # traversal tool. Note what this does NOT key on:
            # `on_expensive_mount` is False for every process that reaches
            # this arm, because traversal_roots() returns no hits for an
            # unknown tool, so gating on it would delete the verdict rather
            # than narrow it -- and where the home filesystem IS the expensive
            # one, those followers really are blocked on the filesystem this
            # reaper backstops. Not-a-traversal is the true reason and the
            # only one that survives (ADR-0010).
            #
            # `any(proc.argv)` guards kernel threads, which have an EMPTY
            # /proc/<pid>/cmdline and land here otherwise -- an unknown "tool"
            # in D past budget describes a blocked `kworker` exactly as well as
            # it describes rsync. It was the removed PSI gate that used to hide
            # them, incidentally and not by design: kernel threads are uid 0
            # and there is no user-0 slice to stall. They are excluded on
            # their own merits instead. A record whose cmdline is the empty
            # string names nothing, and naming the specific offending process
            # is the whole reason the /proc step exists; the verdict's own
            # purpose -- keeping the corpus honest about which TOOLS it has
            # not modelled -- gets nothing from a thread that has no command
            # line.
            #
            # `any`, not just a truth test on the list: a zero-byte cmdline
            # reads back as [] and a single NUL as [""], and neither names
            # anything, while ["", "-r", "/big"] still does and is still
            # reported. Zero BYTES is the real test for a kernel thread:
            # procfs files stat as size 0 whatever they contain, so
            # `[ -s /proc/PID/cmdline ]` calls every process one (ADR-0009).
            #
            # This also excludes a case the kernel-thread framing does not
            # name, and excluding it is right for the same reason rather than
            # by accident. read_proc() opens `stat` and `cmdline` separately
            # and falls back to argv=[] when the second raises, so a process
            # that exits between the two reads arrives here indistinguishable
            # from a kernel thread. Nothing is really lost: the record would
            # have named nothing, which is what the guard is for, and a
            # process still alive at the next poll reads its argv fine and is
            # classified then.
            findings.append(Finding(proc, "opaque_traversal", detail))

    # Fan-out is an aggregate verdict: N bounded walks under one parent are one
    # unbounded walk, and the per-invocation rule is arithmetically defeatable
    # by splitting. Counted as ONE traversal for budget purposes.
    groups = {}
    for proc in traversals:
        parent = effective_parent(proc, all_procs)
        key = ("ppid", parent.pid) if parent else ("uid", proc.uid)
        groups.setdefault(key, []).append(proc)

    for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(members) < fanout_n:
            continue
        leader = min(members, key=lambda p: p.pid)
        detail = {
            "group": "%s=%s" % key,
            "members": [p.pid for p in members],
            "count": len(members),
        }
        findings.append(Finding(leader, "fanout_traversal", detail))

    return findings


# --------------------------------------------------------------------------
# Action
# --------------------------------------------------------------------------

def terminate(proc, grace_s=None, sleep=time.sleep, killer=os.kill,
              alive=None):
    """TERM, grace, KILL, then RE-CHECK. Returns the action actually achieved.

    A process blocked in a filesystem syscall does not die on SIGKILL until
    that syscall returns. Reporting `killed` for one that is still sitting in
    D is a claim the tool did not earn, and an audit log that reports success
    it did not achieve is worse than no audit log.
    """
    grace_s = KILL_GRACE_S if grace_s is None else grace_s
    if alive is None:
        def alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except OSError as exc:
                return exc.errno != errno.ESRCH

    try:
        killer(proc.pid, signal.SIGTERM)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return "already_gone"
        return "signal_failed"

    sleep(grace_s)
    if not alive(proc.pid):
        return "terminated"

    try:
        killer(proc.pid, signal.SIGKILL)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return "terminated"
        return "signal_failed"

    sleep(1)
    if alive(proc.pid):
        return "signalled_but_wedged"
    return "killed"


# --------------------------------------------------------------------------
# State, latching, output
# --------------------------------------------------------------------------

def load_state(path):
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    # The same explicit mode append_audit() sets, for the OPPOSITE failure.
    # That one guards against a umask STRICTER than root's 022 narrowing the
    # file; this guards against the umask being ignored entirely. A directory
    # carrying a POSIX DEFAULT ACL -- a named reader grant on the audit
    # directory does this -- suppresses the umask at creation and intersects
    # the create mode with the default entries instead. That is how this file
    # once landed group-writable, after which the installer's own ownership
    # check refused the next install for a group-writable file in the audit
    # directory.
    #
    # Before the WRITE, not merely before the rename. A crash between the two
    # leaves the temp file behind, and a group-writable leftover is the same
    # refusal under a different name.
    #
    # fchmod as well as the create mode, and NOT redundant with it: O_CREAT's
    # mode argument is IGNORED when the file already exists, so a .tmp left by
    # a build older than this fix keeps its old mode straight through O_TRUNC.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(tmp, flags, 0o644), "w") as fh:
        os.fchmod(fh.fileno(), 0o644)
        json.dump(state, fh)
    os.replace(tmp, path)


# `logged_actions` in the state file is keyed by TWO shapes, deliberately,
# and this is the whole list:
#
# * a BLIND SENTINEL -- one of `LATCH_SENTINELS`. It names a fact about the
#   POLL: the tool could see no user slice, or no process at all. There is
#   no pid to name, the value stored is a constant rather than an action,
#   and the two sentinels are separate keys on purpose so a node with no
#   user slices and a node with an unreadable `/proc` cannot dedup against
#   each other -- they are different failures with different causes.
# * a FINDING KEY -- `_finding_key()`, `verdict:pid:starttime`. It names one
#   process in one arm, and the value stored is that finding's action.
#
# Folding them into one shape would mean either giving a blind poll a
# fabricated pid, or giving a finding a name the end-of-poll prune cannot
# take apart: that prune keeps a key whose tail after the first `:` is a
# live `Proc.key`, which is exactly what makes a finding key prunable and a
# sentinel not. What the two DO share is `_latch_key()`, so a third shape
# cannot arrive unannounced.
LATCH_BLIND_SLICES = "blind"
LATCH_BLIND_PROCS = "procs_blind"
LATCH_SENTINELS = (LATCH_BLIND_SLICES, LATCH_BLIND_PROCS)


def _latch_key(key):
    """`key`, having checked it is one of the two known shapes.

    Construction-time, because the symptom of a third shape is not an
    exception: it is a state file whose prune silently keeps or drops the
    wrong entries, which surfaces as audit rows that repeat or go missing
    weeks later. A key this function does not recognise is a bug in this
    file, and the poll that would have written it is not one to trust.
    """
    if key in LATCH_SENTINELS:
        return key
    parts = key.split(":")
    if len(parts) == 3 and all(parts) and parts[1].isdigit():
        return key
    raise AssertionError(
        "latch key %r is neither a blind sentinel nor verdict:pid:starttime"
        % (key,))


def _finding_key(finding):
    """The identity latch()/the audit dedup/the NEW stdout marker all use.

    One function, not the same format string copied at each call site:
    a key that drifted between them would silently break whichever
    consumer used the stale spelling. `verdict:pid:starttime` -- PID alone
    is reused; PID plus the kernel's starttime is not, and the verdict is
    part of the identity because the same process can surface under a
    different verdict across polls.

    The second of the two `logged_actions` key shapes; see `LATCH_SENTINELS`
    above for the first and for why they are not unified.
    """
    return _latch_key("%s:%s" % (finding.verdict, finding.proc.key))


def _latch_blind(logged_actions, audit_path, sentinel, state_word):
    """One blind record for `sentinel`, appended and latched -- unless the
    last poll already did, in which case nothing is written.

    The two blind arms in run() differ only in which sentinel they latch and
    what the record's `state` says; the record's SHAPE is this function's,
    so the two cannot drift apart. Returns whether a record was written, so
    the caller can decide whether that alone is worth a state save.
    """
    if logged_actions.get(sentinel) == sentinel:
        return False
    append_audit(audit_path, [{"ts": time.time(), "layer": "reaper",
                               "action": "blind", "state": state_word}])
    logged_actions[_latch_key(sentinel)] = sentinel
    return True


def latch(state, findings):
    """Which findings are new since the last poll.

    NEW, and only NEW: whether a new finding is also ACTIONABLE -- and so
    whether it fails the unit -- is `run()`'s question, decided against
    NEVER_KILL at the EXIT_* return. Stating the predicate here too would be
    a second copy of it to drift from.

    What this latch is for is the standing case: without it a months-old
    finding fails the unit forever and `systemctl --failed` stops being a
    signal anyone reads.
    """
    seen = set(state.get("latched", []))
    current = set()
    new = []
    for finding in findings:
        key = _finding_key(finding)
        current.add(key)
        if key not in seen:
            new.append(finding)
    state["latched"] = sorted(current)
    return new


def append_audit(path, entries):
    if not path or not entries:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # One rotation when the file crosses the threshold, not a check on every
    # call: once rotated the fresh file this open() creates is tiny again,
    # so the very next call finds nothing to rotate. A missing file -- the
    # ordinary first-ever-write case -- is an OSError here and skips
    # rotation the same way a file under the threshold does.
    try:
        if os.path.getsize(path) >= AUDIT_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    with open(path, "a") as fh:
        for entry in entries:
            # Stamped HERE and nowhere else. A record names the build that
            # WROTE it, and this function is the writer -- so this is the
            # truthful home for the field and the one choke point every call
            # site passes through. Adding it at each builder instead left the
            # two blind-state records (no-user-slices, no-processes) without
            # it, which made "which build wrote the last row?" answer null in
            # exactly the failure mode where the question is asked.
            #
            # dict(entry, ...) rather than entry[...] = : the caller's dict is
            # not ours to mutate, and a test asserting on a built record would
            # otherwise see a field its builder never put there.
            fh.write(json.dumps(dict(entry, version=__version__),
                                sort_keys=True) + "\n")
    # Explicit, not trusted to umask: the trail is readable by everyone on
    # the node by decision (ADR-0012). `open(path, "a")` on a brand-new file
    # is 0666 & ~umask, which under a stricter umask than root's usual 022
    # would silently narrow it.
    os.chmod(path, 0o644)


def format_finding(entry, is_new):
    marker = "NEW " if is_new else "    "
    return "%s%-19s pid=%-8d uid=%-6s %-9s cpu=%-9s age=%-9s %-14s %s\n      %s" % (
        marker,
        entry["verdict"],
        entry["pid"],
        entry["uid"],
        "state=" + str(entry.get("state")),
        entry.get("cpu_s"),
        entry.get("age_s"),
        "via=" + str(entry.get("origin")),
        "-> " + str(entry.get("action")),
        (entry.get("cmdline") or "")[:160],
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

# Where a HAND run keeps its state and audit trail when `--spool` is not
# given. The deployed unit always passes `--spool` (the compiled
# `[install].spool_dir`); this default exists so an operator trying the
# tool gets local disk rather than a home directory that may be on the
# filesystem under investigation.
SPOOL_ENV = "WALK_BLOCKER_SPOOL"
SPOOL_PREFIX = "/var/tmp/walk-blocker-"


def default_spool():
    return os.environ.get(
        SPOOL_ENV, SPOOL_PREFIX + str(os.environ.get("USER") or os.getuid()))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="reaper.py",
        description="Report (and, only when told to, stop) unbounded "
                    "filesystem walks on this login node.")
    # Outside the mode group because asking what is installed is not a mode,
    # not because it survives a broken payload -- it does not: the module-level
    # `import search_rules as R` above must succeed before this parser exists,
    # so `--version` needs search_rules.py beside it like every other
    # invocation. The answer that DOES survive a half-installed payload is the
    # payload marker, which the installer writes before this file.
    parser.add_argument("--version", action="version",
                        version="walk-blocker %s" % __version__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true", default=True,
                      help="find and report; take no action (the default)")
    mode.add_argument("--kill", action="store_true",
                      help="act on findings. Off until the classifier has "
                           "been read against real traffic.")
    parser.add_argument("--kill-others", action="store_true",
                        help="permit acting on processes owned by other "
                             "users. Without this, --kill only touches your "
                             "own; killing someone else's month-old work is a "
                             "per-incident human decision, not a flag default.")
    parser.add_argument("--max-kills", type=int, default=MAX_KILLS,
                        help="stop acting after this many findings in one "
                             "poll and report the rest as skipped_kill_cap. "
                             "Each act costs the kill grace plus one second "
                             "serially, and the unit's own start timeout is "
                             "what is being protected -- a cap is the honest "
                             "way to stay inside it rather than trusting the "
                             "timeout alone.")
    parser.add_argument("--spool", default=default_spool(),
                        help="state and audit live here; local disk, never "
                             "$HOME, because $HOME may be the filesystem "
                             "under investigation")
    parser.add_argument("--state", default=None, help="override the state file")
    parser.add_argument("--audit", default=None, help="override the audit log")
    parser.add_argument("--min-interval", type=float, default=5.0,
                        help="shortest gap between polls worth differencing; "
                             "below this the fraction is noise")
    parser.add_argument("--settle", type=float, default=SETTLE_S,
                        help="seconds to wait for a second sample when there "
                             "is no previous poll to difference against")
    parser.add_argument("--io-threshold", type=float, default=IO_STALL_FRACTION)
    parser.add_argument("--cpu-threshold", type=float, default=CPU_STALL_FRACTION)
    parser.add_argument("--budget", type=float, default=TRAVERSAL_BUDGET_S)
    parser.add_argument("--fanout", type=int, default=FANOUT_N)
    parser.add_argument("--top", type=int, default=5,
                        help="how many slices to show in the ranking")
    parser.add_argument("--json", action="store_true",
                        help="emit findings as JSON on stdout")
    # Test seams.
    parser.add_argument("--cgroup-root", default=CGROUP_USER_SLICE)
    parser.add_argument("--proc-root", default="/proc")
    parser.add_argument("--mounts", default=MOUNT_TABLE)
    return parser


def run(args, out=sys.stdout, err=sys.stderr, sleep=time.sleep, killer=os.kill,
        alive=None):
    spool = args.spool
    state_path = args.state or os.path.join(spool, "reaper-state.json")
    audit_path = args.audit or os.path.join(spool, "reaper-audit.jsonl")

    state = load_state(state_path)
    previous = state.get("sample")
    # Fetched here, not lower down where the ordinary poll first needs it, so
    # the blind branch below can dedupe against it too -- otherwise a node
    # stuck blind for days would re-earn per-poll audit spam, just for a
    # different fact.
    logged_actions = state.setdefault("logged_actions", {})

    current = sample_slices(args.cgroup_root)
    if not current["slices"]:
        # Not the same as "ran fine, nothing to report": this means the tool
        # cannot see ANYTHING, which a cgroup-layout change would cause
        # silently and permanently. return 0 here read identically to the
        # healthy-and-quiet case below, and the unit's ExecStart= is
        # deliberately NOT `-`-prefixed so this ONE unit's exit code is what
        # reaches `systemctl --failed`.
        #
        # Logged once, not every poll -- cleared the moment slices are visible
        # again (below), so a LATER recurrence is a new event and gets
        # logged again rather than being suppressed forever by a stale entry.
        err.write("no %s*%s found under %s\n"
                  % (SLICE_PREFIX, SLICE_SUFFIX, args.cgroup_root))
        if _latch_blind(logged_actions, audit_path, LATCH_BLIND_SLICES,
                        "no-user-slices"):
            save_state(state_path, state)
        return EXIT_BLIND
    # Cleared unconditionally, not only when this poll goes on to save state
    # via the pruning near the end of this function. `stale` below can still
    # take a second reading and any later failure must not leave a recovered
    # node latched blind forever.
    logged_actions.pop(LATCH_BLIND_SLICES, None)

    stale = previous is None
    if not stale and (current["ts"] - previous["ts"]) < args.min_interval:
        # Two polls a fraction of a second apart divide a near-zero stall by a
        # near-zero window. The quotient is noise, and it is noise that looks
        # exactly like a finding. Running the tool twice by hand does this.
        stale = True
    if stale:
        # No usable prior poll. Taking one reading and calling it a finding
        # would report every slice's entire history since boot.
        err.write("no usable previous sample; taking a second reading in %.0fs\n"
                  % args.settle)
        sleep(args.settle)
        previous = current
        current = sample_slices(args.cgroup_root)

    ranked = stalling_slices(previous, current)
    state["sample"] = current

    if args.top and ranked:
        out.write("io.pressure (full) over the last %.0fs, by user slice:\n"
                  % (current["ts"] - previous["ts"]))
        for uid, io_frac, cpu_frac, cpu_s in ranked[:args.top]:
            out.write("  uid=%-6d io=%6.1f%%  cpu_psi=%6.1f%%  cpu=%s\n" % (
                uid, io_frac * 100.0, cpu_frac * 100.0,
                "%.1fs" % cpu_s if cpu_s is not None else "n/a"))

    stalling = set()
    io_by_uid = {}
    for uid, io_frac, cpu_frac, _cpu_s in ranked:
        io_by_uid[uid] = io_frac
        if io_frac >= args.io_threshold or cpu_frac >= args.cpu_threshold:
            stalling.add(uid)

    # EVERY process, not just the ones under a stalling slice. This used to
    # return early when no slice crossed the threshold, and to narrow the
    # candidates to the stalling uids when one did. Measured at a reference
    # deployment, that gate hid the largest finding on the node: a metadata
    # walk of a parallel filesystem accrues essentially no PSI io, and no
    # per-process counter sees one either -- `getdents64` and `statx` are not
    # read syscalls, so /proc/PID/io is structurally blind to it, and
    # delay accounting is a node-wide sysctl this tree does not flip.
    # Lowering the threshold does not reach it: the wedged walkers sit orders
    # of magnitude below a byte-reading grep, not within a tuning margin.
    #
    # Neither half of the gate's cost argument survives measurement either:
    # scan_procs() below was ALREADY unconditional, so the walk it was meant
    # to avoid was being paid anyway on every poll a slice crossed, and
    # classifying the whole table produced a handful of findings, not a flood.
    #
    # So PSI stops deciding what is looked at. It stays exactly what it is good
    # at -- ranking users by cost, and corroborating a finding on the record
    # below -- and `stalling` now says only whether the slice agreed
    # (ADR-0009).
    mounts = R.read_mounts(args.mounts, POLICY)
    all_procs = scan_procs(args.proc_root)
    if not all_procs:
        # The same shape as the no-user-slice check above, for the input that
        # is now primary. An unreadable or empty /proc means the reaper saw
        # NOTHING -- its own pid is always there on a live node -- and
        # returning 0 with no findings reads identically to a healthy quiet
        # node, the clean bill of health this layer must never give.
        #
        # Latched and cleared exactly like "blind", under its own key so the
        # two cannot dedup against each other, and state is saved so the
        # dedup survives to the next poll.
        err.write("no processes readable under %s\n" % args.proc_root)
        _latch_blind(logged_actions, audit_path, LATCH_BLIND_PROCS,
                     "no-processes")
        # state["sample"] was already set to `current` above and is saved here
        # deliberately: the PSI read succeeded, only /proc failed, so the next
        # poll should difference against THIS sample.
        save_state(state_path, state)
        return EXIT_BLIND
    # Redundant today and kept deliberately. The end-of-poll prune drops every
    # key that is not a finding key, and any poll that reaches THIS line also
    # reaches that prune -- there is no early return between them -- so this
    # cannot be isolated by a test and is not pinned by one. It is here because
    # its sibling above is in exactly the position this one would be in the day
    # someone adds an early return below: that pop WAS redundant too, until the
    # /proc check above made it load-bearing, and nothing warned.
    logged_actions.pop(LATCH_BLIND_PROCS, None)

    findings = classify(all_procs, mounts, budget_s=args.budget,
                        fanout_n=args.fanout)
    new = latch(state, findings)
    new_keys = set(_finding_key(f) for f in new)

    # logged_actions: fetched above, before the blind check -- which ACTION
    # was last written to the audit file for each key, not just which keys
    # have been seen. latch()'s seen-set alone would mean a standing finding
    # is logged once, ever, even after --kill promotion sends it a real TERM
    # and then a real KILL on later polls -- each of those is a new fact the
    # audit trail must not silently drop ("never claim a kill that did not
    # land"). Reported-only findings repeat the same action every poll, so
    # this is exactly where the redundant re-append stops.

    me = os.getuid()
    entries = []
    acted = 0
    failed_kills = 0
    for finding in findings:
        # Set only in the branch that actually calls terminate(): a REAL
        # signal was sent (or attempted) this poll, which is a new fact
        # regardless of whether the resulting action STRING matches the
        # last poll's. A wedged process gets terminate() invoked fresh
        # every poll and can legitimately keep returning the identical
        # "signalled_but_wedged" -- deduping on the string alone silently
        # dropped every signal after the first, which is exactly the
        # "claimed a kill that did not land" shape, inverted: real signals
        # sent, only the first ever shown on disk.
        real_kill_attempt = False
        if args.kill and finding.verdict not in NEVER_KILL:
            if finding.proc.uid != me and not args.kill_others:
                # No signal sent -- a standing POLICY decision, same
                # dedup-eligible shape as plain "reported". A finding capped
                # by --kill-others every poll for the same reason is not a
                # new fact each time; only a real signal is.
                finding.action = "skipped_other_user"
            elif acted >= args.max_kills:
                # Same reasoning: --max-kills binding on the same finding
                # every poll is a standing cap, not a new attempt. If it
                # recurs it recurs identically, which is exactly what
                # dedup exists to collapse -- unlike a wedged process,
                # nothing was sent this poll to under-report.
                finding.action = "skipped_kill_cap"
            else:
                acted += 1
                real_kill_attempt = True
                try:
                    finding.action = terminate(finding.proc, sleep=sleep,
                                               killer=killer, alive=alive)
                except Exception as exc:
                    # An exception here is not caught inside terminate() --
                    # OSError from a real signal already is -- so this is
                    # something unexpected from an injected sleep/killer/alive.
                    # Losing this finding's entry, and every entry already
                    # decided this poll, to an unhandled exception would be
                    # the same defect in the other direction: real signals
                    # sent, nothing on disk to show for it.
                    finding.action = "kill_error"
                    finding.detail = dict(finding.detail,
                                          kill_error="%s: %s" % (
                                              type(exc).__name__, exc))
                if finding.action in KILL_DID_NOT_LAND:
                    failed_kills += 1
        elif finding.verdict in NEVER_KILL:
            finding.action = "reported_never_killed"
        # `io_by_uid` holds exactly the uids stalling_slices() produced a
        # DIFFERENCED reading for, so membership in it -- not in `stalling` --
        # is what separates "measured and quiet" from "never measured". Passing
        # None for the latter leaves the field off the record entirely.
        measured = finding.proc.uid in io_by_uid
        entry = finding.record(
            io_by_uid.get(finding.proc.uid),
            stalling_slice=(finding.proc.uid in stalling) if measured else None)
        entries.append(entry)
        # Written when the action changed, OR a real kill attempt just
        # happened regardless of whether it changed: a standing REPORTED
        # finding is not re-appended in full every poll forever, which is
        # what the action-changed half fixes -- but a real signal is never
        # noise to deduplicate away, whatever it returns. Decided and
        # persisted HERE, immediately after each finding's action, not
        # batched -- `terminate()` sleeps the grace plus one second per
        # finding it acts on, and a `Type=oneshot` unit with no margin left
        # can be killed mid-loop, after real signals landed and before a
        # single entry reached disk.
        key = _finding_key(finding)
        if real_kill_attempt or logged_actions.get(key) != finding.action:
            append_audit(audit_path, [entry])
            logged_actions[key] = finding.action

    # Pruned when the PROCESS dies, not when the finding stops being current.
    #
    # It used to prune to exactly `latched`, and that re-logged a standing
    # finding every time it flickered. `D` state is transient: a process drops
    # out of the arm for one poll, its key is pruned, and the next poll it
    # returns as new -- several rows for one process, the same finding, the
    # same action, in a trail being read to decide whether to promote to
    # --kill.
    #
    # The episode ends when the process does, which is a fact rather than a
    # window someone guessed. `_finding_key` is `verdict:pid:starttime`, so
    # the identity after the verdict is exactly `Proc.key`, and PID reuse
    # cannot mask a new process because the kernel's starttime differs.
    #
    # `latched` is deliberately NOT changed: the stdout NEW marker, and the
    # exit code that says whether this poll found anything new, keep the
    # meanings they had.
    #
    # `alive_keys`, not `alive`: run() already has an `alive` parameter, the
    # liveness callable terminate() is handed.
    alive_keys = set()
    for proc in all_procs.values():
        alive_keys.add(proc.key)
    standing = set(state["latched"])
    state["logged_actions"] = {
        k: v for k, v in logged_actions.items()
        if k in standing or k.split(":", 1)[-1] in alive_keys}

    save_state(state_path, state)

    if args.json:
        out.write(json.dumps(entries, sort_keys=True, indent=2) + "\n")
    else:
        if not findings:
            # Says what was actually looked at. Naming the whole table is what
            # makes a quiet poll evidence of a quiet node rather than evidence
            # of a closed gate.
            out.write("%d process(es) across %d user slice(s), %d of them "
                      "stalling: no traversal this tool can name.\n"
                      % (len(all_procs), len(current["slices"]), len(stalling)))
        for finding, entry in zip(findings, entries):
            out.write(format_finding(entry, _finding_key(finding) in new_keys) + "\n")

    # A real signal that did not land fails the unit on EVERY poll it
    # recurs: it is not a standing fact but a fresh failed action, and the
    # trail already carries one row per attempt. Checked first, because a
    # poll that both found something new and failed to kill something is
    # the second event more than the first. See EXIT_* above.
    if failed_kills:
        return EXIT_KILL_FAILED
    # A new ACTIONABLE finding fails the unit so `systemctl --failed` surfaces
    # it. A standing one does not, or one months-old finding fails the unit
    # forever and the unit stops being a signal anyone reads -- and neither
    # does a new one nobody can act on, which is the same failure arriving
    # many times over days instead of once for months. See EXIT_* above.
    return EXIT_ACTIONABLE if any(
        f.verdict not in NEVER_KILL for f in new) else EXIT_QUIET


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.kill:
        args.report = False
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
