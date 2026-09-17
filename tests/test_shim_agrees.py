"""The generated shim, asserted against the rule table and on its own.

Every row of `argv_cases.CASES` is judged here by the rendered `guard.sh`
under the node's shell and compared with `search_rules.check()`; a
disagreement outranks whatever parsing question sits underneath it. The rest
of the file pins what the matrix cannot: every branch of `classify_mount()`
in both consumers (ADR-0016), the refusal text, the audited escape hatch, the
two mount readers, the fork-free fast path and the renderer's own contract
with its templates.

Nothing here writes to the journal. The fixture site points the logger at a
path that does not exist; see conftest.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

import conftest
from walk_blocker import __version__, search_rules as R
from walk_blocker.render import shim as render
from argv_cases import CASES, FAST_CWD, FAST_DEEP
from conftest import ROOT, SHIM_SH, make_policy, resolve_cwd, run_shim


@pytest.fixture(autouse=True)
def _fixture_home(fixture_home):
    """Every test here judges paths under the fixture `/home`."""


def _mount_table(path, rows):
    with open(str(path), "w") as fh:
        fh.write("/dev/sda1 / ext4 rw,relatime 0 0\n")
        for row in rows:
            fh.write(row + "\n")
    return str(path)


# --------------------------------------------------------------------------
# the matrix
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case_id,argv,cwd,expected,note",
                         CASES, ids=[c[0] for c in CASES])
def test_shim_agrees(case_id, argv, cwd, expected, note, shim_env, mounts, policy, node_fs):
    """The rendered shim's verdict, which must be the table's."""
    result = run_shim(shim_env, argv, cwd=cwd)
    refused = result.returncode == R.EXIT_REFUSED
    assert refused is expected, "%s: %s\nstderr: %s" % (
        case_id, note, result.stderr.decode())
    table = R.check(argv, resolve_cwd(cwd, node_fs), mounts, policy) is not None
    assert table is refused, "%s: table=%s shim=%s" % (case_id, table, refused)
    if not refused:
        assert result.returncode == 0, result.stderr.decode()
        assert b"RAN" in result.stdout, "guard did not exec the real binary"


def test_the_shim_tests_run_under_the_shell_the_node_uses():
    """Makes the shell the shim is driven under visible rather than assumed.
    A suite that quietly tests the wrong shell is worse than one that
    reports which shell it tested (ADR-0015)."""
    if not shutil.which("dash"):
        pytest.skip(
            "dash is not installed, so the shim is being exercised under %r "
            "and not the /bin/sh a node typically runs. Install it to close "
            "the gap." % SHIM_SH)
    assert SHIM_SH == shutil.which("dash"), (
        "dash is installed but the shim harness is using %r" % SHIM_SH)


# --------------------------------------------------------------------------
# classify_mount, both consumers, every branch (ADR-0016)
# --------------------------------------------------------------------------

# (id, /proc/mounts row, mount point, expected class under CLASSIFY_POLICY)
CLASSIFY_ROWS = [
    ("listed-type", "fast /scratch wekafs rw,relatime 0 0", "/scratch", "expensive"),
    ("glob-type", "gw /gateway fuse.s3gw rw,relatime 0 0", "/gateway", "expensive"),
    ("remote-by-source", "srv:/export /remsrc newfs rw,relatime 0 0", "/remsrc", "expensive"),
    ("remote-by-netdev", "cluster /remnet newfs rw,_netdev,relatime 0 0", "/remnet", "expensive"),
    ("remote-by-addr", "cluster /remaddr newfs rw,relatime,addr=filer 0 0", "/remaddr", "expensive"),
    ("local-unknown", "tmpfs /run tmpfs rw,nosuid,nodev 0 0", "/run", "cheap"),
    ("local-unknown-lookalike-options",
     "/dev/sdb1 /data ext4 rw,my_netdev,x-addr=1 0 0", "/data", "cheap"),
    ("cheap-override-on-listed-type", "nas:/archive /archive nfs4 rw,relatime 0 0",
     "/archive", "cheap"),
    ("expensive-override-with-maxdepth", "/dev/sdc1 /bigdisk ext4 rw,relatime 0 0",
     "/bigdisk", "expensive"),
]

CLASSIFY_POLICY = make_policy(remote_fstypes=("wekafs", "nfs4", "fuse.*"),
                              mounts={"/home": ("expensive", 4),
                                      "/archive": ("cheap", None),
                                      "/bigdisk": ("expensive", 3)})


@pytest.fixture(scope="module")
def classify_env(tmp_path_factory, node_fs):
    base = tmp_path_factory.mktemp("classify")
    table = _mount_table(base / "mounts", [row for _id, row, _m, _c in CLASSIFY_ROWS])
    site = conftest.make_site(table)
    rendered = conftest.render_to(str(base), CLASSIFY_POLICY, site)
    env = conftest.build_shim_env(str(base / "farm"), rendered["guard"],
                                  conftest.wrapped_names_for(site), node_fs)
    env["mounts"] = table
    env["rendered"] = rendered
    return env


@pytest.mark.parametrize("case_id,row,mount,expected",
                         CLASSIFY_ROWS, ids=[c[0] for c in CLASSIFY_ROWS])
def test_table_and_shim_classify_every_branch_alike(case_id, row, mount, expected, classify_env):
    """An unbounded `find <mount> -name x` is refused exactly when the mount
    is expensive -- in the table, and in the rendered shim compiled from the
    same policy. Branch by branch, as ADR-0016 requires."""
    source, _point, fstype, options = row.split()[:4]
    assert R.classify_mount((source, mount, fstype, options), CLASSIFY_POLICY) == expected
    table = R.read_mounts(classify_env["mounts"], CLASSIFY_POLICY)
    argv = ["find", mount, "-name", "x"]
    table_refuses = R.check(argv, "/var/tmp", table, CLASSIFY_POLICY) is not None
    result = run_shim(classify_env, argv)
    shim_refuses = result.returncode == R.EXIT_REFUSED
    assert table_refuses is (expected == "expensive"), case_id
    assert shim_refuses is table_refuses, "%s: table=%s shim=%s\n%s" % (
        case_id, table_refuses, shim_refuses, result.stderr.decode())
    if shim_refuses:
        assert "(%s)" % fstype in result.stderr.decode()


def test_the_sh_reader_classifies_every_branch_like_the_awk_one(classify_env, tmp_path):
    """The awk reader answers on a host that has awk, which is every host the
    suite runs on; the sh reader answers where it is missing. Both carry the
    tiers inline, so both are driven over the same rows."""
    text = classify_env["rendered"]["guard_text"]
    marker = "\nSG_AWK="
    start = text.index(marker) + 1
    end = text.index("\n", start)
    text = text[:start] + "SG_AWK='/nonexistent/awk'" + text[end:]
    guard = tmp_path / "guard-sh-reader.sh"
    guard.write_text(text)
    guard.chmod(0o755)
    env = conftest.build_shim_env(str(tmp_path / "farm"), str(guard),
                                  conftest.wrapped_names_for(classify_env["rendered"]["site"]),
                                  classify_env["node_fs"])
    for case_id, _row, mount, expected in CLASSIFY_ROWS:
        result = run_shim(env, ["find", mount, "-name", "x"])
        assert (result.returncode == R.EXIT_REFUSED) is (expected == "expensive"), (
            case_id, result.stderr.decode())


def test_an_expensive_override_with_maxdepth_feeds_the_allowance_in_both_consumers(classify_env):
    """`/bigdisk` is a local ext4 made expensive by override, with its own
    ceiling of 3: a bound of 3 is allowed there and nowhere else, and 4 is
    refused everywhere."""
    table = R.read_mounts(classify_env["mounts"], CLASSIFY_POLICY)
    for argv, expected in (
        (["find", "/bigdisk", "-maxdepth", "3"], False),
        (["find", "/bigdisk", "-maxdepth", "4"], True),
        (["find", "/scratch", "-maxdepth", "3"], True),
        (["find", "/home", "-maxdepth", "4"], False),
        (["find", "/gateway", "-maxdepth", "3"], True),
    ):
        assert (R.check(argv, "/", table, CLASSIFY_POLICY) is not None) is expected, argv
        result = run_shim(classify_env, argv)
        assert (result.returncode == R.EXIT_REFUSED) is expected, (argv, result.stderr.decode())
    stderr = run_shim(classify_env, ["find", "/bigdisk", "-name", "x"]).stderr.decode()
    assert "Or bound the walk: -maxdepth 3 ..." in stderr


def test_remote_proxy_off_makes_the_source_and_option_tests_inert_in_the_shim(
        make_shim_env, tmp_path):
    strict = make_policy(remote_proxy=False)
    table = _mount_table(tmp_path / "mounts", [
        "srv:/export /remsrc newfs rw,_netdev,addr=filer 0 0",
        "srv:/export /listed nfs4 rw 0 0",
    ])
    env = make_shim_env(policy=strict, mount_table=table)
    assert run_shim(env, ["find", "/remsrc", "-name", "x"]).returncode == 0
    assert run_shim(env, ["find", "/listed", "-name", "x"]).returncode == R.EXIT_REFUSED


def test_the_refusal_ranks_an_unlisted_remote_type_after_every_listed_one(
        make_shim_env, tmp_path):
    """A walk from `/` that descends into a listed type and into a remote
    unknown names the listed one: severity is SG_FSTYPES order, and a type
    no glob matches ranks last, as `_severity()` says."""
    table = _mount_table(tmp_path / "mounts", [
        "srv:/x /aaa newfs rw,_netdev 0 0",
        "nas:/archive /zzz nfs4 rw 0 0",
    ])
    env = make_shim_env(mount_table=table)
    stderr = run_shim(env, ["find", "/", "-name", "x"]).stderr.decode()
    assert "would walk /zzz (nfs4)" in stderr
    hit = R.check(["find", "/", "-name", "x"], "/var/tmp",
                  R.read_mounts(table, env["rendered"]["policy"]), env["rendered"]["policy"])
    assert hit.mount == "/zzz"


# --------------------------------------------------------------------------
# the glob -> ERE twins
# --------------------------------------------------------------------------

@pytest.mark.parametrize("glob,fstype,matches", [
    ("fuse.*", "fuse.sshfs", True),
    ("fuse.*", "fusectl", False),
    ("nfs?", "nfs4", True),
    ("nfs?", "nfs", False),
    ("nfs", "nfs4", False),
    ("smb[23]", "smb3", True),
    ("smb[!23]", "smb3", False),
    ("9p+", "9p+", True),
    ("9p+", "9pp", False),
])
def test_glob_to_ere_matches_what_fnmatch_matches(glob, fstype, matches):
    import fnmatch
    assert fnmatch.fnmatchcase(fstype, glob) is matches
    assert bool(re.match(render.glob_to_ere(glob), fstype)) is matches
    assert render.glob_to_ere(glob).startswith("^") and render.glob_to_ere(glob).endswith("$")


def test_glob_to_ere_spells_the_example_the_way_the_design_says():
    assert render.glob_to_ere("fuse.*") == r"^fuse\..*$"


def test_the_shims_glob_to_ere_agrees_with_the_renderers(shim_env):
    """The sh twin, driven directly: source the rendered shim's function
    out of the file and convert the same globs. Sourcing the whole shim
    would exec a tool, so the function body is cut out and run alone."""
    text = open(os.path.join(shim_env["shim_dir"], "find")).read()
    start = text.index("sg_glob_to_ere() {")
    end = text.index("\n}\n", start) + 3
    body = text[start:end]
    for glob in ("fuse.*", "nfs?", "smb[!23]", "9p+", "a.b", "x|y", "(z)", "c^d$", "w{1}", "b\\s"):
        script = body + "\nsg_glob_to_ere '%s'\nprintf '%%s' \"$sg_ere\"\n" % glob.replace("'", "'\\''")
        got = subprocess.run([SHIM_SH, "-c", script], capture_output=True, text=True)
        assert got.returncode == 0, got.stderr
        assert got.stdout == render.glob_to_ere(glob), glob


def test_a_fstypes_seam_with_a_glob_reaches_the_awk_reader(shim_variant, tmp_path):
    """WALK_BLOCKER_FSTYPES set to a glob the compiled list lacks: the sh
    side converts it, the awk reader matches on it, and the verdict changes
    -- proving the runtime conversion is wired through and not just the
    compiled ERE."""
    table = _mount_table(tmp_path / "mounts", ["gw /gateway fuse.s3gw rw 0 0"])
    variant = shim_variant()
    env = {"WALK_BLOCKER_MOUNTS": table}
    assert run_shim(variant, ["find", "/gateway", "-name", "x"], env=env).returncode == 0
    env["WALK_BLOCKER_FSTYPES"] = "wekafs:fuse.*"
    assert run_shim(variant, ["find", "/gateway", "-name", "x"], env=env).returncode == R.EXIT_REFUSED


# --------------------------------------------------------------------------
# the refusal text
# --------------------------------------------------------------------------

def test_refusal_exit_code_is_not_greps_no_match(shim_env):
    """Exit 2, never 1. grep uses 1 for 'no match' and a caller must never
    read a block as an empty result."""
    result = run_shim(shim_env, ["grep", "-r", "pat", "/scratch"])
    assert result.returncode == R.EXIT_REFUSED == 2
    assert result.returncode != 1


def test_refusal_names_tool_root_mount_and_filesystem(shim_env):
    stderr = run_shim(shim_env, ["find", "/scratch/sub", "-name", "x"], cwd="/var/tmp").stderr.decode()
    assert stderr.startswith("REFUSED: find /scratch/sub would walk /scratch (wekafs) unbounded")
    assert "starts at or just inside /scratch" in stderr


def test_refusal_names_both_sacct_and_scontrol_for_a_scheduler_pattern(shim_env):
    """The error has to tell the caller what to do instead. Both tools are
    named because they fail in opposite directions: sacct is durable and
    scoped to what the caller's accounting covers; scontrol ignores
    accounting and forgets the job MinJobAge after it ends. Each catch
    travels with its tool, and the configured extra tool gets its line."""
    result = run_shim(
        shim_env,
        ["find", "/", "/home", "-type", "f", "-name", "slurm-4242_*.out", "-print"])
    stderr = result.stderr.decode()
    assert result.returncode == 2
    assert "sacct -j 4242" in stderr
    assert "scontrol show job 4242" in stderr
    assert "StdOut" in stderr
    assert "lookup-job 4242" in stderr
    assert "your account may see" in stderr
    assert "for 300s after the job ends" in stderr  # [slurm].min_job_age_s
    assert "%j unexpanded" in stderr
    assert "resolved path" in stderr


def test_the_extra_job_tool_line_is_configuration(make_shim_env):
    argv = ["find", "/scratch", "-name", "slurm-4242_*.out"]
    none = make_shim_env(**{"slurm.extra_job_tools": []})
    assert "4242" in run_shim(none, argv).stderr.decode()
    assert "lookup-job" not in run_shim(none, argv).stderr.decode()
    two = make_shim_env(**{"slurm.extra_job_tools": ["job-report", "job-where"],
                           "slurm.min_job_age_s": 60})
    stderr = run_shim(two, argv).stderr.decode()
    assert "    job-report 4242\n    job-where 4242\n" in stderr
    assert "for 60s after the job ends" in stderr


def test_refusal_ends_with_the_version_and_the_optional_site_lines(shim_env, make_shim_env):
    stderr = run_shim(shim_env, ["find", "/scratch", "-name", "x"]).stderr.decode()
    assert stderr.rstrip().endswith("walk-blocker %s" % __version__)
    assert "Documentation: https://docs.example.org/hpc/walk-blocker" in stderr
    assert "Contact: hpc-help@example.org" in stderr
    bare_site = conftest.make_site("/proc/mounts")
    del bare_site["site"]["docs_url"]
    del bare_site["site"]["contact"]
    text = render.render_shim(make_policy(), bare_site, __version__)
    assert "Documentation:" not in text and "Contact:" not in text
    assert "walk-blocker %s" % __version__ not in text  # the version travels in SG_VERSION
    assert "SG_VERSION='%s'" % __version__ in text


def test_refusal_offers_walk_job_for_every_refusal(shim_env):
    """The general alternative: the only one that answers a question about a
    whole filesystem, since there is no index of it and cannot be one
    (ADR-0007)."""
    for argv in (
        ["find", "/scratch", "-name", "x"],
        ["grep", "-r", "pat", "/scratch"],
        ["du", "-sh", "/scratch"],
        ["rg", "pat", "/home"],
        ["tree", "/archive"],
    ):
        stderr = run_shim(shim_env, argv).stderr.decode()
        assert "walk-job -- %s ..." % argv[0] in stderr, argv
        assert "walk-job -h" in stderr, argv


def test_refusal_names_the_tools_own_depth_flag_not_the_supplied_value(shim_env, policy):
    """The supplied value is deliberately looser than the ceiling, so a
    message echoing it back is distinguishable from one naming the flag."""
    for tool, argv, flag in (
        ("find", ["find", "/scratch", "-maxdepth", "7"], "-maxdepth"),
        ("rg", ["rg", "--max-depth", "7", "PAT", "/scratch"], "--max-depth"),
        ("tree", ["tree", "-L", "7", "/scratch"], "-L"),
    ):
        stderr = run_shim(shim_env, argv).stderr.decode()
        assert "Or bound the walk: %s %d ..." % (flag, policy.maxdepth_allowed) in stderr, tool
        assert "has no depth flag" not in stderr, tool


def test_the_refusal_advises_the_ceiling_that_actually_applies(shim_env, policy):
    """And not the global one, on a mount whose allowance is looser. The
    advised command is then RUN and has to be allowed: an advisory layer
    whose advice is refused teaches people to route around it."""
    a = policy.mounts["/home"][1]
    g = policy.maxdepth_allowed
    for argv, flag, want, advised in (
        (["find", "/home", "-name", "x"], "-maxdepth", a,
         ["find", "/home", "-maxdepth", str(a), "-name", "x"]),
        (["tree", "/home"], "-L", a, ["tree", "-L", str(a), "/home"]),
        (["rg", "PAT", "/home"], "--max-depth", a,
         ["rg", "--max-depth", str(a), "PAT", "/home"]),
        (["find", "/scratch", "-name", "x"], "-maxdepth", g,
         ["find", "/scratch", "-maxdepth", str(g), "-name", "x"]),
        # A descending walk takes the STRICTEST mount it enters.
        (["find", "/", "-name", "x"], "-maxdepth", g,
         ["find", "/", "-maxdepth", str(g), "-name", "x"]),
    ):
        stderr = run_shim(shim_env, argv).stderr.decode()
        assert "Or bound the walk: %s %d ..." % (flag, want) in stderr, argv
        assert b"RAN" in run_shim(shim_env, advised).stdout, advised


def test_du_is_refused_with_advice_that_fits_a_size_question(shim_env):
    for argv in (["du", "-sh", "/scratch"], ["du", "-d", "2", "/scratch"]):
        stderr = run_shim(shim_env, argv).stderr.decode()
        assert "prune the OUTPUT, not the walk" in stderr, argv
        assert "flags (-d --max-depth)" in stderr, argv
        assert "rg --max-depth" not in stderr, argv
        assert "-exec du" not in stderr, argv
        assert "walk-job -- du" in stderr, argv


def test_refusal_offers_a_different_tool_when_the_tool_has_no_bound(shim_env):
    stderr = run_shim(shim_env, ["grep", "-r", "pat", "/scratch"]).stderr.decode()
    assert "no depth flag" in stderr
    assert "rg --max-depth" in stderr


def test_refusal_does_not_recommend_locate(shim_env):
    for argv in (["find", "/scratch", "-name", "x"], ["du", "-sh", "/scratch"],
                 ["grep", "-r", "pat", "/scratch"], ["rg", "pat", "/home"]):
        assert "locate" not in run_shim(shim_env, argv).stderr.decode(), argv


def test_refusal_offers_the_device_bound_only_where_it_can_help(shim_env):
    descends = run_shim(shim_env, ["find", "/", "-name", "x"])
    assert descends.returncode == 2
    assert "-xdev" in descends.stderr.decode()
    on_mount = run_shim(shim_env, ["find", "/scratch", "-name", "x"])
    assert on_mount.returncode == 2
    assert "-xdev" not in on_mount.stderr.decode()
    grep = run_shim(shim_env, ["grep", "-r", "pat", "/"])
    assert grep.returncode == 2
    assert "keep the walk on the filesystem" not in grep.stderr.decode()


def test_refusal_names_the_most_severe_mount_a_descending_walk_enters(shim_env):
    stderr = run_shim(shim_env, ["find", "/", "-name", "x"]).stderr.decode()
    assert "would walk /home (wekafs)" in stderr or "would walk /scratch (wekafs)" in stderr
    assert "(nfs4)" not in stderr.splitlines()[0]


def test_refusal_admits_it_is_bypassable_and_names_the_sink(shim_env):
    """ADR-0001: a guardrail described as a sandbox invites reliance it
    cannot support. And if it claims overrides are recorded, it names where."""
    stderr = run_shim(shim_env, ["find", "/scratch", "-name", "x"]).stderr.decode()
    assert "advisory" in stderr
    assert "/usr/bin/find" in stderr
    assert R.ESCAPE_HATCH in stderr
    assert "recorded" in stderr
    assert "journalctl -t walk-blocker" in stderr
    assert "-o json" in stderr
    assert "_UID" in stderr


def test_refusal_has_no_placeholder_and_no_predecessor_name(shim_env):
    stderr = run_shim(shim_env, ["find", "/scratch", "-name", "x"]).stderr.decode()
    assert "@@" not in stderr
    assert "searchguard" not in stderr


# --------------------------------------------------------------------------
# depth handling the matrix cannot express
# --------------------------------------------------------------------------

def test_shim_honours_a_per_mount_depth_allowance(shim_env):
    """The compiled `/home` override at 4, from the fixture policy."""
    assert run_shim(shim_env, ["find", "/home/me", "-maxdepth", "4"]).returncode == 0
    assert run_shim(shim_env, ["tree", "-L", "4", "/home/me"]).returncode == 0
    assert run_shim(shim_env, ["find", "/home/me", "-maxdepth", "5"]).returncode == 2
    assert run_shim(shim_env, ["find", "/scratch", "-maxdepth", "4"]).returncode == 2
    assert run_shim(shim_env, ["find", "/home/me"]).returncode == 2


def test_shim_allows_a_depth_flag_with_nothing_left_in_argv(shim_env):
    result = run_shim(shim_env, ["find", "/scratch", "-maxdepth"])
    assert result.returncode == 0, result.stderr.decode()
    assert b"RAN" in result.stdout


def test_a_whitespace_padded_depth_agrees_with_the_table(shim_env, mounts, policy):
    argv = ["find", "/scratch", "-maxdepth", " 10", "-name", "x"]
    result = run_shim(shim_env, argv)
    assert result.returncode == 0, result.stderr.decode()
    assert R.check(argv, "/", mounts, policy) is None


def test_shim_and_table_agree_under_a_populated_depth_map(shim_env, mounts, policy, monkeypatch):
    """The seam on both sides: the table through Policy.from_env, the shim
    through the same variable."""
    seam = "/home=4:/archive=6"
    env = {R.DEPTH_BY_MOUNT_SEAM: seam}
    monkeypatch.setenv(R.DEPTH_BY_MOUNT_SEAM, seam)
    monkeypatch.delenv(R.FSTYPES_SEAM, raising=False)
    live = R.Policy.from_env(policy)
    cases = (
        [["find", "/home/me", "-maxdepth", d] for d in ("1", "2", "3", "4", "5", "9")]
        + [["find", "/archive", "-maxdepth", d] for d in ("2", "6", "7")]
        + [["find", "/scratch", "-maxdepth", d] for d in ("2", "4", "6")]
        + [["find", "/", "-maxdepth", d] for d in ("2", "3", "4", "6", "7")]
        + [["du", "-d", "4", "/home/me"], ["rg", "--max-depth", "4", "PAT", "/home/me"],
           ["tree", "-L", "4", "/home/me"], ["find", "/home/me"],
           ["find", "/var/log", "-maxdepth", "9"], ["find", "/", "-xdev", "-maxdepth", "9"]]
    )
    for argv in cases:
        table_refuses = R.check(argv, "/", mounts, live) is not None
        shim_refuses = run_shim(shim_env, argv, env=env).returncode == 2
        assert table_refuses == shim_refuses, argv


def test_the_descending_verdict_does_not_depend_on_mount_order(tmp_path, shim_env, policy):
    """Two mounts of one type tie for a walk from `/`; verdict AND named
    mount are pinned in both orders against both consumers."""
    tail = "nas:/archive /archive nfs4 rw 0 0\n"
    orders = {
        "scratch-first": "fast /scratch wekafs rw 0 0\nfast /home wekafs rw 0 0\n",
        "home-first": "fast /home wekafs rw 0 0\nfast /scratch wekafs rw 0 0\n",
    }
    seen = {}
    for label, body in orders.items():
        path = tmp_path / ("mounts-" + label)
        path.write_text("/dev/sda1 / ext4 rw 0 0\n" + body + tail)
        table = R.read_mounts(str(path), policy)
        for argv in (["find", "/", "-maxdepth", "4"], ["find", "/", "-maxdepth", "9"]):
            hit = R.check(argv, "/", table, policy)
            result = run_shim(shim_env, argv, env={"WALK_BLOCKER_MOUNTS": str(path)})
            stderr = result.stderr.decode()
            assert hit is not None, (label, argv)
            assert result.returncode == 2, (label, argv, stderr)
            named = stderr.split(" would walk ", 1)[1].split(" ", 1)[0]
            assert named == hit.mount, (label, argv)
            seen.setdefault(tuple(argv), set()).add(named)
    for argv, mounts_named in seen.items():
        assert len(mounts_named) == 1, (argv, mounts_named)


# --------------------------------------------------------------------------
# the escape hatch
# --------------------------------------------------------------------------

def test_escape_hatch_allows_and_is_audited(shim_env, tmp_path):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={R.ESCAPE_HATCH: "1", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert b"RAN" in result.stdout
    assert audit.exists(), "an unaudited escape hatch measures nothing"
    entry = json.loads(audit.read_text().strip())
    assert entry["action"] == "escape_hatch"
    assert entry["tool"] == "find"
    assert entry["mount"] == "/scratch"
    assert entry["fs"] == "wekafs"
    assert entry["reason"] == "at_or_near_root"
    assert entry["layer"] == "shim"
    assert entry["uid"] == os.getuid()


def test_escape_hatch_sink_does_not_resolve_through_the_callers_path(rendered_shim):
    """A property of the generated code, asserted there: a runtime test
    cannot shadow an absolute path."""
    shim = rendered_shim["guard_text"]
    site = rendered_shim["site"]
    block = shim[shim.index("sg_audit_emit() {"):]
    block = block[:block.index("\n}\n")]

    assert "SG_LOGGER='%s'" % site["trusted_binaries"]["logger"] in shim
    assert '"$SG_LOGGER" -t walk-blocker' in block
    assert block.index('[ -x "$SG_LOGGER" ]') < block.index("elif command -v logger")
    assert "SG_AWK='%s'" % site["trusted_binaries"]["awk"] in shim
    assert "SG_ID='%s'" % site["trusted_binaries"]["id"] in shim
    assert "${USER" not in block
    assert "SG_J_USER" not in block
    assert "SG_J_UID=$sg_uid" in block
    assert "SG_DATE='%s'" % site["trusted_binaries"]["date"] in shim
    assert '"$SG_DATE" -u ' in block
    assert "$(date " not in block and "=$(date" not in block


def test_escape_hatch_falls_back_to_a_path_resolved_logger(shim_env, logger_stub):
    result = run_shim(
        shim_env, ["find", "/scratch", "-name", "x"],
        env={R.ESCAPE_HATCH: "1",
             "PATH": "%s:%s:%s" % (shim_env["shim_dir"], logger_stub["bin"], shim_env["bin_dir"])})
    assert result.returncode == 0
    assert b"RAN" in result.stdout
    logged = logger_stub["log"].read_text()
    assert "-t walk-blocker" in logged, "the fallback never ran"
    assert '"action":"escape_hatch"' in logged
    assert '"mount":"/scratch"' in logged


def test_escape_hatch_still_allows_when_no_logger_exists_at_all(shim_env, tmp_path):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={R.ESCAPE_HATCH: "1", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    assert result.stderr == b"", result.stderr
    assert '"action":"escape_hatch"' in audit.read_text()


def test_escape_hatch_degrades_honestly_with_no_awk_to_escape_with(shim_variant, tmp_path):
    variant = shim_variant(SG_AWK="'/nonexistent/awk'")
    audit = tmp_path / "audit.jsonl"
    result = run_shim(variant, ["find", '/scratch/say"what', "-name", "x"],
                      env={R.ESCAPE_HATCH: "1", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    entry = json.loads(audit.read_text().strip())
    assert entry["action"] == "escape_hatch"
    assert "no awk" in entry["note"]
    assert "root" not in entry
    assert entry["tool"] == "find"


def test_escape_hatch_says_nothing_when_the_file_sink_is_unwritable(shim_env, tmp_path):
    if os.getuid() == 0:
        pytest.skip("root can write anywhere; the unwritable branch is unreachable")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    result = run_shim(shim_env, ["find", "/scratch", "-name", "x"],
                      env={R.ESCAPE_HATCH: "1", "WALK_BLOCKER_AUDIT": str(locked / "audit.jsonl")})
    assert result.returncode == 0
    assert b"RAN" in result.stdout
    assert result.stderr == b"", result.stderr


@pytest.mark.parametrize("hostile,label", [
    ('/scratch/say"what', "a double quote"),
    ("/scratch/back\\slash", "a backslash"),
    ("/scratch/two\nlines", "a newline, which would split the record"),
    ('/scratch/x","user":"someone-else', "a field-forging payload"),
    ("/scratch/bell\x07", "a raw control character"),
])
def test_escape_hatch_record_is_valid_json_for_a_hostile_root(shim_env, tmp_path, hostile, label):
    audit = tmp_path / "audit.jsonl"
    result = run_shim(shim_env, ["find", hostile, "-name", "x"],
                      env={R.ESCAPE_HATCH: "1", "WALK_BLOCKER_AUDIT": str(audit)})
    assert result.returncode == 0
    lines = [line for line in audit.read_text().splitlines() if line.strip()]
    assert len(lines) == 1, "%s split the record into %d lines" % (label, len(lines))
    entry = json.loads(lines[0])
    assert entry["action"] == "escape_hatch"
    assert entry["uid"] == os.getuid()
    assert entry["root"].startswith("/scratch/")


# --------------------------------------------------------------------------
# the two mount readers
# --------------------------------------------------------------------------

MOUNT_READER_CASES = [
    ("find-at-the-mount-point", ["find", "/scratch", "-name", "x"], True),
    ("find-descending-into-one", ["find", "/", "-name", "x"], True),
    ("find-on-a-cheap-tree", ["find", "/var/log", "-name", "x"], False),
    ("find-deep-enough-to-be-scoped", ["find", "/scratch/runs/set-01", "-name", "x"], False),
    ("device-bound-from-slash", ["find", "/", "-xdev", "-name", "x"], False),
    ("grep-recursive-on-a-home", ["grep", "-r", "pat", "/home"], True),
    ("home-allowance-honoured", ["find", "/home/me", "-maxdepth", "4"], False),
]


@pytest.mark.parametrize("case_id,argv,expected", MOUNT_READER_CASES,
                         ids=[c[0] for c in MOUNT_READER_CASES])
def test_the_sh_mount_reader_agrees_with_the_awk_one(shim_variant, case_id, argv, expected):
    """awk reads the table because dash's `read` is one syscall per byte; the
    sh loop stays for a host without awk, and the two must agree."""
    variant = shim_variant(SG_AWK="'/nonexistent/awk'")
    result = run_shim(variant, argv)
    assert (result.returncode == R.EXIT_REFUSED) is expected, (case_id, result.stderr.decode())


def test_both_mount_readers_skip_a_malformed_short_line(shim_variant, tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda1 / ext4 rw,relatime 0 0\n"
                      "fast /scratch wekafs rw,relatime 0 0\n"
                      "broken /threefield ext4\n"
                      "broken /twofield\n")
    for awk in (None, "'/nonexistent/awk'"):
        variant = shim_variant(**({"SG_AWK": awk} if awk else {}))
        env = {"WALK_BLOCKER_MOUNTS": str(mounts)}
        for short in ("/twofield", "/threefield"):
            result = run_shim(variant, ["find", short, "-name", "x"], env=env)
            assert result.returncode == 0, (awk, short, result.stderr.decode())
        result = run_shim(variant, ["find", "/scratch", "-name", "x"], env=env)
        assert result.returncode == R.EXIT_REFUSED, (awk, result.stderr.decode())


def test_the_shim_ignores_a_whitespace_escaped_mount_and_the_table_does_not(
        shim_env, tmp_path, policy):
    """A DOCUMENTED divergence, pinned so it stays documented. /proc/mounts
    octal-escapes whitespace in a mount point; sh cannot unescape it without
    a fork, so the shim skips such a mount and the table keeps it. Layer 1
    allows, Layer 2 reports -- the safe direction."""
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda1 / ext4 rw,relatime 0 0\n"
                      "fast /data/my\\040dir wekafs rw,relatime 0 0\n")
    result = run_shim(shim_env, ["find", "/data/my dir", "-name", "x"],
                      env={"WALK_BLOCKER_MOUNTS": str(mounts)})
    assert result.returncode == 0, "the shim skips a mount it cannot unescape"
    table = R.read_mounts(str(mounts), policy)
    assert [row[0] for row in table] == ["/data/my dir"]
    assert R.check(["find", "/data/my dir", "-name", "x"], "/var/tmp", table, policy) is not None


def test_a_failing_awk_falls_back_rather_than_reading_as_no_mounts(rendered_shim):
    """awk that RUNS AND FAILS returns an empty string, and an empty
    expensive-mount list means every walk is allowed. Asserted on the
    generated code: the awk branch returns only on success and clears the
    variable before the sh reader runs."""
    shim = rendered_shim["guard_text"]
    start = shim.index("sg_load_mounts() {")
    block = shim[start:shim.index("\n}\n", start)]
    assert "&& return 0" in block
    awk_call = block.index('"$SG_AWK"')
    guard = block.index("&& return 0", awk_call)
    reset = block.index("sg_expensive=''", guard)
    assert reset > guard
    assert "while read -r sg_dev sg_mnt sg_fs sg_opts sg_rest" in block
    # ...and the lists reach awk through the environment, never `-v`, which
    # would process the `\.` in the ERE list as an escape sequence.
    assert "-v " not in block[awk_call - 200:awk_call + 200]
    assert 'ENVIRON["SG_R_RELIST"]' in block


def test_the_mount_reader_opens_only_the_mount_table(rendered_shim):
    """Before sg_exec_real the shim reads one file. Every redirection-from and
    every file argument in the pre-exec regions names $SG_MOUNTS (ADR-0015)."""
    lines = rendered_shim["guard_text"].splitlines()
    end = next(i for i, line in enumerate(lines) if line.startswith("# --- refuse"))
    opens = [line for line in lines[:end]
             if re.search(r"(?<![0-9&>])<\s*\"?\$", line) and not line.strip().startswith("#")]
    assert opens, "expected the sh reader's redirection"
    for line in opens:
        assert '"$SG_MOUNTS"' in line, line
    awk_files = [line for line in lines[:end] if "}' " in line and "2>/dev/null)" in line]
    for line in awk_files:
        assert '"$SG_MOUNTS"' in line, line


# --------------------------------------------------------------------------
# the fork-free regions
# --------------------------------------------------------------------------

def _strip_arithmetic(code):
    """`$(( ... ))` removed, balanced, so the fork scan never sees one --
    unless a real substitution is nested inside it, in which case the bytes
    are left alone and the line is flagged."""
    out = []
    i = 0
    while i < len(code):
        if code.startswith("$((", i):
            depth = 0
            j = i + 1
            while j < len(code):
                if code[j] == "(":
                    depth += 1
                elif code[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(code):
                out.append(code[i:])
                break
            inner = _strip_arithmetic(code[i + 3:j - 1])
            if "$(" in inner or "`" in inner:
                out.append(code[i:j + 1])
            i = j + 1
            continue
        out.append(code[i])
        i += 1
    return "".join(out)


# A `case` arm's pattern, which may carry `|` as alternation and, inside
# single quotes, any character at all. The character class stops at anything
# that could open a substitution, so `x=$(a | b)` is never mistaken for one.
_CASE_ARM = re.compile(r"^\s*(?:'[^']*'|[^()`$'])*\)")


def _fork_offences(line):
    """Why one shim line would fork, or () if it would not."""
    code = line.split("#", 1)[0] if not line.strip().startswith("#") else ""
    code = _strip_arithmetic(code)
    arm = _CASE_ARM.match(code)
    if arm:
        code = code[arm.end():]
    found = []
    if "$(" in code or "`" in code:
        found.append("command substitution")
    if "|" in re.sub(r"\|\|", "", code):
        found.append("pipeline")
    return tuple(found)


def _regions(shim):
    """The three pre-exec regions: the prologue, the helpers, the fast path.
    sg_resolve, sg_audit_emit and sg_exec_real sit between the first two and
    are outside on purpose -- the last two fork by design, after the decision."""
    def marker(prefix):
        return next(i for i, line in enumerate(shim) if line.startswith(prefix))
    prologue_end = marker("# --- resolve the real binary")
    helpers = marker("# --- fork-free helpers")
    helpers_end = marker("# --- end fork-free helpers")
    start = marker("# --- fast path")
    end = marker("# --- path handling")
    assert 0 < prologue_end < helpers < helpers_end < start < end
    assert helpers_end + 1 == start, "a gap opened between the helper region and the fast path"
    return ((0, prologue_end), (helpers, helpers_end), (start, end))


def test_the_fast_path_forks_nothing(rendered_shim):
    """The whole argument for `sh` over Python (ADR-0015): everything before
    `sg_exec_real` costs a parse and no processes. Asserted over three
    regions, because the pre-exec code is not contiguous: the prologue that
    reads the seams, the helpers the fast path calls, and the fast path."""
    shim = rendered_shim["guard_text"].splitlines()
    regions = _regions(shim)
    (p0, p1), (h0, h1), (f0, f1) = regions
    assert any(line.startswith("sg_glob_to_ere()") for line in shim[p0:p1])
    assert any(line.startswith("sg_depth_seam()") for line in shim[p0:p1])
    assert any(line.startswith("sg_take_digits()") for line in shim[h0:h1])
    assert any(line.startswith("# --- bounded?") for line in shim[f0:f1])
    for lo, hi in regions:
        assert not any(line.startswith("sg_exec_real()") for line in shim[lo:hi])
        assert not any(line.startswith("sg_audit_emit()") for line in shim[lo:hi])
    offenders = []
    for lo, hi in regions:
        for lineno, line in enumerate(shim[lo:hi], start=lo + 1):
            for why in _fork_offences(line):
                offenders.append((lineno, why, line.strip()))
    assert offenders == [], "nothing before sg_exec_real may fork; found %d: %s" % (
        len(offenders), offenders[:4])


def test_the_fast_path_fork_check_can_actually_fail():
    """A guard whose only evidence is that nothing triggered it is not
    tested: the same predicate, over lines that must and must not trip it."""
    assert _fork_offences("sg_x=$(date)") == ("command substitution",)
    assert _fork_offences("sg_x=`date`") == ("command substitution",)
    assert _fork_offences("printf x | cat") == ("pipeline",)
    assert _fork_offences('[ -n "$x" ] || return 1') == ()
    assert _fork_offences("    ''|*[!0-9]*) sg_v='' ;;") == ()
    assert _fork_offences("# a comment with $( and | in it") == ()
    assert _fork_offences("    foo) printf x | cat ;;") == ("pipeline",)
    assert _fork_offences("    sg_x=$(a | b)") == ("command substitution", "pipeline")
    assert _fork_offences("sg_n=$((sg_n + 1))") == ()
    assert _fork_offences("    --*) sg_rl_n=$((sg_rl_n + 1)) ;;") == ()
    assert _fork_offences("sg_m=$((a | b))") == ()
    assert _fork_offences("sg_n=$((sg_n + 1)); sg_d=$(date)") == ("command substitution",)
    assert _fork_offences("sg_x=$(( $(date) ))") == ("command substitution",)
    assert _fork_offences("sg_x=$(( $((1 + 2)) + 3 ))") == ()
    assert _fork_offences("sg_x=$((1+2") == ("command substitution",)
    # A quoted metacharacter in a case pattern is a pattern, not a pipe or a
    # substitution -- the glob converter's arm -- and the body is still read.
    assert _fork_offences("            '.'|'('|')'|'$'|'|'|'\\') sg_ere=\"$sg_ere\\\\$sg_ge_c\" ;;") == ()
    assert _fork_offences("            '('|'|') sg_x=$(date) ;;") == ("command substitution",)
    assert _fork_offences("            '|') printf x | cat ;;") == ("pipeline",)


def test_the_fork_check_would_catch_an_injected_fork(rendered_shim):
    """Inject a substitution into each region and confirm the scan sees it."""
    shim = rendered_shim["guard_text"].splitlines()
    for lo, hi in _regions(shim):
        target = next(i for i in range(lo + 1, hi) if shim[i].startswith("    ") and "=" in shim[i]
                      and not shim[i].strip().startswith("#"))
        mutated = list(shim)
        mutated[target] = "    sg_injected=$(date)"
        hits = [i for i in range(lo, hi) if _fork_offences(mutated[i])]
        assert hits == [target], (lo, hi, hits)


# --------------------------------------------------------------------------
# the same claim, from execution rather than from the text
# --------------------------------------------------------------------------
#
# The scan above reasons about generated shell. It cannot see a construct it
# was never taught to look for -- a builtin that forks under one shell and
# not another, a here-string, a `read` from a process substitution -- so the
# claim is also asserted the only way that cannot be fooled by wording: run
# the thing and count the clones. The two checks stay side by side because
# they fail on different mistakes. The text one runs everywhere and names the
# offending line; this one needs `strace` and ptrace permission, and names
# only that something forked.
#
# What it asserts is the design as written, not a slogan: the fast path
# creates NO process, and the mount read creates exactly the ONE
# `sg_load_mounts` documents -- the awk that parses the table in a single
# pass. A second process on that path, or any process on the fast path, is
# the finding. Measured here rather than assumed: the fork the mount read
# makes is real and visible in every trace this test takes.

# `execve` is here to find the boundary -- everything the shim does happens
# before it execs the real tool -- and to say WHICH process each clone
# became. `open`/`openat` are here so "reaches the mount helper" is a fact
# this test checks rather than a claim its name makes.
_TRACED_SYSCALLS = "clone,clone3,vfork,fork,execve,open,openat"
_FORKING = ("clone", "clone3", "vfork", "fork")

# `12345 openat(...)` when more than one process is traced, `openat(...)`
# when only one is -- which is the difference this test is measuring, so the
# parser cannot assume either.
_TRACE_LINE = re.compile(r"^(?:\d+\s+)?([a-z_0-9]+)\(")


def _shell_as_sh(tmp_path, shell):
    """`shell` reachable under the name `sh`.

    Through a symlink, not an argument: bash turns on POSIX mode from the
    BASENAME it was invoked as, so `bash script` and `sh script` are two
    different shells and only the second is the one a login node runs.
    """
    real = shutil.which(shell)
    if real is None:
        pytest.skip("%s is not installed, so the execution-based fork check "
                    "cannot run under it" % shell)
    directory = tmp_path / ("as-sh-" + shell)
    directory.mkdir()
    link = directory / "sh"
    os.symlink(real, str(link))
    return str(link)


def _trace_shim(tmp_path, shell, shim_env, argv, cwd, label, env=None):
    """The shim run under strace; returns (syscall lines, stdout).

    Skips -- once, loudly, with the reason -- when strace is absent or
    ptrace is not permitted, which is the ordinary state of a hardened
    container and not a reason to fail a suite. `env` is merged into the
    shim's environment the way run_shim() merges it.
    """
    strace = shutil.which("strace")
    if strace is None:
        pytest.skip("strace is not on PATH; the execution-based fork check "
                    "needs it (the text-based one still ran)")
    trace_path = tmp_path / ("trace-%s.txt" % label)
    command, environ, real_cwd = conftest.shim_invocation(
        shim_env, argv, cwd=cwd, env=env)
    try:
        proc = subprocess.run(
            [strace, "-f", "-e", "trace=" + _TRACED_SYSCALLS,
             "-o", str(trace_path), shell] + command,
            capture_output=True, env=environ, cwd=real_cwd, input=b"")
    except PermissionError as exc:
        pytest.skip("strace could not be run here (%s); the execution-based "
                    "fork check needs ptrace permission" % exc)
    stderr = proc.stderr.decode("utf-8", "replace")
    first_line = (stderr.strip().splitlines() or ["no output"])[0]
    text = trace_path.read_text(errors="replace") if trace_path.exists() else ""
    if "ptrace" in stderr.lower() or "execve(" not in text:
        pytest.skip("strace exited %d without a usable trace (%s); the "
                    "execution-based fork check needs ptrace permission"
                    % (proc.returncode, first_line))
    lines = []
    for line in text.splitlines():
        match = _TRACE_LINE.match(line)
        if match:
            lines.append((match.group(1), line))
    return lines, proc.stdout.decode("utf-8", "replace")


def _sg_awk(guard_text):
    """The trusted awk the shim was stamped with, unquoted."""
    return _sg_var(guard_text, "SG_AWK")


def _sg_var(guard_text, name):
    """One of the shim's stamped trusted-binary constants, unquoted."""
    value = re.search(r"^%s=(.*)$" % name, guard_text, re.M).group(1).strip()
    return value.strip("'\"")


_EXECVE_PROGRAM = re.compile(r'^(?:\d+\s+)?execve\("([^"]*)"')


def _programs_run(lines):
    """The programs that actually started, in order, after the shell itself.

    A failed execve is not a program: dash searches PATH by attempting
    execve in each directory and leaves an ENOENT line per miss, where bash
    stats first. The refusal test arranges its PATH so no miss occurs, and
    this filter is the second line of defence for a whole line; a miss
    strace splits across `<unfinished ...>` and `<... resumed>` would not be
    caught here, which is why the arrangement comes first.
    """
    started = [line for name, line in lines
               if name == "execve" and "= -1" not in line]
    return [_EXECVE_PROGRAM.match(line).group(1) for line in started][1:]


@pytest.mark.parametrize("shell", ("dash", "bash"))
@pytest.mark.parametrize("case", ("fast-path", "reaches-the-mount-table"))
def test_the_shim_creates_no_process_the_design_does_not_document(
        tmp_path, shim_env, rendered_shim, shell, case):
    """Clones before the real tool is exec'd, counted rather than argued.

    Two argvs, because they leave the shim by different doors: one decided
    in the fast path, which never looks at a mount and must create nothing
    at all, and one allowed only after the mount table has been read, which
    may create exactly the one process `sg_load_mounts` explains and no
    other. Both under dash and under bash-as-sh, because a builtin that
    forks in one shell and not the other is precisely what the text scan
    cannot see.
    """
    if case == "fast-path":
        # grep only traverses when told to, and it was not told to.
        argv = ["grep", "needle", "/tmp/haystack"]
    else:
        # Recursive, so the root has to be CLASSIFIED before it can be
        # allowed -- which is the mount table, read. A merely depth-bounded
        # argv would not do: the global ceiling settles that one without the
        # table ever being opened, which is what makes this row distinct.
        argv = ["grep", "-r", "needle", "/tmp/cheap"]

    lines, stdout = _trace_shim(
        tmp_path, _shell_as_sh(tmp_path, shell), shim_env, argv,
        cwd="/home/someone", label="%s-%s" % (shell, case))

    assert "RAN " in stdout, (
        "the run was refused, so it never reached the exec this test is "
        "about: %r" % stdout)

    # The real tool, not the second execve: on the mount-table path the
    # second execve is the awk, and a boundary that moves when the shim
    # changes would quietly stop asserting anything.
    real_tool = os.path.join(shim_env["bin_dir"], argv[0])
    before = []
    for name, line in lines:
        if name == "execve" and ('"%s"' % real_tool) in line:
            break
        before.append((name, line))
    else:
        raise AssertionError("no execve of %s in the trace: %s"
                             % (real_tool, [ln for _n, ln in lines][:12]))

    opened = [line for name, line in before if name in ("open", "openat")]
    reached = any(shim_env["mounts"] in line for line in opened)
    if case == "fast-path":
        assert not reached, (
            "the fast-path argv read the mount table, so the two rows are no "
            "longer distinct: %s" % opened)
    else:
        assert reached, (
            "this argv was supposed to reach the mount helper and did not, "
            "so it proves only what the fast-path row already proves: %s"
            % opened)

    created = [line for name, line in before if name in _FORKING]
    execed = [line for name, line in before if name == "execve"][1:]

    # WHICH programs ran, first: that is the shell-independent claim, and
    # the one a fork the text scan cannot see would break. The only
    # documented exception is the trusted awk `sg_load_mounts` runs once
    # over the table, and it may appear only on the path that reads it.
    # Counting the awk execs rather than hard-coding one keeps the row
    # honest where SG_AWK does not exist and the pure-sh reader runs
    # instead: there the answer is zero on both paths.
    awk = _sg_awk(rendered_shim["guard_text"])
    documented = [line for line in execed if ('"%s"' % awk) in line]
    assert execed == documented, (
        "a program the design does not document ran before the real tool "
        "under %s: %s" % (shell, [ln for ln in execed if ln not in documented]))
    if case == "fast-path":
        assert documented == [], (
            "the fast path ran the mount-table awk: %s" % documented)
        assert created == [], (
            "%d process(es) created on the fast path under %s -- ADR-0015's "
            "whole argument for `sh`: %s" % (len(created), shell, created[:4]))
        return

    # HOW MANY, second, and this one is shell-dependent by a measured
    # constant: for `sg_expensive=$(... awk ...)` dash forks once and exec's
    # the awk in the child, while bash forks a subshell for the command
    # substitution and then forks again to run the command in it. One extra
    # process on the way to the same single read, never two -- a third would
    # be a fork nothing here explains.
    assert len(created) <= len(documented) + 1, (
        "%d process(es) created before the exec of the real tool under %s "
        "for %d documented read(s): %s"
        % (len(created), shell, len(documented), created[:4]))


# --------------------------------------------------------------------------
# the renderer's contract with its templates
# --------------------------------------------------------------------------

def test_no_placeholder_survives(rendered_shim):
    assert "@@" not in rendered_shim["guard_text"]
    assert "@@" not in rendered_shim["names_text"]


def test_every_placeholder_in_the_templates_is_one_the_renderer_fills(policy, node_fs):
    site = conftest.make_site(node_fs["mounts"])
    filled = set(render.substitutions(policy, site, __version__)) | {"@@WRAPPED_NAMES@@"}
    assert filled == render.placeholders()


def test_the_templates_use_no_placeholder_outside_the_documented_set(rendered_shim):
    expected = {
        "@@CASES@@", "@@FSTYPES@@", "@@REMOTE_FSTYPES_RE@@", "@@REMOTE_PROXY@@",
        "@@MOUNT_OVERRIDES@@", "@@MAXDEPTH@@", "@@UNSCOPED_DEPTH@@",
        "@@DEPTH_ALLOWANCE_MAX@@", "@@MOUNTS_DEFAULT@@", "@@LOGGER@@", "@@AWK@@",
        "@@ID@@", "@@DATE@@", "@@ESCAPE@@", "@@EXIT@@", "@@BIN_DIR@@",
        "@@DISPLAY_NAME@@", "@@DOCS_LINE@@", "@@CONTACT_LINE@@", "@@MIN_JOB_AGE@@",
        "@@EXTRA_JOB_TOOLS@@", "@@VERSION@@", "@@FIND_PREFIX@@",
        "@@FIND_VALUE_LEAD@@", "@@FIND_GLUED@@", "@@WRAPPED_NAMES@@",
    }
    assert render.placeholders() == expected


def test_a_value_that_would_close_the_single_quotes_fails_the_render(policy, node_fs):
    """`SG_VERSION='...'` sits above sg_exec_real; a quote in the value closes
    the string and executes the rest. Refused at build, not escaped."""
    site = conftest.make_site(node_fs["mounts"])
    for bad in ("0.1.0'; echo PWNED; :'", "0.1.0\\", "0.1.0\n"):
        with pytest.raises(render.RenderError):
            render.render_shim(policy, site, bad)
    quoted = conftest.make_site(node_fs["mounts"])
    quoted["trusted_binaries"]["awk"] = "/usr/bin/aw'k"
    with pytest.raises(render.RenderError):
        render.render_shim(policy, quoted, __version__)
    with pytest.raises(render.RenderError):
        render.render_shim(policy.replace(mounts={"/a b": ("cheap", None)}), site, __version__)


def test_the_case_arms_come_from_the_wrapped_profiles_only(rendered_shim):
    """A tool the site leaves alone gets no arm and no name: both artifacts
    are built from wrapped_profiles(), so they cannot drift apart."""
    site = rendered_shim["site"]
    names = rendered_shim["names_text"].split("SG_WRAPPED_NAMES='", 1)[1].split("'")[0].split()
    assert tuple(names) == R.wrapped_names(set(site["shim"]["unwrapped_tools"]))
    for name in ("fzf", "sk"):
        assert name not in names
        assert re.search(r"^    (?:[a-z]+\|)*%s(?:\|[a-z]+)*\)$" % name,
                         rendered_shim["guard_text"], re.M) is None
    for name in names:
        assert re.search(r"^    (?:[a-z]+\|)*%s(?:\|[a-z]+)*\)$" % name,
                         rendered_shim["guard_text"], re.M), name


def test_the_unwrapped_list_is_site_configuration(make_shim_env):
    env = make_shim_env(**{"shim.unwrapped_tools": ["fzf", "sk", "du"]})
    names = env["rendered"]["names_text"]
    assert " du" not in names
    assert "grep" in names
    assert not os.path.exists(os.path.join(env["shim_dir"], "du"))
    assert "    du)" not in env["rendered"]["guard_text"]


def test_fzf_and_sk_are_never_wrapped_by_default(shim_env):
    assert not os.path.exists(os.path.join(shim_env["shim_dir"], "fzf"))
    assert not os.path.exists(os.path.join(shim_env["shim_dir"], "sk"))
    assert "fzf" in R.PROFILE_BY_NAME and "sk" in R.PROFILE_BY_NAME


def test_unknown_tool_is_passed_through(shim_env, tmp_path):
    """The dispatcher has no opinion about a name it does not carry."""
    stub = tmp_path / "othertool"
    stub.write_text("#!/bin/sh\nprintf 'RAN other\\n'\n")
    stub.chmod(0o755)
    os.symlink(os.path.join(shim_env["shim_dir"], "find"), str(tmp_path / "shim-othertool"))
    os.rename(str(tmp_path / "shim-othertool"), os.path.join(shim_env["shim_dir"], "othertool"))
    try:
        result = run_shim(shim_env, ["othertool", "-r", "/scratch"],
                          env={"PATH": "%s:%s:%s" % (shim_env["shim_dir"], tmp_path, shim_env["bin_dir"])})
        assert result.returncode == 0
        assert b"RAN other" in result.stdout
    finally:
        os.unlink(os.path.join(shim_env["shim_dir"], "othertool"))


def test_the_shim_reads_exactly_the_documented_seams(rendered_shim):
    """ADR-0013: the set of WALK_BLOCKER_* names the shim reads is a closed
    list, and a new one is a test failure and a decision."""
    read = set(re.findall(r"\$\{?(WALK_BLOCKER_[A-Z_]+)", rendered_shim["guard_text"]))
    assert read == {"WALK_BLOCKER_UNSCOPED", "WALK_BLOCKER_MOUNTS", "WALK_BLOCKER_FSTYPES",
                    "WALK_BLOCKER_DEPTH_BY_MOUNT", "WALK_BLOCKER_SHIM_DIR", "WALK_BLOCKER_AUDIT"}
    assert R.ESCAPE_HATCH in read and R.FSTYPES_SEAM in read and R.DEPTH_BY_MOUNT_SEAM in read
    header = rendered_shim["guard_text"][:rendered_shim["guard_text"].index("set -f")]
    for name in read:
        assert name in header, "%s is read but not inventoried in the header" % name


def test_the_rendered_shim_carries_no_predecessor_name(rendered_shim):
    for text in (rendered_shim["guard_text"], rendered_shim["names_text"]):
        assert "searchguard" not in text.lower()
        assert "@@" not in text


def test_the_rendered_artifacts_are_shellcheck_clean(rendered_shim):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not on PATH")
    run = subprocess.run(["shellcheck", "--shell=sh", "--severity=warning",
                          rendered_shim["guard"], rendered_shim["names"]],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_sentinels_resolve_to_directories_the_table_calls_expensive(node_fs, mounts):
    """Or the cwd-dependent rows prove something else (argv-grammar rules)."""
    for sentinel in (FAST_CWD, FAST_DEEP):
        real = node_fs[sentinel]
        assert os.path.isdir(real)
        assert any(R.depth_below(real, row[0]) is not None for row in mounts), sentinel


def test_the_template_lives_under_node_and_the_renderer_finds_it():
    assert render.template_path(render.GUARD_TEMPLATE).startswith(os.path.join(ROOT, "node"))
    assert os.path.exists(render.template_path(render.GUARD_TEMPLATE))
    assert os.path.exists(render.template_path(render.NAMES_TEMPLATE))


@pytest.mark.parametrize("shell", ("dash", "bash"))
def test_a_refusal_runs_exactly_the_programs_the_audit_path_documents(
        tmp_path, shim_env, rendered_shim, logger_stub, shell):
    """The audit path, counted (ADR-0018, #29). A refused walk is past the
    decision and off both of the design's counts; `sg_audit_emit`'s own
    comment says it runs four programs -- the trusted `date`, `id`, `awk`
    and `logger` -- to write the record, and until now that number rested
    on the comment's word. Before it, the guarded path pays its one `awk`
    to read the mount table. Five program starts, four distinct programs,
    the mount-table `awk` first, and nothing else: an unexplained program
    on this path is a change to the record's cost that the comment and the
    ADR would both be wrong about.

    The fixture site points the trusted logger at nothing, so the record's
    fourth program is reached through the documented PATH fallback -- the
    stub on `logger_stub["bin"]` -- and its path is what the trace shows.
    """
    argv = ["grep", "-r", "needle", "/scratch"]
    # The logger stub's directory FIRST. The shim is invoked by path and
    # resolves the real tool through the closed bin_dir, so nothing else
    # reads PATH; putting the stub first means the fallback's `logger` is
    # found at the first directory tried, and dash -- which searches PATH
    # by attempting execve in each entry -- leaves no failed attempt in the
    # trace. A failed attempt that strace split across an `<unfinished ...>`
    # and a `<... resumed>` line would otherwise read as a started program.
    env = {"PATH": "%s:%s:%s" % (logger_stub["bin"], shim_env["shim_dir"],
                                 shim_env["bin_dir"])}
    lines, stdout = _trace_shim(
        tmp_path, _shell_as_sh(tmp_path, shell), shim_env, argv,
        cwd="/home/someone", label="%s-refused" % shell, env=env)
    assert "RAN " not in stdout, (
        "the walk was allowed, so this is not the refusal path: %r" % stdout)

    guard = rendered_shim["guard_text"]
    awk = _sg_var(guard, "SG_AWK")
    date = _sg_var(guard, "SG_DATE")
    ident = _sg_var(guard, "SG_ID")
    logger = os.path.join(logger_stub["bin"], "logger")
    for path in (awk, date, ident):
        assert os.access(path, os.X_OK), (
            "%s is not executable here, so the trace would show the PATH "
            "fallback instead of the documented program" % path)

    programs = _programs_run(lines)
    assert programs[:1] == [awk], (
        "the guarded path's mount-table read is not the first program under "
        "%s: %s" % (shell, programs))
    assert sorted(programs[1:]) == sorted([date, ident, awk, logger]), (
        "the audit path under %s ran %s; the design documents exactly "
        "date, id, awk and logger" % (shell, programs[1:]))

    calls = [line for line in logger_stub["log"].read_text().splitlines()
             if line.strip()]
    assert len(calls) == 1 and '"action":"refused"' in calls[0], calls
