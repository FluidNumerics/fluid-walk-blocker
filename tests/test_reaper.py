"""Layer 2 against synthetic /proc, cgroup and mount fixtures.

Everything here is built on disk and read by the real functions. The reaper's
job is to be right about a live node, and the cases that matter -- PSI
differencing, a wedged D-state process, a finding that must not re-fire -- are
exactly the ones that a mock would let us assert the wrong answer about.

The module under test is the STAMPED reaper (see `_reaper_helpers`): the
in-tree `node/reaper.py` with a fictional site's values compiled in, imported
from a scratch directory beside a copy of the rule table, the way the payload
lays it out. The mount table is conftest's fixture table: `/scratch` and
`/home` are the two mounts of one parallel-filesystem type (`/home` loosened
to depth 4), `/archive` an NFS export, `/` local and cheap. Every uid, pid
and cgroup leaf is a fixture value (ADR-0014).
"""

import io
import json
import os
import shutil
import subprocess
import time

import pytest

from _reaper_helpers import (REQUIRED_SOURCES, load_stamped_reaper,
                             site_values, source_text, stamped_text)
from argv_cases import CASES
from conftest import BASE_MOUNTS, resolve_cwd
import walk_blocker
from walk_blocker import stamp

VALUES = site_values()
reaper = load_stamped_reaper(VALUES)
# The reaper's own rule table -- the copy beside it -- so a monkeypatch on
# `R` lands on the module the reaper actually calls.
R = reaper.R
POLICY = reaper.POLICY

CLK = os.sysconf("SC_CLK_TCK")

# Fixture identities. Two users, and generic cgroup leaves: a seated session,
# a seatless one, a container scope.
UID_A = 1000
UID_B = 1001
SEATED = "session-42.scope"
SEATLESS = "session-c7.scope"
CONTAINER = "docker-abc123.scope"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def write_slice(root, uid, io_full_total, cpu_some_total=0.0, cpu_usage=0.0):
    base = root / ("user-%d.slice" % uid)
    base.mkdir(parents=True, exist_ok=True)
    (base / "io.pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=%d\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=%d\n"
        % (int(io_full_total), int(io_full_total)))
    (base / "cpu.pressure").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=%d\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=%d\n"
        % (int(cpu_some_total), int(cpu_some_total)))
    (base / "cpu.stat").write_text("usage_usec %d\nuser_usec 0\n" % int(cpu_usage))
    return base


def write_proc(root, pid, comm, argv, uid=UID_A, ppid=1, state="D",
               cpu_s=0.0, age_s=0.0, uptime=1000000.0, cwd=None,
               cgroup=SEATED, stdin=None):
    base = root / str(pid)
    base.mkdir(parents=True, exist_ok=True)
    ticks = int((uptime - age_s) * CLK)
    utime = int(cpu_s * CLK)
    fields = ["0"] * 20
    fields[0] = state
    fields[1] = str(ppid)
    fields[11] = str(utime)
    fields[12] = "0"
    fields[19] = str(ticks)
    (base / "stat").write_text("%d (%s) %s\n" % (pid, comm, " ".join(fields)))
    # A kernel thread has a ZERO-BYTE cmdline, and that is a different thing
    # from a process whose single argument is the empty string: joining an
    # empty argv and appending the terminator writes one NUL, which read_proc
    # correctly reads back as [""] -- one empty argument. Writing nothing at
    # all is what models `[kworker/...]`.
    (base / "cmdline").write_bytes(
        b"\0".join(a.encode() for a in argv) + b"\0" if argv else b"")
    (base / "status").write_text("Name:\t%s\nUid:\t%d\t%d\t%d\t%d\n"
                                 % (comm, uid, uid, uid, uid))
    # Defaults to a seated-session scope so the cases written before origin
    # existed keep describing a plausible process. cgroup=None omits the file
    # entirely, which is what a process exiting mid-scan looks like.
    if cgroup is not None:
        (base / "cgroup").write_text(
            "0::/user.slice/user-%d.slice/%s\n" % (uid, cgroup))
    # fd/0, for the tools that decide what they walk by asking whether stdin
    # is a terminal. Omitted by default, which read_proc reads as None -- the
    # unreadable case, and what a process belonging to another user really
    # looks like to an unprivileged scan. Pass a device path to model the
    # other two: os.stat follows the link, so the fixture needs a REAL device
    # rather than a name that looks like one.
    if stdin is not None:
        (base / "fd").mkdir(exist_ok=True)
        try:
            os.symlink(stdin, str(base / "fd" / "0"))
        except OSError:
            pass
    if cwd is not None:
        try:
            os.symlink(cwd, str(base / "cwd"))
        except OSError:
            pass
    (root / "uptime").write_text("%f 0.0\n" % uptime)
    return base


@pytest.fixture
def procfs(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text("1000000.0 0.0\n")
    return root


@pytest.fixture
def mounts_path(tmp_path):
    path = tmp_path / "mounts"
    path.write_text(BASE_MOUNTS)
    return str(path)


def read_mounts(path):
    """The fixture table as the reaper reads it, judged by the stamped policy."""
    return R.read_mounts(path, POLICY)


def scan(procfs_root):
    return reaper.scan_procs(str(procfs_root))


def _read_audit(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _seed_state(tmp_path, uids=("1001",)):
    reaper.save_state(
        os.path.join(str(tmp_path / "spool"), "reaper-state.json"),
        {"sample": {"ts": time.time() - 60.0,
                    "slices": {uid: {"io_full_total": 0.0,
                                     "io_some_total": 0.0,
                                     "cpu_some_total": 0.0,
                                     "cpu_usage_usec": 0.0}
                               for uid in uids}}})


def _stalling_kill_args(tmp_path, cg, procfs_root, mounts_path, extra=()):
    """A `--kill --kill-others` run() wired to seed a stalling uid-1001 slice
    and act against synthetic /proc entries for it, without waiting out the
    real kill grace (the injected sleep in each test makes that free)."""
    _seed_state(tmp_path)
    return reaper.build_parser().parse_args([
        "--kill", "--kill-others",
        "--spool", str(tmp_path / "spool"),
        "--audit", str(tmp_path / "audit.jsonl"),
        "--cgroup-root", str(cg),
        "--proc-root", str(procfs_root),
        "--mounts", mounts_path,
        "--min-interval", "0",
    ] + list(extra))


def _report_args(tmp_path, cg, procfs_root, mounts_path, extra=()):
    """A --report (no --kill) run() wired the same way _stalling_kill_args
    is, for tests about the audit trail itself rather than about killing."""
    _seed_state(tmp_path)
    return reaper.build_parser().parse_args([
        "--spool", str(tmp_path / "spool"),
        "--audit", str(tmp_path / "audit.jsonl"),
        "--cgroup-root", str(cg),
        "--proc-root", str(procfs_root),
        "--mounts", mounts_path,
        "--min-interval", "0",
    ] + list(extra))


def _blind_args(tmp_path, procfs_root, mounts_path, spool=None):
    return reaper.build_parser().parse_args([
        "--spool", str(spool or (tmp_path / "spool")),
        "--audit", str(tmp_path / "audit.jsonl"),
        "--cgroup-root", str(tmp_path / "no-such-cgroup-root"),
        "--proc-root", str(procfs_root),
        "--mounts", mounts_path,
    ])


# --------------------------------------------------------------------------
# the stamp: what the build compiles in (ADR-0013)
# --------------------------------------------------------------------------

def test_the_source_carries_exactly_the_required_markers():
    """The `CONSUMERS["reaper.py"]` row, pinned from the file's side: every
    required source has a marker, and no marker names a source the row does
    not list -- a marker outside the row would be stamped and never checked
    for presence, which is the `missing` class the stamp module calls the one
    failure the gate must never report as success."""
    present = [m.source for m in stamp.find_markers(source_text())]
    assert sorted(present) == sorted(REQUIRED_SOURCES)
    assert len(present) == len(set(present)), "a source stamped twice"


def test_stamped_constants_are_carried_into_the_module():
    """The fictional site's values, not the in-tree placeholders, are what
    the imported module holds -- and the compiled policy is built from them."""
    assert reaper.__version__ == walk_blocker.__version__
    assert reaper.CGROUP_USER_SLICE == VALUES["site.toml:reaper.cgroup_root"]
    assert reaper.SLICE_PREFIX == "user-" and reaper.SLICE_SUFFIX == ".slice"
    assert reaper.TRAVERSAL_BUDGET_S == VALUES["site.toml:reaper.traversal_budget_s"]
    assert reaper.FANOUT_N == VALUES["site.toml:reaper.fanout_n"]
    assert reaper.IO_STALL_FRACTION == VALUES["site.toml:reaper.io_stall_fraction"]
    assert reaper.CPU_STALL_FRACTION == VALUES["site.toml:reaper.cpu_stall_fraction"]
    assert reaper.KILL_GRACE_S == VALUES["site.toml:reaper.kill_grace_s"]
    assert reaper.MAX_KILLS == VALUES["site.toml:reaper.max_kills"]
    assert reaper.SETTLE_S == VALUES["site.toml:reaper.settle_s"]
    assert reaper.AUDIT_MAX_BYTES == VALUES["site.toml:reaper.audit_max_bytes"]
    assert reaper.STREAM_FILTERS == ("tail",)
    assert reaper.MOUNT_TABLE == "/proc/mounts"
    # Lists arrive as tuples: the constant on the node is not meant to change.
    assert isinstance(reaper.ORIGINS, tuple) and isinstance(reaper.REMOTE_FSTYPES, tuple)
    # The policy is the fixture policy, assembled from the stamped fields.
    assert reaper.POLICY_DEFAULTS.remote_fstypes == ("wekafs", "nfs4")
    assert reaper.POLICY_DEFAULTS.remote_proxy is True
    assert reaper.POLICY_DEFAULTS.maxdepth_allowed == 2
    assert reaper.POLICY_DEFAULTS.unscoped_depth == 2
    assert reaper.POLICY_DEFAULTS.depth_allowance_max == 8
    assert reaper.POLICY_DEFAULTS.mounts == {"/home": ("expensive", 4)}


def test_the_parser_defaults_are_the_compiled_values():
    """The thresholds left the rule table and live here; the CLI's defaults
    are the stamped constants, so a `--threshold` left off means the site's
    value and not a number typed into an argparse call."""
    args = reaper.build_parser().parse_args([])
    assert args.io_threshold == reaper.IO_STALL_FRACTION
    assert args.cpu_threshold == reaper.CPU_STALL_FRACTION
    assert args.budget == reaper.TRAVERSAL_BUDGET_S
    assert args.fanout == reaper.FANOUT_N
    assert args.max_kills == reaper.MAX_KILLS
    assert args.settle == reaper.SETTLE_S
    assert args.cgroup_root == reaper.CGROUP_USER_SLICE
    assert args.mounts == reaper.MOUNT_TABLE


def test_a_stale_marker_is_reported_by_check_text():
    """A marker whose value disagrees with the site is `stale`; a marker that
    is gone is `missing`, and missing is the louder of the two on purpose."""
    current = stamped_text(VALUES)
    assert stamp.check_text(current, VALUES, "py", REQUIRED_SOURCES) == []

    fanout = "FANOUT_N = %r  # GENERATED from site.toml:reaper.fanout_n" % (
        VALUES["site.toml:reaper.fanout_n"],)
    assert fanout in current
    drifted = current.replace(fanout, fanout.replace(
        "= %r" % (VALUES["site.toml:reaper.fanout_n"],), "= 99", 1))
    findings = stamp.check_text(drifted, VALUES, "py", REQUIRED_SOURCES)
    assert len(findings) == 1 and findings[0].startswith("stale:"), findings
    assert "FANOUT_N" in findings[0]

    removed = current.replace(fanout + "\n", "")
    findings = stamp.check_text(removed, VALUES, "py", REQUIRED_SOURCES)
    assert findings == ["missing: no `# GENERATED from site.toml:reaper.fanout_n` marker"]

    # The in-tree file is allowed to be stale (its placeholders are not a
    # site's) but must never be missing a required marker.
    for finding in stamp.check_text(source_text(), VALUES, "py", REQUIRED_SOURCES):
        assert finding.startswith("stale:"), finding


def test_the_policy_seams_are_honoured_at_import(tmp_path, monkeypatch):
    """`POLICY = Policy.from_env(POLICY_DEFAULTS)` is the audited seam of
    ADR-0013: the compiled defaults stand, and the two `WALK_BLOCKER_*` test
    seams replace what they name, exactly as the shim treats them."""
    monkeypatch.setenv("WALK_BLOCKER_FSTYPES", "ext4:xfs")
    monkeypatch.setenv("WALK_BLOCKER_DEPTH_BY_MOUNT", "/scratch=3")
    seamed = load_stamped_reaper(VALUES, str(tmp_path))
    assert seamed.POLICY_DEFAULTS.remote_fstypes == ("wekafs", "nfs4")
    assert seamed.POLICY.remote_fstypes == ("ext4", "xfs")
    assert seamed.POLICY.mounts == {"/home": ("expensive", None),
                                    "/scratch": ("expensive", 3)}


def test_version_flag_names_the_product(capsys):
    with pytest.raises(SystemExit) as exc:
        reaper.build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "walk-blocker %s" % walk_blocker.__version__


def test_the_spool_default_is_local_and_named_for_the_product(monkeypatch):
    monkeypatch.delenv("WALK_BLOCKER_SPOOL", raising=False)
    monkeypatch.setenv("USER", "someone")
    assert reaper.default_spool() == "/var/tmp/walk-blocker-someone"
    monkeypatch.setenv("WALK_BLOCKER_SPOOL", "/var/tmp/elsewhere")
    assert reaper.default_spool() == "/var/tmp/elsewhere"


# --------------------------------------------------------------------------
# PSI differencing
# --------------------------------------------------------------------------

def test_psi_totals_are_differenced_not_read(tmp_path):
    """PSI totals are cumulative since boot. Reading totals means every
    finding is a finding forever."""
    root = tmp_path / "cg"
    root.mkdir()
    write_slice(root, UID_A, io_full_total=23.8 * 3600 * 1e6)  # a day's total
    first = reaper.sample_slices(str(root))

    # Second poll one minute later, with no new stall at all.
    write_slice(root, UID_A, io_full_total=23.8 * 3600 * 1e6)
    second = reaper.sample_slices(str(root))
    second["ts"] = first["ts"] + 60.0

    ranked = reaper.stalling_slices(first, second)
    assert ranked, "the slice should still be ranked"
    uid, io_fraction, _cpu, _cpu_s = ranked[0]
    assert uid == UID_A
    assert io_fraction == 0.0, (
        "a lifetime total re-read as a finding is the bug this differencing "
        "exists to prevent")


def test_psi_difference_measures_the_interval(tmp_path):
    root = tmp_path / "cg"
    root.mkdir()
    write_slice(root, UID_A, io_full_total=1000.0 * 1e6)
    first = reaper.sample_slices(str(root))
    # 30 of the next 60 seconds are spent fully stalled on I/O.
    write_slice(root, UID_A, io_full_total=1030.0 * 1e6)
    second = reaper.sample_slices(str(root))
    second["ts"] = first["ts"] + 60.0

    uid, io_fraction, _c, _cs = reaper.stalling_slices(first, second)[0]
    assert uid == UID_A
    assert io_fraction == pytest.approx(0.5, abs=0.01)


def test_first_poll_reports_nothing(tmp_path):
    """With no previous sample there is no interval, so there is no finding."""
    root = tmp_path / "cg"
    root.mkdir()
    write_slice(root, UID_A, io_full_total=9e12)
    only = reaper.sample_slices(str(root))
    assert reaper.stalling_slices(None, only) == []
    assert reaper.stalling_slices({}, only) == []


def test_counters_going_backwards_are_not_a_finding(tmp_path):
    """A reboot or a recreated slice resets the counter; that is not a stall."""
    root = tmp_path / "cg"
    root.mkdir()
    write_slice(root, UID_A, io_full_total=5000.0 * 1e6)
    first = reaper.sample_slices(str(root))
    write_slice(root, UID_A, io_full_total=1.0 * 1e6)
    second = reaper.sample_slices(str(root))
    second["ts"] = first["ts"] + 60.0
    assert reaper.stalling_slices(first, second) == []


def test_slices_are_matched_by_the_compiled_prefix_and_suffix(tmp_path):
    """Only `<prefix><uid><suffix>` directories are slices; a scope, the
    manager service or a stray directory beside them is not sampled."""
    root = tmp_path / "cg"
    root.mkdir()
    write_slice(root, UID_A, io_full_total=1.0)
    (root / "user-notanumber.slice").mkdir()
    (root / "session-42.scope").mkdir()
    (root / "user@1000.service").mkdir()
    assert set(reaper.sample_slices(str(root))["slices"]) == {str(UID_A)}


def test_the_io_threshold_is_a_fraction_in_the_open_interval():
    """Both stall thresholds are fractions of wall time, calibrated per site
    from the differenced rate (ADR-0002); the schema bounds them to (0, 1)
    and the compiled constants must land inside it, or the stall predicate
    is either always or never true."""
    assert 0.0 < reaper.IO_STALL_FRACTION < 1.0
    assert 0.0 < reaper.CPU_STALL_FRACTION < 1.0


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def test_an_interior_empty_argv_element_survives_the_proc_read(procfs, mounts_path):
    """Only the TRAILING empty is an artifact; an interior one is real argv.

    /proc/<pid>/cmdline is NUL-TERMINATED, so the split always yields a final
    empty. Filtering every empty rather than that one made Layer 2 blind to
    `grep -r "" /scratch`: strip the empty and /scratch slides into the
    pattern slot, so the reaper reads the walk as the cwd while Layer 1
    refuses it against /scratch. The two layers must answer the same
    question about the same process.
    """
    write_proc(procfs, 4242, "grep", ["grep", "-r", "", "/scratch"],
               ppid=1, state="D")
    proc = scan(procfs)[4242]
    assert proc.argv == ["grep", "-r", "", "/scratch"], proc.argv
    hits, _unresolved, err = reaper.traversal_roots(proc, read_mounts(mounts_path))
    assert err is None, err
    assert [h[0] for h in hits] == ["/scratch"], hits


def test_orphan_traversal_is_the_incident_shape(procfs, mounts_path):
    """PPID 1, root on the expensive mount: the `find` whose `head` never
    sent SIGPIPE."""
    write_proc(procfs, 4101, "find",
               ["find", "/", "/home", "-type", "f",
                "-name", "slurm-4242_*.out", "-print"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    procs = scan(procfs)
    findings = reaper.classify(procs, read_mounts(mounts_path))
    verdicts = [f.verdict for f in findings]
    assert "orphan_traversal" in verdicts
    finding = [f for f in findings if f.verdict == "orphan_traversal"][0]
    assert finding.detail["fs"] == "wekafs"
    assert finding.detail["mount"] in ("/home", "/scratch")


def test_runaway_traversal_has_a_live_parent(procfs, mounts_path):
    write_proc(procfs, 500, "bash", ["-bash"], uid=UID_B, ppid=1, state="S")
    write_proc(procfs, 4102, "grep", ["grep", "-rIn", "pat", "/home/someone"],
               uid=UID_B, ppid=500, state="D", cpu_s=420.0, age_s=38 * 60)
    procs = scan(procfs)
    findings = reaper.classify(procs, read_mounts(mounts_path))
    assert "runaway_traversal" in [f.verdict for f in findings]


def test_a_young_traversal_with_a_parent_is_left_alone(procfs, mounts_path):
    """Someone's interactive search that started ten seconds ago is work."""
    write_proc(procfs, 500, "bash", ["-bash"], uid=UID_B, ppid=1, state="S")
    write_proc(procfs, 501, "grep", ["grep", "-rIn", "pat", "/scratch"],
               uid=UID_B, ppid=500, state="D", cpu_s=0.4, age_s=10)
    procs = scan(procfs)
    assert reaper.classify(procs, read_mounts(mounts_path)) == []


def test_the_budget_is_the_compiled_constant(procfs, mounts_path):
    """`classify()` reads `TRAVERSAL_BUDGET_S` when no budget is passed: a
    walk one second under it is work, one second over it is a finding."""
    budget = reaper.TRAVERSAL_BUDGET_S
    write_proc(procfs, 500, "bash", ["-bash"], uid=UID_B, ppid=1, state="S")
    write_proc(procfs, 501, "grep", ["grep", "-rIn", "pat", "/scratch"],
               uid=UID_B, ppid=500, state="D", cpu_s=5.0, age_s=budget - 1)
    assert reaper.classify(scan(procfs), read_mounts(mounts_path)) == []
    write_proc(procfs, 501, "grep", ["grep", "-rIn", "pat", "/scratch"],
               uid=UID_B, ppid=500, state="D", cpu_s=5.0, age_s=budget + 1)
    assert [f.verdict for f in reaper.classify(scan(procfs), read_mounts(mounts_path))] == [
        "runaway_traversal"]


def test_orphan_idle_is_reported_never_killed(procfs, mounts_path):
    """PPID-1 readers of pipes whose writers are gone: a leak, not a load."""
    write_proc(procfs, 4103, "grep",
               ["grep", "-E", "--line-buffered", "pattern"],
               uid=UID_B, ppid=1, state="S", cpu_s=0.0, age_s=52 * 86400)
    procs = scan(procfs)
    findings = reaper.classify(procs, read_mounts(mounts_path))
    assert [f.verdict for f in findings] == ["orphan_idle"]
    assert "orphan_idle" in reaper.NEVER_KILL


def test_opaque_traversal_is_counted_but_not_actionable(procfs, mounts_path):
    """python3 in os.walk, rsync, tar. Counted, because otherwise the corpus
    reports the tool list complete when it has only ever looked for itself."""
    write_proc(procfs, 900, "rsync", ["rsync", "-a", "/scratch/x", "/tmp/y"],
               uid=UID_B, ppid=800, state="D", cpu_s=99.0, age_s=3600)
    procs = scan(procfs)
    findings = reaper.classify(procs, read_mounts(mounts_path))
    assert [f.verdict for f in findings] == ["opaque_traversal"]
    assert "opaque_traversal" in reaper.NEVER_KILL


def test_fanout_counts_sixteen_bounded_walks_as_one_finding(procfs, mounts_path):
    """Without this the per-invocation rule is defeatable by division."""
    write_proc(procfs, 700, "bash", ["-bash"], uid=UID_B, ppid=1, state="S")
    write_proc(procfs, 701, "xargs",
               ["xargs", "-P16", "-I{}", "find", "{}", "-maxdepth", "2"],
               uid=UID_B, ppid=700, state="S")
    for i in range(16):
        write_proc(procfs, 800 + i, "find",
                   ["find", "/scratch/shard%d" % i, "-maxdepth", "2", "-name", "x"],
                   uid=UID_B, ppid=701, state="D", cpu_s=5.0, age_s=120)
    procs = scan(procfs)
    findings = reaper.classify(procs, read_mounts(mounts_path))
    fanout = [f for f in findings if f.verdict == "fanout_traversal"]
    assert len(fanout) == 1, "one decision, not sixteen"
    assert fanout[0].detail["count"] == 16


def test_fanout_attributes_through_the_transparent_wrapper(procfs, mounts_path):
    """xargs is transparent: the group is the real parent's, not xargs's own
    invisible one. GENERIC_WRAPPERS is ported for exactly this walk."""
    write_proc(procfs, 700, "bash", ["-bash"], uid=UID_B, ppid=1, state="S")
    write_proc(procfs, 701, "xargs", ["xargs", "-P8", "find"],
               uid=UID_B, ppid=700, state="S")
    for i in range(8):
        write_proc(procfs, 810 + i, "find",
                   ["find", "/scratch/s%d" % i, "-name", "x"],
                   uid=UID_B, ppid=701, state="D", cpu_s=1.0, age_s=60)
    procs = scan(procfs)
    findings = reaper.classify(procs, read_mounts(mounts_path))
    fanout = [f for f in findings if f.verdict == "fanout_traversal"][0]
    assert fanout.detail["group"] == "ppid=700", (
        "grouped under bash, not under the xargs that is merely relaying")


def test_fanout_threshold_is_the_compiled_constant(procfs, mounts_path):
    """One under `FANOUT_N` is not a fan-out; `FANOUT_N` is."""
    n = reaper.FANOUT_N
    write_proc(procfs, 700, "bash", ["-bash"], uid=UID_B, ppid=1, state="S")
    for i in range(n - 1):
        write_proc(procfs, 800 + i, "find", ["find", "/scratch/s%d" % i, "-name", "x"],
                   uid=UID_B, ppid=700, state="D", cpu_s=1.0, age_s=60)
    verdicts = [f.verdict for f in reaper.classify(scan(procfs), read_mounts(mounts_path))]
    assert "fanout_traversal" not in verdicts
    write_proc(procfs, 800 + n - 1, "find", ["find", "/scratch/last", "-name", "x"],
               uid=UID_B, ppid=700, state="D", cpu_s=1.0, age_s=60)
    verdicts = [f.verdict for f in reaper.classify(scan(procfs), read_mounts(mounts_path))]
    assert verdicts.count("fanout_traversal") == 1


def test_a_walk_on_cheap_local_disk_is_not_a_finding(procfs, mounts_path):
    write_proc(procfs, 950, "find", ["find", "/var/log", "-name", "x"],
               uid=UID_B, ppid=1, state="R", cpu_s=1.0, age_s=99999)
    procs = scan(procfs)
    assert reaper.classify(procs, read_mounts(mounts_path)) == []


def test_a_walk_of_the_nfs_export_is_a_finding_too(procfs, mounts_path):
    """The mount class comes from the compiled policy, not from a name: the
    NFS export in the fixture table is expensive by type and its walk is
    reported with its own type on the record (ADR-0016)."""
    write_proc(procfs, 951, "find", ["find", "/archive", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=1.0, age_s=99999)
    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))
    assert [f.verdict for f in findings] == ["orphan_traversal"]
    assert findings[0].detail["fs"] == "nfs4"


def test_unreadable_cwd_is_recorded_not_guessed(procfs, mounts_path):
    """Another user's /proc/<pid>/cwd is not readable. A relative root that
    cannot be resolved is reported as unresolved rather than assumed."""
    write_proc(procfs, 960, "find", ["find", ".", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=10.0, age_s=99999)
    procs = scan(procfs)
    assert procs[960].cwd is None
    hits, unresolved, _err = reaper.traversal_roots(procs[960], read_mounts(mounts_path))
    assert hits == []
    assert unresolved is True


# --------------------------------------------------------------------------
# action honesty
# --------------------------------------------------------------------------

def test_a_process_wedged_in_a_syscall_is_not_reported_as_killed():
    """A `find` blocked in a filesystem syscall does not die on SIGKILL until
    the syscall returns. An audit log that reports success it did not achieve
    is worse than no audit log."""
    proc = reaper.Proc(pid=4242, starttime=1)
    sent = []
    action = reaper.terminate(
        proc, grace_s=0,
        sleep=lambda _s: None,
        killer=lambda pid, sig: sent.append(sig),
        alive=lambda pid: True)  # never dies: still blocked in the syscall
    assert action == "signalled_but_wedged"
    assert sent == [reaper.signal.SIGTERM, reaper.signal.SIGKILL]


def test_a_process_that_dies_on_term_is_terminated_not_killed():
    proc = reaper.Proc(pid=4243, starttime=1)
    sent = []
    action = reaper.terminate(
        proc, grace_s=0, sleep=lambda _s: None,
        killer=lambda pid, sig: sent.append(sig),
        alive=lambda pid: False)
    assert action == "terminated"
    assert sent == [reaper.signal.SIGTERM]


def test_a_process_that_survives_term_but_dies_on_kill():
    proc = reaper.Proc(pid=4244, starttime=1)
    # terminate() checks liveness twice: after the grace period, then after
    # KILL. Survived the first, gone by the second.
    lifetime = [True, False]
    action = reaper.terminate(
        proc, grace_s=0, sleep=lambda _s: None,
        killer=lambda pid, sig: None,
        alive=lambda pid: lifetime.pop(0))
    assert action == "killed"


def test_the_grace_defaults_to_the_compiled_constant():
    proc = reaper.Proc(pid=4245, starttime=1)
    slept = []
    reaper.terminate(proc, sleep=slept.append, killer=lambda pid, sig: None,
                     alive=lambda pid: False)
    assert slept == [reaper.KILL_GRACE_S]


def test_a_process_already_gone_is_said_so():
    def killer(pid, sig):
        raise OSError(reaper.errno.ESRCH, "no such process")
    action = reaper.terminate(reaper.Proc(pid=4246, starttime=1), grace_s=0,
                              sleep=lambda _s: None, killer=killer)
    assert action == "already_gone"


# --------------------------------------------------------------------------
# audit durability
# --------------------------------------------------------------------------

def test_an_entry_lands_on_disk_before_a_later_findings_kill_raises(
        tmp_path, procfs, mounts_path):
    """`terminate()` can only raise via an injected sleep/killer/alive --
    real signal failures are OSErrors it already swallows -- but a `Type=
    oneshot` unit killed mid-loop looks exactly like an unhandled exception
    here: real signals sent, and everything not yet flushed to disk lost.
    Two findings; the second's killer raises. The first's entry must already
    be durable, and the second must still get one rather than vanish."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4101, "find",
               ["find", "/scratch/a", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    write_proc(procfs, 4102, "find",
               ["find", "/scratch/b", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path)

    calls = []

    def killer(pid, sig):
        calls.append(pid)
        if len(calls) > 1:
            raise RuntimeError("injected: killer wedged")

    reaper.run(args, sleep=lambda _s: None, killer=killer,
               alive=lambda pid: False)  # TERM alone is enough to end each

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 2, entries
    actions = sorted(e["action"] for e in entries)
    assert actions == ["kill_error", "terminated"], entries
    errored = [e for e in entries if e["action"] == "kill_error"][0]
    assert "RuntimeError" in errored["kill_error"], errored


def test_append_audit_is_called_once_per_finding_not_once_for_the_batch(
        tmp_path, procfs, mounts_path, monkeypatch):
    """`append_audit` called once after the whole action loop loses every
    entry to a mid-loop kill of the unit. One call per finding, so each is
    durable as soon as its own action is decided."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4103, "find",
               ["find", "/scratch/c", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    write_proc(procfs, 4104, "find",
               ["find", "/scratch/d", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path)

    calls = []
    real_append_audit = reaper.append_audit

    def spy(path, entries):
        calls.append(len(entries))
        real_append_audit(path, entries)

    monkeypatch.setattr(reaper, "append_audit", spy)
    reaper.run(args, sleep=lambda _s: None, killer=lambda pid, sig: None,
               alive=lambda pid: False)

    assert calls == [1, 1], calls


def test_a_standing_finding_is_not_reappended_every_poll(
        tmp_path, procfs, mounts_path):
    """The audit file used to gain a full entry for every currently-matching
    finding on every poll, so the same handful of long-running-process
    records landed on every poll forever. Two polls of the SAME process,
    --report only, so its action ("reported") never changes between them --
    one audit entry, not two."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4105, "find",
               ["find", "/scratch/e", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    reaper.run(args, sleep=lambda _s: None)
    write_slice(cg, UID_B, io_full_total=120.0 * 1e6)
    reaper.run(args, sleep=lambda _s: None)

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 1, entries
    assert entries[0]["action"] == "reported"


def test_a_flickering_finding_is_not_relogged_while_its_process_lives(
        tmp_path, procfs, mounts_path):
    """`D` state is transient, and the dedup used to be pruned to exactly
    what was latched -- so a standing finding that dropped out of the arm for
    one poll came back as new and wrote a second row.

    Three polls. The middle one has the process in `S`, so it is not a finding
    at all; the third has it back in `D`. The process never died, so the
    episode never ended, so one row."""
    cg = tmp_path / "cg"
    args = _report_args(tmp_path, cg, procfs, mounts_path)
    # An unmodelled tool: this is the opaque_traversal arm, where D-state
    # flicker is the ordinary case.
    argv = ["node", "agent/index.js"]

    for poll, (state, io_total) in enumerate(
            (("D", 60.0), ("S", 120.0), ("D", 180.0)), start=1):
        write_slice(cg, UID_B, io_full_total=io_total * 1e6)
        # Same pid AND same starttime throughout: uptime advances with the
        # age so the computed starttime does not move, which is what makes
        # this one process rather than three.
        write_proc(procfs, 4105, "node", argv, uid=UID_B, ppid=900,
                   state=state, cpu_s=6652.1, age_s=4 * 86400 + poll,
                   uptime=1000000.0 + poll)
        reaper.run(args, sleep=lambda _s: None)

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert [(e["verdict"], e["pid"], e["action"]) for e in entries] == [
        ("opaque_traversal", 4105, "reported_never_killed")], entries


def test_flickering_processes_reduce_to_one_row_each(
        tmp_path, procfs, mounts_path):
    """A synthetic population of long-lived unmodelled processes, each
    flickering between D and S across six polls the way a wedged process
    does on a live node. The trail must hold one row per process that can
    produce a record -- never one per D episode -- and the stream followers
    among them must leave no row at all (ADR-0010). The origin labels come
    along on the same sample."""
    # (pid, comm, argv, cgroup leaf, whether it flickers)
    sample = [
        (2001, "ugrep", ["ugrep", "-rln", "needle", "/"], SEATED, True),
        (2002, "node", ["node", "agent/index.js"], SEATLESS, True),
        (2003, "tail", ["tail", "-n", "0", "-F", "/scratch/a.log"], SEATED, True),
        (2004, "monitor-agent", ["monitor-agent", "--config", "x"],
         "monitor-agent.service", True),
        (2005, "python3", ["/usr/bin/python3", "watcher"], "watcher.service", False),
        (1, "systemd", ["/lib/systemd/systemd", "--system"], "init.scope", False),
        (2006, "python", ["python", "gen_status.py"], SEATLESS, False),
        (2007, "node", ["node", "agent/index.js"], SEATLESS, False),
        (2008, "python", ["python", "-m", "uvicorn", "--port", "8000"], SEATLESS, False),
        (2009, "tail", ["tail", "-n", "0", "-F", "/scratch/b.log"], SEATED, False),
        (2010, "python3", ["python3", "probe.py"], "probe.service", False),
        (2011, "bash", ["bash", "run.sh"], CONTAINER, False),
    ]
    cg = tmp_path / "cg"
    args = _report_args(tmp_path, cg, procfs, mounts_path)
    for poll in range(1, 7):
        write_slice(cg, UID_B, io_full_total=60.0 * poll * 1e6)
        for pid, comm, argv, cgroup, flickers in sample:
            write_proc(procfs, pid, comm, argv, uid=UID_B, ppid=900,
                       state=("S" if flickers and poll % 2 == 0 else "D"),
                       cpu_s=6652.1, age_s=4 * 86400 + poll,
                       uptime=1000000.0 + poll, cgroup=cgroup)
        reaper.run(args, sleep=lambda _s: None)

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    seen = sorted(e["pid"] for e in entries)
    expected = sorted(pid for pid, comm, *_ in sample if comm != "tail")
    assert seen == expected, [(e["pid"], e["cmdline"]) for e in entries]
    assert len(entries) == len(sample) - 2
    # `ugrep -rln needle /` is a KNOWN tool walking an expensive mount with a
    # live parent, past budget: the one actionable verdict in the sample.
    by_pid = {e["pid"]: e for e in entries}
    assert by_pid[2001]["verdict"] == "runaway_traversal"
    assert {e["verdict"] for pid, e in by_pid.items() if pid != 2001} == {
        "opaque_traversal"}
    origins = {e["origin"] for e in entries}
    assert {"ssh_seated", "ssh_seatless", "systemd_service", "init",
            "container"} <= origins, origins


def test_a_pid_reused_by_a_new_process_is_logged_again(
        tmp_path, procfs, mounts_path):
    """The inverse, and the reason the retention keys on `Proc.key` rather
    than on the pid: a genuinely NEW process that happens to inherit the pid
    must still reach the trail. The kernel's starttime is what separates
    them."""
    cg = tmp_path / "cg"
    args = _report_args(tmp_path, cg, procfs, mounts_path)
    argv = ["node", "agent/index.js"]

    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4105, "node", argv, uid=UID_B, ppid=900, state="D",
               cpu_s=6652.1, age_s=4 * 86400, uptime=1000000.0)
    reaper.run(args, sleep=lambda _s: None)

    # Same pid, younger by a day against the same uptime -- a different
    # starttime, so a different process.
    write_slice(cg, UID_B, io_full_total=120.0 * 1e6)
    write_proc(procfs, 4105, "node", argv, uid=UID_B, ppid=900, state="D",
               cpu_s=6652.1, age_s=3 * 86400, uptime=1000000.0)
    reaper.run(args, sleep=lambda _s: None)

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 2, entries


def test_a_finding_nobody_can_act_on_does_not_fail_the_unit(
        tmp_path, procfs, mounts_path):
    """Unactionable findings outnumber actionable ones at a reference
    deployment by a wide margin (ADR-0009). Every new finding once exited 1
    and the unit's ExecStart is deliberately not `-`-prefixed, so
    `systemctl --failed` was driven almost entirely by verdicts in NEVER_KILL
    that can never be acted on. An alert nobody can act on teaches people to
    stop reading the alert.

    Reporting is untouched: the row is still written and the table still
    marks it NEW. Only the alerting channel changed."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4105, "node", ["node", "agent/index.js"],
               uid=UID_B, ppid=900, state="D", cpu_s=6652.1, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    out = io.StringIO()
    assert reaper.run(args, out=out, sleep=lambda _s: None) == reaper.EXIT_QUIET

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert [(e["verdict"], e["pid"]) for e in entries] == [
        ("opaque_traversal", 4105)], entries
    assert "NEW" in out.getvalue(), out.getvalue()


def test_an_actionable_finding_still_fails_the_unit_beside_one_that_is_not(
        tmp_path, procfs, mounts_path):
    """The inverse: the unactionable majority must not MASK an actionable
    finding in the same poll. That is `any()`, and this fixture -- a MIXED
    list -- is where `all` would fail in one direction; the empty list is
    where it fails in the other, and the quiet test above pins that edge."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4105, "node", ["node", "agent/index.js"],
               uid=UID_B, ppid=900, state="D", cpu_s=6652.1, age_s=4 * 86400)
    # A known tool, rooted on the expensive mount, live parent, past budget.
    write_proc(procfs, 4106, "find",
               ["find", "/scratch/e", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=500, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    rc = reaper.run(args, sleep=lambda _s: None)
    assert rc == reaper.EXIT_ACTIONABLE, rc

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    verdicts = sorted((e["verdict"], e["pid"]) for e in entries)
    assert verdicts == [("opaque_traversal", 4105),
                        ("runaway_traversal", 4106)], entries


def test_the_three_exit_codes_are_distinct():
    """They have to be, or the split is decorative: a cgroup-layout change
    that blinds Layer 2 permanently and someone running a long grep were the
    same event in `systemctl --failed` until they were separated."""
    assert len({reaper.EXIT_QUIET, reaper.EXIT_ACTIONABLE,
                reaper.EXIT_BLIND}) == 3
    # Both failure codes are nonzero, which is the whole mechanism: a
    # non-zero exit fails a Type=oneshot unit BY DEFAULT.
    assert reaper.EXIT_QUIET == 0
    assert reaper.EXIT_ACTIONABLE and reaper.EXIT_BLIND


def test_a_changed_action_is_appended_again(tmp_path, procfs, mounts_path):
    """The dedup must not hide a REAL change: once --kill is promoted, the
    same finding can legitimately get a new action on a later poll (TERM
    sent, then KILL sent), and each of those is a new fact the audit trail
    must not drop. Same process both polls; `alive` flips from False (TERM
    alone ends it) to True (forces the KILL path) to produce two distinct
    real actions for the same key."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4106, "find",
               ["find", "/scratch/f", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path)

    reaper.run(args, sleep=lambda _s: None, killer=lambda pid, sig: None,
               alive=lambda pid: False)
    write_slice(cg, UID_B, io_full_total=120.0 * 1e6)
    reaper.run(args, sleep=lambda _s: None, killer=lambda pid, sig: None,
               alive=lambda pid: True)

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 2, entries
    assert entries[0]["action"] != entries[1]["action"], entries


def test_a_repeated_identical_kill_attempt_is_not_deduplicated(
        tmp_path, procfs, mounts_path):
    """Deduping on the ACTION STRING alone silently dropped every real signal
    after the first, for exactly the process this repo cares most about --
    one wedged in D state, where terminate() is invoked fresh every poll and
    can legitimately keep returning the identical "signalled_but_wedged".
    Each of those polls sent a REAL SIGTERM and SIGKILL; the audit trail must
    show all of them -- "never claim a kill that did not land" in the other
    direction: a real kill that DID happen must not go unclaimed either."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4109, "find",
               ["find", "/scratch/h", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path)

    killed = []
    for i in range(3):
        write_slice(cg, UID_B, io_full_total=(60.0 + 60.0 * i) * 1e6)
        reaper.run(args, sleep=lambda _s: None,
                   killer=lambda pid, sig: killed.append(sig),
                   alive=lambda pid: True)

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 3, entries
    assert {e["action"] for e in entries} == {"signalled_but_wedged"}
    assert len(killed) == 6, (
        "3 polls x TERM+KILL each -- every one a real signal, not a retry")


def test_a_standing_kill_cap_skip_is_not_reappended_every_poll(
        tmp_path, procfs, mounts_path):
    """The other side of the design decision the test above pins: no signal
    is sent when --max-kills caps a finding, so unlike a real (wedged)
    attempt, a finding capped identically every poll IS dedup-eligible --
    same shape as an ordinary standing "reported" finding, not a repeated
    action."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4110, "find",
               ["find", "/scratch/i", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path,
                               extra=["--max-kills", "0"])

    for i in range(3):
        write_slice(cg, UID_B, io_full_total=(60.0 + 60.0 * i) * 1e6)
        reaper.run(args, sleep=lambda _s: None,
                   killer=lambda pid, sig: None, alive=lambda pid: False)

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 1, entries
    assert entries[0]["action"] == "skipped_kill_cap"


def test_a_repeated_kill_error_is_not_deduplicated(tmp_path, procfs, mounts_path):
    """kill_error means terminate() itself raised -- something unexpected
    from an injected sleep/killer/alive, ambiguous whether a signal landed
    -- so like a real attempt (and unlike a policy skip) it must never be
    silently collapsed across polls: real_kill_attempt is set before the
    `try`, so it survives into this except branch too."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4111, "find",
               ["find", "/scratch/j", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path)

    def killer(pid, sig):
        raise RuntimeError("injected: killer wedged")

    for i in range(3):
        write_slice(cg, UID_B, io_full_total=(60.0 + 60.0 * i) * 1e6)
        reaper.run(args, sleep=lambda _s: None, killer=killer,
                   alive=lambda pid: False)

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 3, entries
    assert {e["action"] for e in entries} == {"kill_error"}


def test_kill_without_kill_others_never_signals_another_users_process(
        tmp_path, procfs, mounts_path):
    """Killing someone else's work is a per-incident human decision, not a
    flag default: `--kill` alone skips another user's finding, sends nothing,
    and says so on the record; `--kill-others` is what unlocks it."""
    other = UID_B if os.getuid() != UID_B else UID_A
    cg = tmp_path / "cg"
    write_slice(cg, other, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4112, "find",
               ["find", "/scratch/k", "-type", "f", "-name", "x"],
               uid=other, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    _seed_state(tmp_path, uids=(str(other),))
    args = reaper.build_parser().parse_args([
        "--kill",
        "--spool", str(tmp_path / "spool"),
        "--audit", str(tmp_path / "audit.jsonl"),
        "--cgroup-root", str(cg),
        "--proc-root", str(procfs),
        "--mounts", mounts_path,
        "--min-interval", "0",
    ])

    signalled = []
    reaper.run(args, sleep=lambda _s: None,
               killer=lambda pid, sig: signalled.append((pid, sig)),
               alive=lambda pid: False)

    assert signalled == [], signalled
    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert [e["action"] for e in entries] == ["skipped_other_user"], entries

    # The same finding, with --kill-others: a real signal, and a new action.
    write_slice(cg, other, io_full_total=120.0 * 1e6)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path)
    reaper.run(args, sleep=lambda _s: None,
               killer=lambda pid, sig: signalled.append((pid, sig)),
               alive=lambda pid: False)
    assert [pid for pid, _sig in signalled] == [4112]
    assert [e["action"] for e in _read_audit(str(tmp_path / "audit.jsonl"))] == [
        "skipped_other_user", "terminated"]


def test_append_audit_rotates_past_the_size_threshold(tmp_path, monkeypatch):
    """No logrotate snippet is shipped and the installer's write locations
    are a deliberately closed set, so the audit file rotates itself rather
    than growing forever."""
    monkeypatch.setattr(reaper, "AUDIT_MAX_BYTES", 100)
    path = str(tmp_path / "audit.jsonl")
    reaper.append_audit(path, [{"a": "x" * 200}])
    assert os.path.getsize(path) > 100

    reaper.append_audit(path, [{"a": "second"}])

    assert os.path.exists(path + ".1"), "the oversized file must be rotated aside"
    with open(path + ".1") as fh:
        assert "x" * 200 in fh.read()
    # `version` is stamped by append_audit() itself, so a written record is
    # never byte-identical to the dict handed in. Compared field-wise: what
    # this test is about is that the post-rotation file holds ONLY the second
    # entry.
    rotated = _read_audit(path)
    assert [dict(e, version=None) for e in rotated] == [
        {"a": "second", "version": None}], (
        "the fresh file must hold only what was appended after rotation")
    assert rotated[0]["version"] == reaper.__version__


def test_append_audit_does_not_trust_the_creating_umask(tmp_path):
    """`open(path, 'a')` on a brand-new file is 0666 & ~umask; the trail is
    world-readable by decision (ADR-0012), so the mode is set explicitly."""
    old_umask = os.umask(0o077)
    try:
        path = str(tmp_path / "audit.jsonl")
        reaper.append_audit(path, [{"a": 1}])
    finally:
        os.umask(old_umask)
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o644, oct(mode)


def test_save_state_does_not_trust_the_creating_umask_either(tmp_path):
    """The sibling of the test above, for the OPPOSITE failure: the umask
    being ignored entirely, as a directory carrying a POSIX default ACL does.
    `os.umask(0)` reproduces the effect without needing an ACL. The chmod is
    on the TEMP file, before the rename, so the live path is never briefly
    group-writable."""
    old_umask = os.umask(0)
    try:
        path = str(tmp_path / "reaper-state.json")
        reaper.save_state(path, {"latched": []})
    finally:
        os.umask(old_umask)
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o644, oct(mode)
    assert not os.path.exists(path + ".tmp")


def test_every_audit_record_names_the_build_that_wrote_it(
        tmp_path, procfs, mounts_path):
    """Rows outlive deploys. EVERY record carries `version`, including the
    two blind-state entries no Finding builds, because it is stamped in
    append_audit() -- the single writer every call site passes through.
    Asserted against the module's own `__version__`, which the stamp set."""
    audit = str(tmp_path / "audit.jsonl")
    write_proc(procfs, 4106, "find", ["find", "/scratch", "-name", "q"],
               age_s=9000)
    finding = reaper.classify(scan(procfs), read_mounts(mounts_path))[0]
    built = finding.record()
    blind = {"ts": 1.0, "layer": "reaper", "action": "blind",
             "state": "no-user-slices"}
    procs_blind = {"ts": 2.0, "layer": "reaper", "action": "blind",
                   "state": "no-processes"}
    reaper.append_audit(audit, [built, blind, procs_blind])

    written = _read_audit(audit)
    assert len(written) == 3, written
    for entry in written:
        assert entry["version"] == reaper.__version__, entry
        assert entry["layer"] == "reaper"

    # The builder's dict is not the writer's to mutate.
    assert "version" not in built
    assert "version" not in blind


def test_the_blind_records_carry_the_build_through_the_real_path(
        tmp_path, procfs, mounts_path):
    """The end-to-end half: the entry run() actually writes when the reaper
    goes blind, which is when someone wants to know what is deployed."""
    args = _blind_args(tmp_path, procfs, mounts_path)
    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_BLIND

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert [e["action"] for e in entries] == ["blind"], entries
    assert entries[0]["version"] == reaper.__version__
    assert entries[0]["layer"] == "reaper"


def test_a_version_change_alone_does_not_defeat_the_dedup(
        tmp_path, procfs, mounts_path, monkeypatch):
    """The FIXED-STRING latch shape. `logged_actions["blind"]` stores a
    constant; a version stamp cannot leak into that identity without
    someone typing it there, which is what this test forbids."""
    audit = str(tmp_path / "audit.jsonl")
    args = _blind_args(tmp_path, procfs, mounts_path)

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_BLIND
    first = _read_audit(audit)
    assert len(first) == 1, first

    # The redeploy: same state, same spool, a different build.
    monkeypatch.setattr(reaper, "__version__", "99.0.0")
    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_BLIND

    after = _read_audit(audit)
    assert len(after) == 1, (
        "a version change must not re-log a standing state: %r" % (after,))
    assert after[0]["version"] == first[0]["version"]


def test_a_version_change_alone_does_not_relog_a_standing_finding(
        tmp_path, procfs, mounts_path, monkeypatch):
    """The MUTABLE latch shape. `logged_actions[_finding_key(finding)]`
    stores `finding.action`, so unlike the fixed-string latches it is a value
    a change could thread the version through. On the node that regression
    re-logs every standing finding on every redeploy, in the trail the
    `--kill` promotion is read from."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4107, "find",
               ["find", "/scratch/e", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    reaper.run(args, sleep=lambda _s: None)
    first = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(first) == 1, first
    assert first[0]["action"] == "reported", first

    write_slice(cg, UID_B, io_full_total=120.0 * 1e6)
    monkeypatch.setattr(reaper, "__version__", "99.0.0")
    reaper.run(args, sleep=lambda _s: None)

    after = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(after) == 1, (
        "a version change must not re-log a standing finding: %r" % (after,))


def test_the_state_temp_file_is_never_left_group_writable(tmp_path,
                                                          monkeypatch):
    """A crash between the write and the rename must not leave the refusal
    behind under a different filename. save_state() sets the mode at CREATE
    time rather than before the rename. Driven by making os.replace raise,
    which is the one step that turns a completed write into an orphan."""
    def explode(_src, _dst):
        raise OSError("interrupted")
    monkeypatch.setattr(os, "replace", explode)

    path = str(tmp_path / "reaper-state.json")
    old_umask = os.umask(0)
    try:
        with pytest.raises(OSError):
            reaper.save_state(path, {"latched": []})
    finally:
        os.umask(old_umask)

    orphan = path + ".tmp"
    assert os.path.exists(orphan), "the orphan is the case under test"
    mode = os.stat(orphan).st_mode & 0o777
    assert mode == 0o644, oct(mode)


def test_a_preexisting_temp_file_does_not_keep_its_old_mode(tmp_path):
    """The case the create mode alone CANNOT cover, and the only reason the
    fchmod exists: `O_CREAT`'s mode argument is ignored when the file already
    exists, so a `.tmp` left group-writable by an older build would keep its
    mode through O_TRUNC and `os.replace` would carry it onto the state
    file."""
    path = str(tmp_path / "reaper-state.json")
    stale = path + ".tmp"
    with open(stale, "w") as fh:
        fh.write("{}")
    os.chmod(stale, 0o664)

    reaper.save_state(path, {"latched": []})

    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o644, oct(mode)


@pytest.mark.skipif(shutil.which("setfacl") is None,
                    reason="setfacl unavailable; the ACL cause cannot be built")
def test_a_real_default_acl_does_not_make_the_state_file_group_writable(
        tmp_path):
    """The actual mechanism, not the stand-in. A POSIX default ACL suppresses
    the umask at creation and intersects the create mode with the default
    entries instead; if the umask simulation and this ever diverge, the
    umask tests would not notice."""
    spool = tmp_path / "spool"
    spool.mkdir()
    rc = subprocess.run(["setfacl", "-d", "-m", "g::rwx", str(spool)],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        pytest.skip("filesystem rejected the default ACL: %s" % rc.stderr)

    path = str(spool / "reaper-state.json")
    reaper.save_state(path, {"latched": []})
    mode = os.stat(path).st_mode & 0o777
    assert not mode & 0o022, "group/other must never gain w: %s" % oct(mode)


# --------------------------------------------------------------------------
# blind states
# --------------------------------------------------------------------------

def test_the_reaper_fails_loudly_when_it_cannot_see_any_slice(
        tmp_path, procfs, mounts_path):
    """An empty or nonexistent --cgroup-root used to `return 0` -- silently
    identical to "ran fine, nothing to report" -- so a cgroup-layout change
    would make Layer 2 permanently blind while the unit kept reporting
    success."""
    args = _blind_args(tmp_path, procfs, mounts_path)
    rc = reaper.run(args, sleep=lambda _s: None)
    assert rc == reaper.EXIT_BLIND

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 1, entries
    assert entries[0]["action"] == "blind"
    assert entries[0]["state"] == "no-user-slices"


def test_a_standing_blindness_is_not_reappended_every_poll(
        tmp_path, procfs, mounts_path):
    """A node stuck blind for days must not re-earn one audit line per poll
    just because the poll happened again."""
    args = _blind_args(tmp_path, procfs, mounts_path)
    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_BLIND
    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_BLIND

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 1, entries


def test_blindness_is_reported_again_after_recovering(
        tmp_path, procfs, mounts_path):
    """The dedup must not be permanent: run() clears "blind" the moment
    slices are visible again, so a LATER recurrence is a new event and is
    logged again."""
    cg = tmp_path / "cg"
    spool = tmp_path / "spool"
    blind_args = _blind_args(tmp_path, procfs, mounts_path, spool=spool)
    healthy_args = reaper.build_parser().parse_args([
        "--spool", str(spool),
        "--audit", str(tmp_path / "audit.jsonl"),
        "--cgroup-root", str(cg),
        "--proc-root", str(procfs),
        "--mounts", mounts_path,
        "--min-interval", "0",
    ])

    assert reaper.run(blind_args, sleep=lambda _s: None) == reaper.EXIT_BLIND
    write_slice(cg, UID_B, io_full_total=0.0)  # a real, quiet slice: recovers
    # And a real, harmless process. An EMPTY /proc is not a healthy node --
    # the reaper's own pid is always in there -- so it is its own blind
    # condition, and this poll has to describe the node it claims to.
    write_proc(procfs, 5000, "bash", ["/bin/bash", "-l"],
               uid=UID_B, ppid=900, state="S", cpu_s=0.5, age_s=30.0)
    assert reaper.run(healthy_args, sleep=lambda _s: None) == reaper.EXIT_QUIET
    assert reaper.run(blind_args, sleep=lambda _s: None) == reaper.EXIT_BLIND

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    blind_entries = [e for e in entries
                     if e["action"] == "blind" and e["state"] == "no-user-slices"]
    assert len(blind_entries) == 2, entries


def test_an_unreadable_proc_is_blind_not_a_clean_bill_of_health(
        tmp_path, mounts_path):
    """`/proc` is the reaper's primary input (ADR-0009). scan_procs()
    returning nothing cannot happen on a live node -- the reaper's own pid is
    always there -- so it means the scan failed, and returning 0 with no
    findings would read identically to a healthy quiet node. Latched like the
    no-user-slice check, under its own key so the two cannot dedup against
    each other."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=0.0)
    empty_proc = tmp_path / "empty-proc"
    empty_proc.mkdir()
    (empty_proc / "uptime").write_text("1000000.0 1000000.0\n")
    args = _report_args(tmp_path, cg, empty_proc, mounts_path)

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_BLIND
    # Twice: the second poll must not re-append, same dedup as "blind".
    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_BLIND

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert [e["state"] for e in entries] == ["no-processes"], entries


def test_the_blind_clear_survives_a_poll_that_never_reaches_the_prune(
        tmp_path, procfs, mounts_path):
    """Why `logged_actions.pop("blind", None)` is unconditional. The
    end-of-poll prune clears "blind" on any poll that runs to completion; the
    pop only earns its place on a poll that clears "blind" and then returns
    EARLY, before the prune -- `/proc` being unreadable is exactly such a
    poll. blind -> (slices back, but /proc empty) -> blind: without the pop,
    the middle poll leaves "blind" latched and the third spell is never
    logged."""
    cg = tmp_path / "cg"
    empty_proc = tmp_path / "empty-proc"
    empty_proc.mkdir()
    (empty_proc / "uptime").write_text("1000000.0 1000000.0\n")
    spool = tmp_path / "spool"
    audit = tmp_path / "audit.jsonl"

    def _args(cgroup_root, proc_root):
        return reaper.build_parser().parse_args([
            "--spool", str(spool), "--audit", str(audit),
            "--cgroup-root", str(cgroup_root), "--proc-root", str(proc_root),
            "--mounts", mounts_path, "--min-interval", "0",
        ])

    no_slices = _args(tmp_path / "no-such-cgroup-root", procfs)
    assert reaper.run(no_slices, sleep=lambda _s: None) == reaper.EXIT_BLIND

    write_slice(cg, UID_B, io_full_total=0.0)
    assert reaper.run(_args(cg, empty_proc), sleep=lambda _s: None) == \
        reaper.EXIT_BLIND

    assert reaper.run(no_slices, sleep=lambda _s: None) == reaper.EXIT_BLIND

    entries = _read_audit(str(audit))
    no_slice_entries = [e for e in entries if e.get("state") == "no-user-slices"]
    assert len(no_slice_entries) == 2, entries


def test_max_kills_caps_a_poll_and_still_audits_the_rest(
        tmp_path, procfs, mounts_path):
    """A cap is the honest way to keep a `Type=oneshot` unit inside its start
    timeout, rather than trusting the timeout to land at a convenient
    moment. What it skips is still reported, not silently dropped."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4105, "find",
               ["find", "/scratch/e", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    write_proc(procfs, 4106, "find",
               ["find", "/scratch/f", "-type", "f", "-name", "x"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path,
                               extra=["--max-kills", "1"])

    killed = []
    lifetime = [True, False]
    reaper.run(args, sleep=lambda _s: None,
               killer=lambda pid, sig: killed.append((pid, sig)),
               alive=lambda pid: lifetime.pop(0))

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 2, entries
    actions = sorted(e["action"] for e in entries)
    assert actions == ["killed", "skipped_kill_cap"], entries
    assert len(killed) == 2, "one process TERM'd then KILL'd, the other untouched"


# --------------------------------------------------------------------------
# latching
# --------------------------------------------------------------------------

def _finding(pid, verdict="runaway_traversal"):
    return reaper.Finding(reaper.Proc(pid=pid, starttime=7), verdict, {})


def test_a_standing_finding_does_not_keep_failing_the_unit():
    """Without this a months-old finding fails the unit forever and
    `systemctl --failed` stops being a signal anyone reads."""
    state = {}
    first = _finding(4104)
    assert len(reaper.latch(state, [first])) == 1, "first sighting is new"
    again = _finding(4104)
    assert reaper.latch(state, [again]) == [], "still there is not news"


def test_a_finding_that_goes_away_and_returns_is_new_again():
    state = {}
    reaper.latch(state, [_finding(10)])
    reaper.latch(state, [])
    assert len(reaper.latch(state, [_finding(10)])) == 1


def test_pid_reuse_does_not_mask_a_new_finding():
    """PID alone is reused; PID plus the kernel's starttime is not."""
    state = {}
    old = reaper.Finding(reaper.Proc(pid=555, starttime=100), "runaway_traversal", {})
    new = reaper.Finding(reaper.Proc(pid=555, starttime=999), "runaway_traversal", {})
    assert len(reaper.latch(state, [old])) == 1
    assert len(reaper.latch(state, [new])) == 1


def test_the_state_file_round_trips_and_a_broken_one_is_empty(tmp_path):
    path = str(tmp_path / "spool" / "reaper-state.json")
    reaper.save_state(path, {"latched": ["a:1:2"], "sample": {"ts": 1.0, "slices": {}}})
    assert reaper.load_state(path) == {"latched": ["a:1:2"],
                                       "sample": {"ts": 1.0, "slices": {}}}
    with open(path, "w") as fh:
        fh.write("{not json")
    assert reaper.load_state(path) == {}
    assert reaper.load_state(str(tmp_path / "nowhere.json")) == {}


def test_two_polls_too_close_together_are_not_differenced(tmp_path):
    """Dividing a near-zero stall by a near-zero window yields noise that looks
    exactly like a finding."""
    cg = tmp_path / "cg"
    cg.mkdir()
    write_slice(cg, UID_A, io_full_total=1000.0 * 1e6)

    args = reaper.build_parser().parse_args([
        "--spool", str(tmp_path / "spool"),
        "--cgroup-root", str(cg),
        "--proc-root", str(tmp_path / "noproc"),
        "--settle", "0",
    ])
    reaper.save_state(
        os.path.join(str(tmp_path / "spool"), "reaper-state.json"),
        {"sample": {"ts": time.time(),
                    "slices": {str(UID_A): {"io_full_total": 1000.0 * 1e6,
                                            "io_some_total": 0.0,
                                            "cpu_some_total": 0.0,
                                            "cpu_usage_usec": 0.0}}}})

    slept = []
    reaper.run(args, sleep=lambda s: slept.append(s))
    assert slept, "a too-short interval must be resampled, not trusted"


# --------------------------------------------------------------------------
# Origin: how a process reached the node (ADR-0003)
# --------------------------------------------------------------------------

def test_origin_separates_the_two_session_shapes(procfs, mounts_path):
    """A finding has to say which way in the process came: one that arrived
    through a login shell is one Layer 1 was in front of and did not stop.
    logind's seated and seatless session scopes are the two universal
    shapes, and the example site labels them by transport."""
    write_proc(procfs, 4100, "find", ["find", "/scratch", "-name", "x"],
               age_s=9000, cgroup=SEATED)
    write_proc(procfs, 4101, "find", ["find", "/scratch", "-name", "y"],
               age_s=9000, cgroup=SEATLESS)
    procs = scan(procfs)
    findings = reaper.classify(procs, read_mounts(mounts_path))
    by_pid = {f.proc.pid: f.record()["origin"] for f in findings}
    assert by_pid[4100] == "ssh_seated"
    assert by_pid[4101] == "ssh_seatless"


def test_origin_marks_what_layer_one_never_applied_to(procfs, mounts_path):
    """A scheduler step or a container never had the shim on its PATH at all.
    A leaf the site's table does not name is `other`, not a guess."""
    write_proc(procfs, 4102, "find", ["find", "/scratch", "-name", "z"],
               age_s=9000, cgroup="step_0")
    finding = reaper.classify(scan(procfs), read_mounts(mounts_path))[0]
    assert finding.record()["origin"] == "other"


def test_a_container_is_separated_from_other_origins(procfs, mounts_path):
    """ADR-0001 lists containers as something the shim cannot reach at all.
    Folding them into `other` alongside services would hide the answer the
    audit log is read for."""
    write_proc(procfs, 4104, "find", ["find", "/scratch", "-name", "c"],
               age_s=9000, cgroup=CONTAINER)
    finding = reaper.classify(scan(procfs), read_mounts(mounts_path))[0]
    assert finding.record()["origin"] == "container"


def test_a_process_without_a_readable_cgroup_is_unknown_not_an_error(
        procfs, mounts_path):
    """/proc entries vanish under the scan. An unreadable cgroup file is a
    missing field, never a traceback that ends the poll."""
    write_proc(procfs, 4103, "find", ["find", "/scratch", "-name", "q"],
               age_s=9000, cgroup=None)
    entry = reaper.classify(scan(procfs), read_mounts(mounts_path))[0].record()
    assert entry["origin"] == "unknown"
    assert entry["leaf_cgroup"] is None


def test_classify_origin_maps_every_example_label_and_the_two_fallbacks():
    """The compiled table, row by row, plus `other` for a shape it has not
    seen and `unknown` for a leaf that could not be read."""
    assert reaper.classify_origin("watcher.service") == "systemd_service"
    assert reaper.classify_origin("init.scope") == "init"
    assert reaper.classify_origin(SEATED) == "ssh_seated"
    assert reaper.classify_origin(SEATLESS) == "ssh_seatless"
    assert reaper.classify_origin(CONTAINER) == "container"
    assert reaper.classify_origin("user.slice") == "other"
    assert reaper.classify_origin("step_0") == "other"
    assert reaper.classify_origin(None) == "unknown"
    assert reaper.classify_origin("") == "unknown"


def test_origins_come_from_config_and_the_first_match_wins(tmp_path):
    """The label set is `[reaper].origins`, compiled in: a site that runs a
    scheduler adapter adds one row and its trail says so. Order is the
    site's, first match wins, a leaf no row matches is `other`, and the two
    fallbacks are code -- present whatever the table says."""
    origins = [
        {"pattern": r"^step_\d+$", "label": "scheduler_step"},
        {"pattern": r"^session-c\d+\.scope$", "label": "ssh_seatless"},
        # Overlaps the row above on purpose: the earlier row must win.
        {"pattern": r"^session-", "label": "any_session"},
    ]
    custom = load_stamped_reaper(
        site_values(**{"site.toml:reaper.origins": origins}), str(tmp_path))
    assert custom.ORIGINS == tuple(origins)
    assert custom.classify_origin("step_0") == "scheduler_step"
    assert custom.classify_origin(SEATLESS) == "ssh_seatless"
    assert custom.classify_origin(SEATED) == "any_session"
    assert custom.classify_origin(CONTAINER) == "other"
    assert custom.classify_origin(None) == "unknown"
    # The default module is untouched by a second load.
    assert reaper.classify_origin(SEATED) == "ssh_seated"


def test_the_audit_record_carries_the_other_half_of_its_own_dedup_key(
        procfs, mounts_path):
    """`_finding_key` is `verdict:pid:starttime`. A reader who cannot
    recompute the key can only count ROWS, and a standing process is
    re-logged every poll. The assertion is RECONSTRUCTION, not presence: a
    field named `starttime` that did not match the key would be worse than
    no field."""
    write_proc(procfs, 4104, "find", ["find", "/scratch", "-name", "q"],
               age_s=9000)
    finding = reaper.classify(scan(procfs), read_mounts(mounts_path))[0]
    entry = finding.record()
    rebuilt = "%s:%s:%s" % (entry["verdict"], entry["pid"], entry["starttime"])
    assert rebuilt == reaper._finding_key(finding)


def test_origin_does_not_change_any_verdict(procfs, mounts_path):
    """Origin is descriptive. A process is not more or less of a runaway
    because of how its owner logged in, and wiring it into classify() would
    make the kill decision depend on the transport. Every label, `other` and
    `unknown` included, yields the same verdict on the same process."""
    verdicts = []
    for pid, cg in ((4200, SEATED),
                    (4201, SEATLESS),
                    (4202, CONTAINER),
                    (4203, "watcher.service"),
                    (4204, "init.scope"),
                    (4205, "step_0"),
                    (4206, None)):
        root = procfs / ("origin-%d" % pid)
        root.mkdir()
        write_proc(root, pid, "find", ["find", "/scratch", "-name", "x"],
                   age_s=9000, cgroup=cg)
        findings = reaper.classify(scan(root), read_mounts(mounts_path))
        verdicts.append(findings[0].verdict)
    assert len(set(verdicts)) == 1, verdicts


def test_a_remote_command_attributes_to_its_shell_not_to_the_transport_daemon(
        procfs, mounts_path):
    """Under a transport that incubates the session shell as its own child
    the ancestry is find -> bash -> transportd -> transportd -> systemd,
    where under a classic sshd it is find -> bash -> sshd. effective_parent
    must stop at the session shell either way. Adding `bash` to
    GENERIC_WRAPPERS is a plausible future edit; it would silently
    reattribute every remote command on the node to the daemon (ADR-0003)."""
    write_proc(procfs, 5000, "transportd", ["transportd"], ppid=1)
    write_proc(procfs, 5001, "transportd", ["transportd"], ppid=5000)
    write_proc(procfs, 5002, "bash", ["bash", "-c", "find /scratch -name x"],
               ppid=5001, cgroup=SEATLESS)
    write_proc(procfs, 5003, "find", ["find", "/scratch", "-name", "x"],
               ppid=5002, age_s=9000, cgroup=SEATLESS)
    procs = scan(procfs)
    parent = reaper.effective_parent(procs[5003], procs)
    assert parent is not None and parent.comm == "bash", (
        "expected the session shell, got %s"
        % (parent.comm if parent else None))
    assert "bash" not in R.GENERIC_WRAPPERS


# --------------------------------------------------------------------------
# traversal_roots against the shared rule table
# --------------------------------------------------------------------------

class _Proc:
    """The fields traversal_roots() reads.

    `stdin_tty` defaults True because that is what R.check() defaults to, and
    these two have to be driven from the same assumption or the agreement
    test below compares a terminal against a pipe and calls the difference a
    drift. A process really on the end of a pipe is a separate, deliberate
    divergence -- see the stdin tests further down."""

    def __init__(self, argv, cwd, stdin_tty=True):
        self.comm = argv[0]
        self.argv = argv
        self.cwd = cwd
        self.stdin_tty = stdin_tty


def test_a_tool_that_renames_itself_is_still_seen(procfs, mounts_path):
    """`comm` is 15 bytes the process may overwrite, and on a login node the
    thing overwriting it is usually the wrapper -- a coding agent's shell
    functions launch the search tools through `exec -a <name>` and set comm
    to the agent's own version string. Keyed on comm, both halves of the
    classifier agreed on the wrong answer: no profile, no hits, `known_tool`
    False, and no audit record at all for an unbounded walk of / that had
    been running for days. argv[0] is what resolves the tool."""
    write_proc(procfs, 4301, "9.9.999",
               ["bfs", "-S", "dfs", "-regextype", "findutils-default", "/",
                "-name", "dump.py", "-path", "*some_data*"],
               uid=UID_B, ppid=900, state="R", cpu_s=67643.0, age_s=177584,
               cwd="/var/tmp")
    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))
    verdicts = [f.verdict for f in findings]
    assert verdicts, "a self-renamed traversal left no record at all"
    assert "runaway_traversal" in verdicts, verdicts
    finding = [f for f in findings if f.verdict == "runaway_traversal"][0]
    assert finding.detail["fs"] == "wekafs"
    assert finding.detail["mount"] in ("/home", "/scratch"), finding.detail


def test_an_unmodelled_leading_option_is_reported_not_swallowed(
        procfs, mounts_path):
    """The fail-safe for the next bfs: an operand scan that charges nothing
    falls back to the cwd, and from a CHEAP cwd that produces no hits --
    indistinguishable from a clean bill of health. An absolute path left
    unaccounted for in argv is what tells a missed root from a genuinely
    cwd-rooted walk."""
    write_proc(procfs, 4302, "bfs",
               ["bfs", "-Q", "unknown-flag", "/", "-name", "dump.py"],
               uid=UID_B, ppid=900, state="R", cpu_s=9000.0, age_s=100000,
               cwd="/var/tmp")
    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))
    assert [f.verdict for f in findings] == ["unparsed_traversal"], findings
    assert "/" in findings[0].detail["root_error"]


def test_a_genuinely_cwd_rooted_walk_is_not_reported_as_unparsed(
        procfs, mounts_path):
    """`find -name foo` really does walk the cwd, so the fail-safe above must
    stay quiet: a backstop that cries wolf on correct commands produces a
    trail nobody reads."""
    write_proc(procfs, 4303, "find", ["find", "-name", "foo"],
               uid=UID_B, ppid=900, state="R", cpu_s=9000.0, age_s=100000,
               cwd="/var/tmp")
    assert reaper.classify(scan(procfs), read_mounts(mounts_path)) == []


def test_layer2_agrees_with_layer1_on_every_matrix_row(mounts, policy, node_fs,
                                                       fixture_home):
    """The reaper is Layer 1's backstop, so a walk Layer 1 refuses must be a
    walk Layer 2 reports. They drifted once and it was invisible: check()
    applied effective_cwd() and traversal_roots() did not. Both now go
    through resolved_roots(), asserted across the whole shared matrix rather
    than for the one row that surfaced it.

    IMPLICATION, not equivalence. The converse is not an invariant and must
    not be asserted: the reaper does not consult bounded(), so it reports a
    walk rooted on an expensive filesystem even when Layer 1 allowed it as
    depth-bounded. Layer 1 judges a command before it runs, while Layer 2 is
    looking at a process already past budget, and a bounded walk doing that
    is still worth naming."""
    missed = []
    for case_id, argv, cwd, _expected, _note in CASES:
        real_cwd = resolve_cwd(cwd, node_fs)
        if R.check(argv, real_cwd, mounts, policy) is None:
            continue
        hits, _unresolved, _err = reaper.traversal_roots(
            _Proc(argv, real_cwd), mounts, policy)
        if not hits:
            missed.append(case_id)
    assert missed == [], (
        "Layer 1 refuses these but Layer 2 reports nothing -- the backstop is "
        "blind to %d shape(s): %s" % (len(missed), missed))


def test_layer2_does_not_name_a_mount_a_device_bound_walk_never_enters(
        mounts, policy):
    """The reaper does not consult bounded(), and that is right: a shallow
    walk of an expensive mount past budget is still worth naming. A DEVICE
    bound is a different claim -- `find / -xdev` cannot cross onto /scratch
    at all, so a finding that says it is walking /scratch is false about the
    process, not merely conservative about its cost."""
    bounded_walk = _Proc(["find", "/", "-xdev", "-name", "x"], "/var/tmp")
    hits, _unresolved, err = reaper.traversal_roots(bounded_walk, mounts, policy)
    assert err is None
    assert hits == [], "named a filesystem the walk cannot reach: %s" % (hits,)

    # The other reason is untouched: this one really does start on the mount.
    on_mount = _Proc(["find", "/scratch", "-xdev", "-name", "x"], "/var/tmp")
    hits, _unresolved, _err = reaper.traversal_roots(on_mount, mounts, policy)
    assert [h[1] for h in hits] == ["/scratch"]
    assert hits[0][3] == "at_or_near_root"

    # And a depth bound still reports, which is the distinction being drawn.
    shallow = _Proc(["find", "/scratch", "-maxdepth", "1"], "/var/tmp")
    hits, _unresolved, _err = reaper.traversal_roots(shallow, mounts, policy)
    assert hits, "a depth-bounded walk of an expensive mount is still worth naming"


def test_layer2_applies_the_base_directory_change(mounts, policy):
    """The specific miss, kept as its own row so the message is legible when
    it breaks."""
    expensive = _Proc(["fd", "--base-directory", "/scratch", "pat"], "/var/tmp")
    hits, _unresolved, _err = reaper.traversal_roots(expensive, mounts, policy)
    assert hits, "the reaper must see a walk rooted at the base directory"
    assert hits[0][0] == "/scratch"

    # The inverse: a cheap base from an expensive cwd is not a finding.
    cheap = _Proc(["fd", "--base-directory", "/tmp/cheap", "pat"], "/scratch/x")
    assert reaper.traversal_roots(cheap, mounts, policy)[0] == []


def test_an_absolute_base_makes_a_relative_operand_reportable(mounts, policy):
    """`/proc/<pid>/cwd` is unreadable for another user's process, which is
    the NORMAL case for what this reaper watches -- so how an unknown cwd is
    handled decides most of its findings. `fd --base-directory /scratch pat
    sub` resolves to /scratch/sub with no cwd at all."""
    proc = _Proc(["fd", "--base-directory", "/scratch", "pat", "sub"], None)
    hits, unresolved, _err = reaper.traversal_roots(proc, mounts, policy)
    assert hits, "an absolute base resolves the operand without a cwd"
    assert hits[0][0] == "/scratch/sub"
    assert not unresolved


def test_a_genuinely_relative_root_is_still_unresolved(mounts, policy):
    """The inverse, and the reason the fix is a sentinel rather than a
    blanket "report it anyway": a path that is still relative AFTER the
    coordinate change is one that really did need the cwd we could not read,
    and guessing would put another user's directory in the audit log."""
    for argv in (["fd", "pat", "sub"], ["grep", "-r", "pat"]):
        hits, unresolved, _err = reaper.traversal_roots(_Proc(argv, None),
                                                        mounts, policy)
        assert hits == [], argv
        assert unresolved, argv


def test_a_cheap_base_with_no_cwd_is_not_a_finding(mounts, policy):
    """Resolvable and cheap: reported as neither a hit nor unresolved."""
    proc = _Proc(["fd", "--base-directory", "/tmp/cheap", "pat", "sub"], None)
    hits, unresolved, _err = reaper.traversal_roots(proc, mounts, policy)
    assert hits == []
    assert not unresolved


class _FullProc(_Proc):
    """Everything classify() reads, so a finding can actually be built."""

    def __init__(self, argv, cwd, pid=4321, ppid=1, age_s=99999.0,
                 cpu_s=500.0, state="D", uid=UID_A, starttime=77777):
        _Proc.__init__(self, argv, cwd)
        self.pid = pid
        self.ppid = ppid
        self.age_s = age_s
        self.cpu_s = cpu_s
        self.state = state
        self.uid = uid
        self.starttime = starttime
        self.leaf_cgroup = SEATED
        # The real `Proc.key` shape (`pid:starttime`): a record that
        # reconstructs `_finding_key` is only evidence if the double builds
        # its key the way the real one does.
        self.key = "%d:%s" % (pid, starttime)


def _with_broken_parser(fn):
    """Run fn() with the shared argv parser raising, and restore it after."""
    original = R.resolved_roots

    def explode(*_a, **_k):
        raise ValueError("parser bug")

    R.resolved_roots = explode
    try:
        return fn()
    finally:
        R.resolved_roots = original


def test_a_parse_failure_is_reported_not_swallowed(mounts, policy):
    """`except Exception: return ([], False)` answered "no expensive roots,
    nothing unresolved" for an internal failure -- a clean bill of health
    indistinguishable from a process that really is fine. This is the
    backstop layer, so it is the last place that should answer a question it
    did not manage to ask."""
    proc = _FullProc(["find", "/scratch", "-name", "x"], "/var/tmp")

    hits, unresolved, error = _with_broken_parser(
        lambda: reaper.traversal_roots(proc, mounts, policy))
    assert hits == []
    assert not unresolved
    assert error and "ValueError" in error, error

    # And a healthy call still reports no error, so the third state is not
    # simply always set.
    assert reaper.traversal_roots(proc, mounts, policy)[2] is None


def test_a_known_tool_that_will_not_parse_still_reaches_the_audit_trail(
        mounts, policy):
    """`hits` is empty on a parse failure, so the process fell past every arm
    of classify() and left NO record at all. It names a defect in THIS code
    rather than in the user's command, which is why it belongs in the trail
    meant to justify promoting to --kill."""
    proc = _FullProc(["find", "/scratch", "-name", "x"], "/var/tmp")

    findings = _with_broken_parser(
        lambda: reaper.classify({proc.pid: proc}, mounts, policy=policy))
    assert [f.verdict for f in findings] == ["unparsed_traversal"], findings
    assert "ValueError" in findings[0].detail["root_error"]

    entry = findings[0].record()
    assert entry["verdict"] == "unparsed_traversal"


def test_an_unknown_comm_that_will_not_parse_is_not_reported(mounts, policy):
    """The inverse. A tool this repo has no profile for is not something the
    parser was ever asked about, so a failure there is not a finding -- and
    emitting one would put every unrelated process in the trail."""
    proc = _FullProc(["rsync", "-a", "/scratch/", "/backup/"], "/var/tmp")
    findings = _with_broken_parser(
        lambda: reaper.classify({proc.pid: proc}, mounts, policy=policy))
    assert [f.verdict for f in findings] == ["opaque_traversal"], findings
    assert "root_error" not in findings[0].detail


def test_healthy_classification_is_unchanged(mounts, policy):
    """The regression guard: the ordinary verdicts still come out."""
    proc = _FullProc(["find", "/scratch", "-name", "x"], "/var/tmp")
    findings = reaper.classify({proc.pid: proc}, mounts, policy=policy)
    assert [f.verdict for f in findings] == ["orphan_traversal"], findings


def test_a_parse_failure_can_never_be_killed():
    """Killing because THIS code could not parse the argv would be acting on
    our own failure rather than on evidence about the process."""
    assert "unparsed_traversal" in reaper.NEVER_KILL
    # The verdicts that exist to name a defect or a doubt, not a culprit.
    for verdict in ("orphan_idle", "opaque_traversal", "unparsed_traversal"):
        assert verdict in reaper.NEVER_KILL, verdict
    for verdict in ("orphan_traversal", "runaway_traversal", "fanout_traversal"):
        assert verdict not in reaper.NEVER_KILL, verdict


# --------------------------------------------------------------------------
# PSI corroborates, it does not gate (ADR-0009)
# --------------------------------------------------------------------------

def _tiny_stall_usec(window_s, fraction):
    """PSI microseconds that come to `fraction` of a `window_s` window."""
    return window_s * 1e6 * fraction


def test_a_finding_below_the_psi_threshold_is_still_reported(
        tmp_path, procfs, mounts_path):
    """The regression this whole change exists for. A metadata walk of a
    parallel filesystem accrues almost no PSI io; the slice is seeded here
    far under the threshold. It must still be scanned, and the finding must
    still be named."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=_tiny_stall_usec(60.0, 0.000005))
    write_proc(procfs, 7913, "find",
               ["find", "/scratch/project/cube", "/scratch",
                "-maxdepth", "6", "-name", "*run*", "-mmin", "-20"],
               uid=UID_B, ppid=1, state="D", cpu_s=39600.0, age_s=3 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_ACTIONABLE

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert [e["verdict"] for e in entries] == ["orphan_traversal"], entries
    assert entries[0]["pid"] == 7913
    assert entries[0]["stalling_slice"] is False, (
        "the slice did NOT stall -- that is the point, and the record has to "
        "say so rather than the finding not existing")


def test_a_poll_with_no_slice_stalling_still_scans(
        tmp_path, procfs, mounts_path):
    """run() used to return early with "nothing scanned" whenever no slice
    crossed the threshold -- the ordinary state of almost every slice, so
    the early return was the reaper's main failure mode rather than an
    optimisation. Pinned so it cannot come back."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=0.0)  # perfectly quiet: zero stall
    write_proc(procfs, 4200, "find",
               ["find", "/scratch/x", "-type", "f", "-name", "y"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_ACTIONABLE

    entries = _read_audit(str(tmp_path / "audit.jsonl"))
    assert [e["verdict"] for e in entries] == ["orphan_traversal"], entries
    assert entries[0]["stalling_slice"] is False


def test_a_process_outside_every_stalling_slice_is_still_classified(
        tmp_path, procfs, mounts_path):
    """The narrowing, not just the early return: one slice stalls hard and a
    DIFFERENT user's traversal is the real finding."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)          # 100%: stalling
    write_slice(cg, UID_A, io_full_total=_tiny_stall_usec(60.0, 0.000005))
    write_proc(procfs, 4301, "grep",
               ["grep", "-rIn", "pat", "/scratch/loud"],
               uid=UID_B, ppid=900, state="D", cpu_s=500.0, age_s=4 * 86400)
    write_proc(procfs, 4302, "find",
               ["find", "/scratch/quiet", "-type", "f", "-name", "x"],
               uid=UID_A, ppid=1, state="D", cpu_s=39600.0, age_s=4 * 86400)
    _seed_state(tmp_path, uids=(str(UID_A), str(UID_B)))
    args = reaper.build_parser().parse_args([
        "--spool", str(tmp_path / "spool"),
        "--audit", str(tmp_path / "audit.jsonl"),
        "--cgroup-root", str(cg),
        "--proc-root", str(procfs),
        "--mounts", mounts_path,
        "--min-interval", "0",
    ])

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_ACTIONABLE

    by_pid = {e["pid"]: e for e in _read_audit(str(tmp_path / "audit.jsonl"))}
    assert set(by_pid) == {4301, 4302}, by_pid
    assert by_pid[4301]["stalling_slice"] is True
    assert by_pid[4302]["stalling_slice"] is False


def test_the_record_carries_the_psi_delta_alongside_the_stall_verdict(
        tmp_path, procfs, mounts_path):
    """Both halves of the corroboration, not just the boolean: a reader
    deciding --kill promotion needs the measured fraction as well as whether
    it crossed, because a hair under the threshold and zero are both False
    and are not the same evidence."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=30.0 * 1e6)  # 50% of a 60s window
    write_proc(procfs, 4401, "find",
               ["find", "/scratch/x", "-type", "f", "-name", "y"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_ACTIONABLE

    entry = _read_audit(str(tmp_path / "audit.jsonl"))[0]
    assert entry["stalling_slice"] is True
    assert entry["io_pressure_delta"] == pytest.approx(0.5, abs=0.01)


def test_the_cpu_threshold_also_marks_a_slice_stalling(
        tmp_path, procfs, mounts_path):
    """`stalling_slice` is either threshold: a CPU-bound recursive grep over
    a hot cache stalls the CPU controller and not the I/O one."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=0.0, cpu_some_total=60.0 * 1e6)
    write_proc(procfs, 4402, "grep", ["grep", "-rIn", "pat", "/scratch/x"],
               uid=UID_B, ppid=900, state="R", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_ACTIONABLE
    entry = _read_audit(str(tmp_path / "audit.jsonl"))[0]
    assert entry["stalling_slice"] is True
    assert entry["io_pressure_delta"] == 0.0


def test_kill_acts_on_a_finding_psi_did_not_corroborate(
        tmp_path, procfs, mounts_path):
    """A deliberate decision, not an oversight (ADR-0009). Making the
    threshold gate ACTION instead of OBSERVATION fails for the same measured
    reason: PSI cannot see a metadata walk, so the clearest runaway on the
    node would become permanently unkillable. Acting on the cgroup signal
    ALONE stays forbidden -- the /proc step still names the process -- it
    does not require the cgroup signal. `stalling_slice: false` on the
    record is what tells a human PSI disagreed."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=0.0)
    write_proc(procfs, 4501, "find",
               ["find", "/scratch/x", "-type", "f", "-name", "y"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _stalling_kill_args(tmp_path, cg, procfs, mounts_path)

    signalled = []
    reaper.run(args, sleep=lambda _s: None,
               killer=lambda pid, sig: signalled.append((pid, sig)),
               alive=lambda _pid: False)

    assert signalled and signalled[0][0] == 4501, signalled
    entry = _read_audit(str(tmp_path / "audit.jsonl"))[0]
    assert entry["action"] == "terminated"
    assert entry["stalling_slice"] is False


def test_classifying_a_whole_process_table_is_not_noisy(mounts, policy):
    """The cost objection the gate answered, measured rather than assumed:
    classify() over a large synthetic table of ordinary processes with two
    planted traversals must produce two findings and no others. The filter
    that keeps the trail readable is classify()'s own evidence, not the
    slice signal in front of it."""
    procs = {}
    for pid in range(1000, 7000):
        # The node's ordinary population: shells, python, node, daemons.
        # None of them is a traversal and none may become a finding.
        procs[pid] = _FullProc(["python3", "-u", "train.py"], "/home/u",
                               pid=pid, ppid=pid - 1, age_s=9e5, cpu_s=5e4,
                               state="S", uid=UID_A + (pid % 2))
    procs[9001] = _FullProc(
        ["find", "/scratch", "-type", "f", "-name", "x"], "/home/u",
        pid=9001, ppid=1, age_s=4 * 86400, cpu_s=73657.0, state="D", uid=UID_B)
    procs[9002] = _FullProc(
        ["grep", "-rIn", "pat", "/scratch"], "/home/u",
        pid=9002, ppid=900, age_s=4 * 86400, cpu_s=500.0, state="D", uid=UID_A)

    findings = reaper.classify(procs, mounts, policy=policy)

    assert sorted(f.proc.pid for f in findings) == [9001, 9002], [
        (f.verdict, f.proc.pid) for f in findings]


def test_a_kernel_thread_is_not_an_opaque_traversal(procfs, mounts_path):
    """A blocked `kworker` is an unknown tool, in D, past budget -- every
    term of the opaque_traversal arm. It never surfaced while PSI gated
    because kernel threads are uid 0 and there is no user-0 slice to cross a
    threshold. That was luck, not a decision, so the exclusion is on the
    merits: a kernel thread has an EMPTY /proc/<pid>/cmdline, and a record
    naming nothing is the opposite of the /proc step's purpose (ADR-0009)."""
    write_proc(procfs, 2433, "kworker/126:0-events", [],
               uid=0, ppid=2, state="D", cpu_s=120.0, age_s=6 * 3600)
    write_proc(procfs, 3130, "rsync",
               ["rsync", "-a", "/scratch/x", "/tmp/y"],
               uid=UID_B, ppid=800, state="D", cpu_s=99.0, age_s=6 * 3600)

    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))

    assert [(f.verdict, f.proc.pid) for f in findings] == [
        ("opaque_traversal", 3130)], [
        (f.verdict, f.proc.pid, f.proc.argv) for f in findings]


def test_a_uid_psi_never_measured_gets_no_stall_verdict_at_all(
        tmp_path, procfs, mounts_path):
    """The third state, and the one that matters for reading the trail.
    `stalling_slice: false` must mean "measured, and the slice was quiet". A
    uid with no differenced reading -- no slice of its own, or a slice first
    seen this poll -- was never measured at all. Writing False for it would
    let the field the --kill decision is read against report "no stall here"
    about a user nobody sampled. The key is omitted instead."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4601, "find",
               ["find", "/scratch/unmeasured", "-type", "f", "-name", "x"],
               uid=UID_A, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_ACTIONABLE

    entry = _read_audit(str(tmp_path / "audit.jsonl"))[0]
    assert entry["pid"] == 4601 and entry["verdict"] == "orphan_traversal"
    assert "stalling_slice" not in entry, entry
    assert "io_pressure_delta" not in entry, entry


def test_a_slice_first_seen_this_poll_is_also_unmeasured(
        tmp_path, procfs, mounts_path):
    """The other way a uid has no differenced reading: its slice exists now
    but was not in the previous sample, so its cumulative total is history
    and stalling_slices() skips it. Same third state on the record."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_slice(cg, UID_A, io_full_total=60.0 * 1e6)  # not in the seeded state
    write_proc(procfs, 4602, "find",
               ["find", "/scratch/new", "-type", "f", "-name", "x"],
               uid=UID_A, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path)

    assert reaper.run(args, sleep=lambda _s: None) == reaper.EXIT_ACTIONABLE
    entry = _read_audit(str(tmp_path / "audit.jsonl"))[0]
    assert "stalling_slice" not in entry, entry


def test_an_argv_of_one_empty_string_names_nothing_either(procfs, mounts_path):
    """The second half of the kernel-thread guard. `any(proc.argv)` excludes
    two shapes: a zero-byte cmdline reads back as `[]`; a cmdline of a single
    NUL -- a real process whose entire argv is one empty argument -- reads
    back as `[""]`. Neither names anything, and `bool(proc.argv)` is true
    for the second, so swapping `any` for `bool` would pass the sibling test
    and fail this one."""
    write_proc(procfs, 4701, "mystery", [""],
               uid=UID_B, ppid=800, state="D", cpu_s=99.0, age_s=6 * 3600)

    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))

    assert findings == [], [
        (f.verdict, f.proc.pid, f.proc.argv) for f in findings]


def test_a_tail_follower_is_not_an_opaque_traversal(procfs, mounts_path):
    """`tail -F` opens the fixed set of files named on its own command line
    and blocks; GNU tail has no recursion option at all, so no argv makes it
    a traversal (ADR-0010). Note what is NOT the reason: these followers may
    well be blocked on the expensive filesystem, and `on_expensive_mount` is
    False for every process that reaches this arm anyway. Gating on it would
    delete the verdict rather than narrow it. The rsync is the positive
    control: an unmodelled tool that really can walk a tree must still be
    counted, or this exclusion has eaten the verdict."""
    write_proc(procfs, 1850, "tail",
               ["tail", "-n0", "-F",
                "/home/someone/logs/keepalive.log",
                "/home/someone/logs/run.out"],
               uid=UID_B, ppid=1, state="D", cpu_s=36.7, age_s=94532.5)
    write_proc(procfs, 3130, "rsync",
               ["rsync", "-a", "/scratch/x", "/tmp/y"],
               uid=UID_B, ppid=800, state="D", cpu_s=99.0, age_s=6 * 3600)

    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))

    assert [(f.verdict, f.proc.pid) for f in findings] == [
        ("opaque_traversal", 3130)], [
        (f.verdict, f.proc.pid, f.proc.argv) for f in findings]


def test_the_stream_filter_exclusion_reads_argv_and_not_comm(procfs,
                                                             mounts_path):
    """Keyed on argv[0], deliberately, and the inverse row proves it. `comm`
    is set by whatever wrapper launched the process, so no identity decision
    here may rest on it. A process whose comm says `tail` while its argv says
    `rsync` is an unmodelled tool and must still be reported; the reverse,
    argv saying `tail`, is excluded. `exec -a tail` would suppress a record,
    which is tolerable only because this verdict is in NEVER_KILL."""
    write_proc(procfs, 5001, "tail",
               ["rsync", "-a", "/scratch/x", "/tmp/y"],
               uid=UID_B, ppid=800, state="D", cpu_s=99.0, age_s=6 * 3600)
    write_proc(procfs, 5002, "rsync",
               ["tail", "-F", "/home/someone/run.log"],
               uid=UID_B, ppid=800, state="D", cpu_s=99.0, age_s=6 * 3600)

    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))

    assert [(f.verdict, f.proc.pid) for f in findings] == [
        ("opaque_traversal", 5001)], [
        (f.verdict, f.proc.pid, f.proc.argv) for f in findings]


def test_a_stream_filter_reaches_no_other_verdict_either(procfs, mounts_path):
    """The exclusion means NO record, which is the decided disposition. Driven
    at both the orphaned and the live-parent shapes, since those are the two
    ways the other arms are entered."""
    write_proc(procfs, 6001, "tail", ["tail", "-F", "/scratch/run.log"],
               uid=UID_B, ppid=1, state="D", cpu_s=0.0, age_s=6 * 3600)
    write_proc(procfs, 6002, "tail", ["tail", "-F", "/scratch/run.log"],
               uid=UID_B, ppid=800, state="D", cpu_s=500.0, age_s=6 * 3600)

    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))

    assert findings == [], [
        (f.verdict, f.proc.pid, f.proc.argv) for f in findings]


def test_the_stream_filter_set_is_the_compiled_constant(procfs, mounts_path):
    """A site's additions to `[reaper].stream_filters` take effect through
    the stamped tuple and nowhere else: with the set emptied, the same
    `tail -F` is an unmodelled tool in D past budget and IS recorded."""
    write_proc(procfs, 6003, "tail", ["tail", "-F", "/scratch/run.log"],
               uid=UID_B, ppid=800, state="D", cpu_s=500.0, age_s=6 * 3600)
    saved = reaper.STREAM_FILTERS
    try:
        reaper.STREAM_FILTERS = ()
        findings = reaper.classify(scan(procfs), read_mounts(mounts_path))
    finally:
        reaper.STREAM_FILTERS = saved
    assert [(f.verdict, f.proc.pid) for f in findings] == [("opaque_traversal", 6003)]


def test_stream_followers_leave_no_record_beside_one_runaway(
        procfs, mounts_path):
    """The shape of a first weekend's trail at a reference deployment,
    synthesised: ten `tail -n0 -F` log-followers on homes on the expensive
    filesystem, orphaned and wedged in D past budget, beside ONE unmodelled
    tool in the same state that really can walk a tree. The trail must be the
    one line it always contained, asserted as one exact list rather than as
    two independent properties -- a test that checks only the parts someone
    thought to name is how the noise came back under a different verdict."""
    for i in range(10):
        write_proc(procfs, 1800 + i, "tail",
                   ["tail", "-n0", "-F",
                    "/home/someone/logs/keepalive.log",
                    "/home/someone/logs/watch-%d.log" % i],
                   uid=UID_B, ppid=1, state="D", cpu_s=36.7, age_s=94532.5)
    write_proc(procfs, 1900, "rsync",
               ["rsync", "-a", "/scratch/dataset/", "/home/someone/copy/"],
               uid=UID_B, ppid=1, state="D", cpu_s=8376.0, age_s=379674.0)

    findings = reaper.classify(scan(procfs), read_mounts(mounts_path))

    assert [(f.verdict, f.proc.pid) for f in findings] == [
        ("opaque_traversal", 1900)], [
        (f.verdict, f.proc.pid, f.proc.argv) for f in findings]


# --------------------------------------------------------------------------
# stdin sensing (ADR-0011)
# --------------------------------------------------------------------------

def test_read_proc_tells_a_terminal_from_a_pipe_from_an_unreadable_stdin(
        procfs):
    """Three states, and the third is not the second. /proc/<pid>/fd/0 sits
    behind the same PTRACE_MODE_READ gate as cwd, so on another user's
    process it raises and `stdin_tty` is None. Read by device number rather
    than by matching the link text."""
    master, slave = os.openpty()
    try:
        write_proc(procfs, 7001, "ugrep", ["ugrep", "pat"],
                   stdin=os.ttyname(slave))
        write_proc(procfs, 7002, "ugrep", ["ugrep", "pat"], stdin="/dev/null")
        write_proc(procfs, 7003, "ugrep", ["ugrep", "pat"])

        procs = scan(procfs)
    finally:
        os.close(master)
        os.close(slave)

    assert procs[7001].stdin_tty is True
    assert procs[7002].stdin_tty is False, "a character device is not a tty"
    assert procs[7003].stdin_tty is None, "unreadable is neither"


def test_a_piped_ugrep_with_no_operand_is_not_a_traversal(procfs, mounts_path):
    """Modelling ugrep makes a piped `ugrep --line-buffered -E ...` log
    filter a KNOWN tool; assuming a terminal, it would take the cwd fallback,
    land on the expensive mount and come out as `runaway_traversal` -- which
    is killable. ugrep really does recurse the cwd when stdin is a terminal,
    so the fallback itself is right; what was wrong was assuming the
    terminal. The tty half is the positive control: same argv, same cwd,
    opposite stdin, opposite verdict."""
    master, slave = os.openpty()
    try:
        write_proc(procfs, 7101, "ugrep",
                   ["ugrep", "--line-buffered", "-E", "pat"],
                   uid=UID_B, ppid=1, state="D", cpu_s=500.0, age_s=6 * 3600,
                   cwd="/home/someone", stdin="/dev/null")
        write_proc(procfs, 7102, "ugrep", ["ugrep", "pat"],
                   uid=UID_B, ppid=1, state="D", cpu_s=500.0, age_s=6 * 3600,
                   cwd="/home/someone", stdin=os.ttyname(slave))

        findings = reaper.classify(scan(procfs), read_mounts(mounts_path))
    finally:
        os.close(master)
        os.close(slave)

    assert [(f.verdict, f.proc.pid) for f in findings] == [
        ("orphan_traversal", 7102)], [
        (f.verdict, f.proc.pid, f.proc.argv) for f in findings]


def test_an_unreadable_stdin_charges_no_fallback_root(procfs, mounts_path):
    """Why `is True` and not `is not False`. An unprivileged scan cannot read
    another user's fd/0 OR cwd -- one ptrace gate, both files. Reading None
    as "terminal" would invent a walk of a directory this process could not
    even name. This is the hand-run, unprivileged shape."""
    write_proc(procfs, 7201, "ugrep", ["ugrep", "pat"],
               uid=UID_B, ppid=1, state="D", cpu_s=500.0, age_s=6 * 3600)

    procs = scan(procfs)
    assert procs[7201].stdin_tty is None
    assert procs[7201].cwd is None

    assert reaper.classify(procs, read_mounts(mounts_path)) == []


def test_a_closed_stdin_is_not_a_terminal_even_with_a_readable_cwd(
        procfs, mounts_path):
    """The shape that makes `is True` load-bearing rather than decorative.
    A process that simply CLOSED fd 0 has no /proc/<pid>/fd/0 at all --
    ENOENT, not EPERM -- while its cwd reads perfectly. A process with no
    stdin is not at a terminal, so ugrep reads a closed descriptor and walks
    nothing: `is True` charges no root; `is not False` reads None as a
    terminal and manufactures a finding about a walk that is not happening.
    This is the only test in the suite that distinguishes the two spellings
    (ADR-0011)."""
    # `cwd` is read with os.readlink, which returns the link TEXT -- the
    # target need not exist on this machine. One level below the mount,
    # deliberately: with unscoped_depth 2, /scratch/someone/runs would be
    # SCOPED and allowed, and the positive control would pass for the wrong
    # reason.
    on_mount = "/scratch/someone"
    write_proc(procfs, 8101, "ugrep", ["ugrep", "pat"],
               uid=UID_B, ppid=1, state="D", cpu_s=500.0, age_s=6 * 3600,
               cwd=on_mount)
    procs = scan(procfs)
    assert procs[8101].stdin_tty is None
    assert procs[8101].cwd == on_mount, "the cwd must be readable, or this proves nothing"

    assert reaper.classify(procs, read_mounts(mounts_path)) == [], (
        "a process with no stdin is not at a terminal; nothing is walked")

    # The positive control, same cwd, stdin a real terminal: the fallback IS
    # charged and the walk IS reported.
    master, slave = os.openpty()
    try:
        write_proc(procfs, 8102, "ugrep", ["ugrep", "pat"],
                   uid=UID_B, ppid=1, state="D", cpu_s=500.0, age_s=6 * 3600,
                   cwd=on_mount, stdin=os.ttyname(slave))
        findings = reaper.classify(scan(procfs), read_mounts(mounts_path))
    finally:
        os.close(master)
        os.close(slave)

    assert [(f.verdict, f.proc.pid) for f in findings] == [
        ("orphan_traversal", 8102)], [
        (f.verdict, f.proc.pid) for f in findings]


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def test_json_output_lists_the_records_written(tmp_path, procfs, mounts_path):
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=60.0 * 1e6)
    write_proc(procfs, 4801, "find",
               ["find", "/scratch/x", "-type", "f", "-name", "y"],
               uid=UID_B, ppid=1, state="D", cpu_s=73657.0, age_s=4 * 86400)
    args = _report_args(tmp_path, cg, procfs, mounts_path, extra=["--json"])
    out = io.StringIO()
    reaper.run(args, out=out, sleep=lambda _s: None)
    # The ranking table precedes the JSON document; the document starts at
    # the first `[`.
    text = out.getvalue()
    records = json.loads(text[text.index("["):])
    assert [(r["verdict"], r["pid"], r["layer"]) for r in records] == [
        ("orphan_traversal", 4801, "reaper")]


def test_a_quiet_poll_names_what_it_examined(tmp_path, procfs, mounts_path):
    """A quiet poll is evidence of a quiet node only if it says what it
    looked at -- the whole table, not a population narrowed by PSI."""
    cg = tmp_path / "cg"
    write_slice(cg, UID_B, io_full_total=0.0)
    write_proc(procfs, 5000, "bash", ["/bin/bash", "-l"],
               uid=UID_B, ppid=900, state="S", cpu_s=0.5, age_s=30.0)
    write_proc(procfs, 5001, "vim", ["vim", "notes.txt"],
               uid=UID_B, ppid=5000, state="S", cpu_s=0.5, age_s=30.0)
    args = _report_args(tmp_path, cg, procfs, mounts_path)
    out = io.StringIO()
    assert reaper.run(args, out=out, sleep=lambda _s: None) == reaper.EXIT_QUIET
    assert "2 process(es) across 1 user slice(s), 0 of them stalling" in out.getvalue()
    assert not os.path.exists(str(tmp_path / "audit.jsonl"))
