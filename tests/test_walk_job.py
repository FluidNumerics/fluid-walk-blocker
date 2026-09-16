"""node/walk-job: the command every refusal advertises.

Driven under **dash**, against a fake PATH of stubs, never the real sbatch.
A test that submitted a real job would put load on a shared cluster from a
unit test, which is the opposite of this tool's purpose.

The in-tree file carries `@@key@@` sentinels on its marker lines (ADR-0013),
so nothing here runs it as checked in: every case runs a copy stamped with a
FICTIONAL site through the real stamper, which is what `walk-blocker build`
does. The values are fixtures (ADR-0014); none is a site's.

The argv re-quoting is the reason this file exists. `walk-job` hands the
user's command to `sbatch --wrap` as ONE shell string, so every operand makes
a round trip through shell quoting; a defect there does not fail loudly, it
silently walks the wrong path or expands a glob against the job's cwd.
"""

import os
import shutil
import subprocess

import pytest

from conftest import ROOT
from walk_blocker import __version__, stamp
from walk_blocker import search_rules as R

WALK_JOB_SRC = os.path.join(ROOT, "node", "walk-job")

# dash, not bash: /bin/sh on an enterprise login node is often dash, and
# walk-job's parameter expansions (the %j peeler especially) are exactly where
# the two diverge.
SH = shutil.which("dash") or "sh"

# Every source the file must carry a marker for -- the `CONSUMERS["walk-job"]`
# row, pinned here so a marker that is renamed or deleted fails a test in
# this file as well as the build.
WALK_JOB_SOURCES = (
    "VERSION",
    "site.toml:slurm.partition",
    "site.toml:slurm.qos",
    "site.toml:slurm.account",
    "site.toml:slurm.default_time",
    "site.toml:slurm.default_mem",
    "site.toml:slurm.sbatch_glob",
    "site.toml:slurm.nice",
    "site.toml:slurm.output_pattern",
    "site.toml:trusted_binaries.bfs",
)

# Which shell constant each source lands on.
WALK_JOB_MARKERS = {
    "VERSION": "WJ_VERSION",
    "site.toml:slurm.partition": "WJ_SITE_PARTITION",
    "site.toml:slurm.qos": "WJ_SITE_QOS",
    "site.toml:slurm.account": "WJ_SITE_ACCOUNT",
    "site.toml:slurm.default_time": "WJ_SITE_TIME",
    "site.toml:slurm.default_mem": "WJ_SITE_MEM",
    "site.toml:slurm.sbatch_glob": "WJ_SITE_SBATCH_GLOB",
    "site.toml:slurm.nice": "WJ_NICE",
    "site.toml:slurm.output_pattern": "WJ_SITE_OUT",
    "site.toml:trusted_binaries.bfs": "WJ_SITE_BFS",
}


def site_values(tmp_path, **overrides):
    """The fixture site's `[slurm]` and `[trusted_binaries].bfs`, keyed the
    way `stamp_text` asks for them. The sbatch glob points into the tmp tree
    so the no-sbatch-anywhere branch is reachable on a host that has a
    scheduler installed. `overrides` are bare dotted keys."""
    values = {
        "VERSION": __version__,
        "site.toml:slurm.partition": "walkers",
        "site.toml:slurm.qos": "low",
        "site.toml:slurm.account": "",
        "site.toml:slurm.default_time": "1:00:00",
        "site.toml:slurm.default_mem": "8G",
        "site.toml:slurm.sbatch_glob": str(tmp_path / "opt" / "slurm-*" / "bin" / "sbatch"),
        "site.toml:slurm.nice": 19,
        "site.toml:slurm.output_pattern": "walk-job-%j.out",
        "site.toml:trusted_binaries.bfs": "/usr/bin/bfs",
    }
    for key, value in overrides.items():
        values["site.toml:" + key] = value
    return values


def stamp_walk_job(tmp_path, **overrides):
    """`node/walk-job` stamped with the fixture site, written as
    `<tmp_path>/walk-job` (the basename is `$0`, hence the job name) with the
    payload's mode. Returns (path, stamped text, values)."""
    with open(WALK_JOB_SRC) as fh:
        source = fh.read()
    values = site_values(tmp_path, **overrides)
    text = stamp.stamp_text(source, values, "sh")
    # The header comment describes the sentinel convention, so look at the
    # marker values themselves, not the whole file.
    assert all("@@" not in m.value for m in stamp.find_markers(text)), \
        "a sentinel survived stamping"
    path = tmp_path / "walk-job"
    path.write_text(text)
    path.chmod(0o755)
    return str(path), text, values


@pytest.fixture
def walk_job(tmp_path):
    """The default stamped copy; most tests drive this one."""
    return stamp_walk_job(tmp_path)[0]


# Prints the --wrap payload and nothing else, so a test can execute it and see
# what argv the compute node would receive.
SBATCH_ECHO_WRAP = """\
#!/bin/sh
while [ $# -gt 0 ]; do
    [ "$1" = --wrap ] && { printf '%s\\n' "$2" > "$WRAPFILE"; break; }
    shift
done
echo 4242
"""

# Records the full argv, one per line, for assertions about sbatch options.
SBATCH_RECORD_ARGV = """\
#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a" >> "$ARGVFILE"; done
echo 4242
"""

ARGV_DUMP = """\
#!/bin/sh
i=0
for a in "$@"; do i=$((i+1)); printf '[%d]=%s\\n' "$i" "$a"; done
"""


def _bin(tmp_path, **scripts):
    """A fake PATH containing only what a test puts in it."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name, body in scripts.items():
        p = d / name
        p.write_text(body)
        p.chmod(0o755)
    return d


def run_wj(script, tmp_path, args, bin_dir=None, extra_env=None):
    env = {
        # Deliberately minimal: walk-job must not depend on the caller's PATH
        # beyond what it resolves itself. /usr/bin and /bin carry sed.
        "PATH": "%s:/usr/bin:/bin" % (bin_dir or ""),
        "HOME": str(tmp_path),
    }
    env.update(extra_env or {})
    return subprocess.run(
        [SH, script] + list(args),
        cwd=str(tmp_path), capture_output=True, text=True, env=env, timeout=30)


def _argv_of(argvfile):
    return argvfile.read_text().splitlines()


def _value_of(argv, flag):
    return argv[argv.index(flag) + 1]


# --- the markers, and the stamper's view of the file -----------------------

def test_the_in_tree_file_carries_exactly_the_expected_markers():
    """One marker per source, on the named constant, each holding a sentinel
    -- so a checked-in `walk-job` can never be mistaken for a built one."""
    with open(WALK_JOB_SRC) as fh:
        markers = stamp.find_markers(fh.read())
    assert {m.source: m.name for m in markers} == WALK_JOB_MARKERS
    assert set(WALK_JOB_MARKERS) == set(WALK_JOB_SOURCES)
    for m in markers:
        assert m.value.startswith("'@@") and m.value.endswith("@@'"), m.value


def test_a_stamped_copy_is_current_and_a_stale_marker_is_reported(tmp_path):
    """`check_text` is the `--check` oracle: clean on what the stamper wrote,
    and naming the one line that drifted when a value is edited by hand."""
    _path, text, values = stamp_walk_job(tmp_path)
    assert stamp.check_text(text, values, "sh", required=WALK_JOB_SOURCES) == []

    stale = text.replace("WJ_SITE_PARTITION='walkers'", "WJ_SITE_PARTITION='elsewhere'")
    assert stale != text
    findings = stamp.check_text(stale, values, "sh", required=WALK_JOB_SOURCES)
    assert len(findings) == 1, findings
    assert findings[0].startswith("stale: ")
    assert "WJ_SITE_PARTITION = 'elsewhere'" in findings[0]
    assert "site.toml:slurm.partition says 'walkers'" in findings[0]


def test_version_prints_the_walk_blocker_version(tmp_path, walk_job):
    bin_dir = _bin(tmp_path)
    r = run_wj(walk_job, tmp_path, ["--version"], bin_dir)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "walk-blocker %s\n" % __version__


# --- argv survives the trip through --wrap ---------------------------------

@pytest.mark.parametrize("operand", [
    "/plain/path",
    "/path with spaces",
    "quote'inside",
    'double"quote',
    "glob*.ckpt",
    "brackets[0-9]?",
    "semi;colon",
    "pipe|char",
    "$(uname)",
    "`hostname`",
    "${HOME}",
    "back\\slash",
    "new\nline",
    "tab\there",
    "-leading-dash-after-ddash",
    "*",
])
def test_operands_reach_the_compute_node_byte_for_byte(tmp_path, walk_job, operand):
    """Each operand must arrive as ONE argument, unexpanded.

    `$(uname)` and backticks are in this list because --wrap is evaluated by a
    shell on the compute node: a quoting defect there is command injection
    into the job, not a cosmetic bug. `*` alone is in it because an unquoted
    glob would expand against the job's cwd and silently change the walk.
    """
    wrapfile = tmp_path / "wrap.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_ECHO_WRAP, argvdump=ARGV_DUMP)
    r = run_wj(walk_job, tmp_path, ["--", "argvdump", operand],
               bin_dir, {"WRAPFILE": str(wrapfile)})
    assert r.returncode == 0, r.stderr
    wrap = wrapfile.read_text().rstrip("\n")

    # Execute the payload the compute node would run, with nice stripped.
    assert wrap.startswith("nice -n 19 ")
    played = subprocess.run(
        [SH, "-c", wrap[len("nice -n 19 "):]],
        capture_output=True, text=True,
        env={"PATH": "%s:/usr/bin:/bin" % bin_dir, "HOME": str(tmp_path)},
        cwd=str(tmp_path), timeout=30)
    assert played.returncode == 0, played.stderr
    assert played.stdout == "[1]=%s\n" % operand


def test_the_command_is_not_split_on_whitespace(tmp_path, walk_job):
    """Three operands stay three, not five."""
    wrapfile = tmp_path / "wrap.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_ECHO_WRAP, argvdump=ARGV_DUMP)
    run_wj(walk_job, tmp_path, ["--", "argvdump", "a b", "c", "d e"],
           bin_dir, {"WRAPFILE": str(wrapfile)})
    wrap = wrapfile.read_text().rstrip("\n")
    played = subprocess.run(
        [SH, "-c", wrap[len("nice -n 19 "):]], capture_output=True, text=True,
        env={"PATH": "%s:/usr/bin:/bin" % bin_dir}, cwd=str(tmp_path), timeout=30)
    assert played.stdout == "[1]=a b\n[2]=c\n[3]=d e\n"


# --- the sbatch options that are load-bearing ------------------------------

def test_the_submission_carries_the_guardrail_options(tmp_path, walk_job):
    """The bound, the low-priority QoS, and an explicit memory reservation.

    --mem is not tuning: on a partition with DefMemPerNode=UNLIMITED a job
    naming no memory is charged the whole node and queues behind an idle one
    (ADR-0007). If this option is ever dropped, the sanctioned alternative
    looks broken on first contact.
    """
    argvfile = tmp_path / "argv.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    r = run_wj(walk_job, tmp_path, ["--", "true"], bin_dir, {"ARGVFILE": str(argvfile)})
    assert r.returncode == 0, r.stderr
    argv = _argv_of(argvfile)

    assert _value_of(argv, "--partition") == "walkers"
    assert _value_of(argv, "--qos") == "low"
    assert _value_of(argv, "--time") == "1:00:00"
    assert _value_of(argv, "--mem") == "8G"
    assert "--parsable" in argv
    # The site stamped no account, so none is claimed.
    assert "--account" not in argv
    # The escape hatch travels with the job, deliberately -- see walk-job's
    # comment for where that override is journaled.
    assert _value_of(argv, "--export") == "ALL,WALK_BLOCKER_UNSCOPED=1"


def test_qos_is_omitted_when_the_site_stamps_none(tmp_path):
    """An empty QoS means "do not pass --qos", never `--qos ''`: the scheduler
    would reject the empty name, and the site said it has no QoS to give."""
    script = stamp_walk_job(tmp_path, **{"slurm.qos": ""})[0]
    argvfile = tmp_path / "argv.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    r = run_wj(script, tmp_path, ["--", "true"], bin_dir, {"ARGVFILE": str(argvfile)})
    assert r.returncode == 0, r.stderr
    argv = _argv_of(argvfile)
    assert "--qos" not in argv
    assert "" not in argv
    assert _value_of(argv, "--partition") == "walkers"
    # The summary names only what was passed.
    assert "submitted walk-job as job 4242 (walkers, limit 1:00:00, mem 8G)" in r.stdout

    # A caller can still ask for one.
    argvfile.write_text("")
    r = run_wj(script, tmp_path, ["-q", "urgent", "--", "true"], bin_dir,
               {"ARGVFILE": str(argvfile)})
    assert r.returncode == 0, r.stderr
    assert _value_of(_argv_of(argvfile), "--qos") == "urgent"


def test_account_is_passed_when_the_site_stamps_one(tmp_path):
    script = stamp_walk_job(tmp_path, **{"slurm.account": "shared"})[0]
    argvfile = tmp_path / "argv.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    r = run_wj(script, tmp_path, ["--", "true"], bin_dir, {"ARGVFILE": str(argvfile)})
    assert r.returncode == 0, r.stderr
    argv = _argv_of(argvfile)
    assert _value_of(argv, "--account") == "shared"
    assert "(walkers, qos low, account shared, limit 1:00:00, mem 8G)" in r.stdout


def test_the_stamped_nice_and_output_pattern_are_used(tmp_path):
    """The two values with no caller-side override: nice lands in the wrap
    string, the output pattern is the -o default and is resolved for the
    human."""
    script = stamp_walk_job(tmp_path, **{"slurm.nice": 5,
                                         "slurm.output_pattern": "walk-%j.log"})[0]
    argvfile = tmp_path / "argv.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    r = run_wj(script, tmp_path, ["--", "true"], bin_dir, {"ARGVFILE": str(argvfile)})
    assert r.returncode == 0, r.stderr
    argv = _argv_of(argvfile)
    assert _value_of(argv, "--wrap") == "nice -n 5 'true'"
    assert _value_of(argv, "--output") == "walk-%j.log"
    assert "output: walk-4242.log\n" in r.stdout


@pytest.mark.parametrize("flag,env,value,sbatch_flag", [
    ("-t", "WALK_JOB_TIME", "4:00:00", "--time"),
    ("-m", "WALK_JOB_MEM", "32G", "--mem"),
    ("-p", "WALK_JOB_PARTITION", "main", "--partition"),
    ("-q", "WALK_JOB_QOS", "normal", "--qos"),
])
def test_each_override_works_as_a_flag_and_as_an_env_var(
        tmp_path, walk_job, flag, env, value, sbatch_flag):
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    for args, extra in (([flag, value, "--", "true"], {}),
                        (["--", "true"], {env: value})):
        argvfile = tmp_path / "argv.txt"
        argvfile.write_text("")
        e = {"ARGVFILE": str(argvfile)}
        e.update(extra)
        r = run_wj(walk_job, tmp_path, args, bin_dir, e)
        assert r.returncode == 0, r.stderr
        assert _value_of(_argv_of(argvfile), sbatch_flag) == value, (args, extra)


# --- resolving sbatch ------------------------------------------------------

def test_sbatch_is_found_on_path(tmp_path, walk_job):
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    r = run_wj(walk_job, tmp_path, ["-n", "--", "true"], bin_dir)
    assert r.returncode == 0
    assert r.stdout.startswith("sbatch ")


def test_no_sbatch_anywhere_reports_it_instead_of_dying_silently(tmp_path, walk_job):
    """The path an earlier review claimed exits 1 with no message under set -e.

    It does not: `set -e` is ignored for a failing left operand of an && list,
    so the fallback loop falls through to wj_die. Pinned because the claim was
    plausible and the failure would be a guard whose advertised alternative
    dies wordlessly on any node without sbatch.

    The stamped glob points into the tmp tree, where nothing is installed, so
    this proves the branch on a host that has a scheduler as well as on one
    that does not.
    """
    bin_dir = _bin(tmp_path)  # empty: no sbatch on PATH either
    r = run_wj(walk_job, tmp_path, ["--", "true"], bin_dir)
    assert r.returncode == 2
    assert "no sbatch on PATH" in r.stderr
    assert str(tmp_path / "opt" / "slurm-*" / "bin" / "sbatch") in r.stderr


def test_a_glob_that_matches_nothing_is_not_taken_for_a_path(tmp_path, walk_job):
    """An unmatched glob stays literal in sh, so the fallback must reject it
    on `-x` rather than hand a path with a `*` in it to exec as a filename --
    which would report a submission against a path that does not exist.

    Through the environment seam this time, so the override is exercised too.
    """
    bin_dir = _bin(tmp_path)
    glob = str(tmp_path / "no-such-*" / "sbatch")
    r = run_wj(walk_job, tmp_path, ["--", "true"], bin_dir,
               {"WALK_JOB_SBATCH_GLOB": glob})
    assert r.returncode == 2
    assert glob in r.stderr, r.stderr
    assert "submitted" not in r.stdout


def test_sbatch_is_found_under_the_glob_when_it_is_not_on_path(tmp_path, walk_job):
    """The branch that carries the login node's real case.

    `ssh host 'cmd'` gets no login profile, so sbatch is absent from PATH --
    the incident shape this tree exists for. The fallback must SUCCEED, not
    only fail: a defect in the resolved path (the loop leaving wj_sbatch
    relative, say) would otherwise ship.
    """
    argvfile = tmp_path / "argv.txt"
    opt = tmp_path / "opt" / "slurm-1.2.3" / "bin"
    opt.mkdir(parents=True)
    (opt / "sbatch").write_text(SBATCH_RECORD_ARGV)
    (opt / "sbatch").chmod(0o755)

    bin_dir = _bin(tmp_path)  # sbatch is NOT on PATH; the stamped glob finds it
    r = run_wj(walk_job, tmp_path, ["-n", "--", "du", "-sh", "/x"], bin_dir)
    assert r.returncode == 0, r.stderr
    # The dry run prints what it would exec: the absolute resolved path, not
    # the bare name, since the bare name is what was missing.
    assert r.stdout.startswith("%s " % (opt / "sbatch")), r.stdout

    r = run_wj(walk_job, tmp_path, ["--", "du", "-sh", "/x"], bin_dir,
               {"ARGVFILE": str(argvfile)})
    assert r.returncode == 0, r.stderr
    assert "job 4242" in r.stdout
    assert "--wrap" in argvfile.read_text()


def test_path_wins_over_the_glob(tmp_path, walk_job):
    """`command -v` first, so a node with both uses the one the operator's
    profile put on PATH -- and a stale versioned prefix left behind by an
    upgrade does not quietly become the submitter."""
    on_path = tmp_path / "argv-path.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    opt = tmp_path / "opt" / "slurm-old" / "bin"
    opt.mkdir(parents=True)
    (opt / "sbatch").write_text("#!/bin/sh\necho 9999\n")
    (opt / "sbatch").chmod(0o755)

    r = run_wj(walk_job, tmp_path, ["--", "true"], bin_dir, {"ARGVFILE": str(on_path)})
    assert r.returncode == 0, r.stderr
    assert "job 4242" in r.stdout, "the glob's sbatch answered instead of PATH's"
    assert on_path.exists()


# --- what sbatch hands back ------------------------------------------------

def test_a_non_numeric_answer_from_sbatch_is_not_reported_as_a_job(tmp_path, walk_job):
    """`sbatch: error: ...` on stdout must not become a job id."""
    bin_dir = _bin(tmp_path, sbatch=(
        "#!/bin/sh\necho 'sbatch: error: invalid partition'\n"))
    r = run_wj(walk_job, tmp_path, ["--", "true"], bin_dir)
    assert r.returncode == 1
    assert "did not return a job id" in r.stderr
    assert "submitted" not in r.stdout


def test_the_cluster_suffix_from_parsable_is_stripped(tmp_path, walk_job):
    bin_dir = _bin(tmp_path, sbatch="#!/bin/sh\necho '4243;clusterb'\n")
    r = run_wj(walk_job, tmp_path, ["--", "true"], bin_dir)
    assert r.returncode == 0, r.stderr
    assert "job 4243" in r.stdout
    assert ";clusterb" not in r.stdout


# --- %j, and %% which is a literal % to sbatch -----------------------------

@pytest.mark.parametrize("pattern,expected", [
    ("out-%j.log", "out-4242.log"),
    ("j%j-%j.log", "j4242-4242.log"),
    ("out-%%j.log", "out-%j.log"),
    ("%j-%%j-%j", "4242-%j-4242"),
    ("plain.log", "plain.log"),
    ("%j", "4242"),
    ("%%", "%"),
    ("weird[*?]-%j.log", "weird[*?]-4242.log"),
])
def test_the_reported_output_path_matches_what_sbatch_will_write(
        tmp_path, walk_job, pattern, expected):
    """%% is a literal % to sbatch, so `out-%%j.log` becomes `out-%j.log`.

    Substituting every `%j` blindly reported a path that would never exist --
    a wrong answer that looks like a right one, which is worse than an error.
    The glob-metacharacter row is there because the peeler uses parameter
    expansion, where an unquoted inner expansion would be read as a pattern.
    """
    bin_dir = _bin(tmp_path, sbatch="#!/bin/sh\necho 4242\n")
    r = run_wj(walk_job, tmp_path, ["-o", pattern, "--", "true"], bin_dir)
    assert r.returncode == 0, r.stderr
    assert "output: %s\n" % expected in r.stdout


# --- argument handling -----------------------------------------------------

@pytest.mark.parametrize("args,needle", [
    (["-t"], "-t needs a value"),
    (["-m"], "-m needs a value"),
    (["-o"], "-o needs a value"),
    (["-p"], "-p needs a value"),
    (["-q"], "-q needs a value"),
    (["--bogus", "--", "true"], "unknown option --bogus"),
])
def test_malformed_invocations_are_refused_with_a_reason(tmp_path, walk_job, args, needle):
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    r = run_wj(walk_job, tmp_path, args, bin_dir)
    assert r.returncode == 2
    assert needle in r.stderr


def test_no_command_prints_usage_and_submits_nothing(tmp_path, walk_job):
    argvfile = tmp_path / "argv.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    r = run_wj(walk_job, tmp_path, ["--"], bin_dir, {"ARGVFILE": str(argvfile)})
    assert r.returncode == 2
    assert "usage:" in r.stderr
    assert not argvfile.exists()


def test_a_bare_command_without_the_separator_still_works(tmp_path, walk_job):
    """The refusal text shows `--`, but someone reading it in anger will omit
    it, and refusing that would be a syntax lesson at the worst moment."""
    wrapfile = tmp_path / "wrap.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_ECHO_WRAP)
    r = run_wj(walk_job, tmp_path, ["du", "-sh", "/some/path"],
               bin_dir, {"WRAPFILE": str(wrapfile)})
    assert r.returncode == 0, r.stderr
    assert wrapfile.read_text().rstrip("\n") == (
        "nice -n 19 'du' '-sh' '/some/path'")


def test_dry_run_submits_nothing(tmp_path, walk_job):
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    argvfile = tmp_path / "argv.txt"
    r = run_wj(walk_job, tmp_path, ["-n", "--", "du", "-sh", "/x"],
               bin_dir, {"ARGVFILE": str(argvfile)})
    assert r.returncode == 0, r.stderr
    assert not argvfile.exists(), "-n must not invoke sbatch"


def test_dry_run_prints_exactly_what_a_real_submit_would_run(tmp_path, walk_job):
    """-n is only worth having if it is faithful.

    Asserting a substring of the printed line proved brittle and weak -- the
    line shell-quotes every argument, so the --wrap payload arrives
    nested-quoted and a naive `"'du' '-sh'" in stdout` fails against correct
    output. So compare behaviour instead: eval the printed line and check it
    produces byte-identical argv to an actual submit of the same command.
    """
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    cmd = ["du", "-sh", "/path with spaces", "glob*"]

    real = tmp_path / "real.txt"
    r = run_wj(walk_job, tmp_path, ["--"] + cmd, bin_dir, {"ARGVFILE": str(real)})
    assert r.returncode == 0, r.stderr

    r = run_wj(walk_job, tmp_path, ["-n", "--"] + cmd, bin_dir)
    assert r.returncode == 0, r.stderr
    printed = r.stdout.strip()

    replayed = tmp_path / "replayed.txt"
    ev = subprocess.run(
        [SH, "-c", printed], capture_output=True, text=True, cwd=str(tmp_path),
        env={"PATH": "%s:/usr/bin:/bin" % bin_dir,
             "ARGVFILE": str(replayed)}, timeout=30)
    assert ev.returncode == 0, ev.stderr
    assert replayed.read_text() == real.read_text()


def test_help_exits_zero_and_names_the_cost_it_does_not_remove(tmp_path, walk_job):
    """`walk-job -h` is what the refusal points at, so it has to say that this
    moves load off the login node and not off the filesystem."""
    bin_dir = _bin(tmp_path, sbatch=SBATCH_RECORD_ARGV)
    r = run_wj(walk_job, tmp_path, ["-h"], bin_dir)
    assert r.returncode == 0
    assert "usage:" in r.stdout
    assert "does not make the walk cheap" in r.stdout
    # The stamped defaults are what the help advertises.
    assert "(walkers, qos low)" in r.stdout
    assert "default ./walk-job-%j.out" in r.stdout


# --- what it must NOT do ---------------------------------------------------

def test_ionice_is_not_used(tmp_path, walk_job):
    """Removed deliberately: a network filesystem's client generates the
    traffic, and a block-layer I/O class applies to a local device, so it
    reaches none of what the job does. It looked like throttling and
    throttled nothing."""
    wrapfile = tmp_path / "wrap.txt"
    bin_dir = _bin(tmp_path, sbatch=SBATCH_ECHO_WRAP)
    run_wj(walk_job, tmp_path, ["--", "true"], bin_dir, {"WRAPFILE": str(wrapfile)})
    assert "ionice" not in wrapfile.read_text()
    assert "nice -n 19" in wrapfile.read_text()


# --- bfs's argv[0] is pinned to the site's trusted binary -------------------
# A `bfs` process's comm does not name its version -- or anything reliable:
# what shows up there is whatever wrapper launched it. And /proc/PID/exe is
# unreadable for another user's process, so a submitted `bfs` job would leave
# no record of WHICH bfs ran. Pinning argv[0] to `[trusted_binaries].bfs` puts
# the answer in the job's own command line -- the only place it survives.
#
# The existence check happens INSIDE the wrap string, not before it. walk-job
# submits from the login node, but the wrapped command executes on a compute
# node whose package state this process never observes. So the wrap string
# carries the conditional itself:
#
#   if [ -x '<pinned>' ]; then nice -n 19 '<pinned>' ARGS; else nice -n 19 bfs ARGS; fi
#
# and it runs where its answer applies.

def _wrap_of(script, tmp_path, args, bin_dir, extra_env=None):
    wrapfile = tmp_path / "wrap.txt"
    env = {"WRAPFILE": str(wrapfile)}
    env.update(extra_env or {})
    r = run_wj(script, tmp_path, args, bin_dir, env)
    assert r.returncode == 0, r.stderr
    return wrapfile.read_text().rstrip("\n")


def _expect_bfs_wrap(pinned, *quoted_ops):
    rest = "".join(" %s" % q for q in quoted_ops)
    return ("if [ -x '%s' ]; then nice -n 19 '%s'%s; "
            "else nice -n 19 bfs%s; fi") % (pinned, pinned, rest, rest)


def test_a_bare_bfs_is_replaced_by_the_stamped_binary(tmp_path):
    """The stamped `[trusted_binaries].bfs` is the pin, with no override."""
    pinned = _bin(tmp_path, sbatch=SBATCH_ECHO_WRAP, bfs=ARGV_DUMP) / "bfs"
    script = stamp_walk_job(tmp_path, **{"trusted_binaries.bfs": str(pinned)})[0]
    wrap = _wrap_of(script, tmp_path, ["--", "bfs", "/scratch/x", "-name", "y"],
                    tmp_path / "bin")
    assert wrap == _expect_bfs_wrap(pinned, "'/scratch/x'", "'-name'", "'y'"), wrap


def test_the_bfs_arm_is_skipped_when_the_site_names_no_bfs(tmp_path):
    """No `[trusted_binaries].bfs` means nothing to pin to: a bare `bfs` is
    submitted exactly as typed, with no `-x` test in the wrap string."""
    script = stamp_walk_job(tmp_path, **{"trusted_binaries.bfs": ""})[0]
    bin_dir = _bin(tmp_path, sbatch=SBATCH_ECHO_WRAP)
    wrap = _wrap_of(script, tmp_path, ["--", "bfs", "/scratch/x", "-name", "y"], bin_dir)
    assert wrap == "nice -n 19 'bfs' '/scratch/x' '-name' 'y'", wrap

    # The caller-side seam still works on such a build.
    wrap = _wrap_of(script, tmp_path, ["--", "bfs", "/scratch/x"], bin_dir,
                    {"WALK_JOB_BFS": "/opt/elsewhere/bfs"})
    assert wrap == _expect_bfs_wrap("/opt/elsewhere/bfs", "'/scratch/x'"), wrap


def test_the_pinned_path_still_resolves_to_the_bfs_profile(mounts, policy):
    """Layer 2 must still recognise the job, or pinning costs the audit trail.

    tool_from_argv() takes argv[0]'s BASENAME, so an absolute path resolves to
    the same profile -- and the record gets strictly better, because the argv
    now names the binary. Asserted rather than assumed: substituting a path
    the table could not resolve would trade one blind spot for another.
    """
    assert R.tool_from_argv(["/usr/bin/bfs", "/scratch", "-name", "x"]) == "bfs"
    # And the table agrees, since it is the other consumer of that argv.
    assert R.check(["/usr/bin/bfs", "/scratch", "-name", "x"], "/",
                   mounts, policy) is not None


def test_only_argv0_and_only_an_exact_bfs_is_pinned(tmp_path, walk_job):
    """A path the caller chose is theirs; only a bare `bfs` is ambiguous."""
    pinned = _bin(tmp_path, sbatch=SBATCH_ECHO_WRAP, bfs=ARGV_DUMP) / "bfs"
    bin_dir = tmp_path / "bin"
    env = {"WALK_JOB_BFS": str(pinned)}

    # A different tool.
    wrap = _wrap_of(walk_job, tmp_path, ["--", "du", "-sh", "/scratch/x"], bin_dir, env)
    assert str(pinned) not in wrap, wrap

    # An absolute bfs the caller supplied.
    wrap = _wrap_of(walk_job, tmp_path, ["--", "/opt/mine/bfs", "/scratch/x"],
                    bin_dir, env)
    assert "'/opt/mine/bfs'" in wrap and str(pinned) not in wrap, wrap

    # A relative one.
    wrap = _wrap_of(walk_job, tmp_path, ["--", "./bfs", "/scratch/x"], bin_dir, env)
    assert "'./bfs'" in wrap and str(pinned) not in wrap, wrap

    # And `bfs` as an OPERAND rather than the command.
    wrap = _wrap_of(walk_job, tmp_path, ["--", "du", "-sh", "bfs"], bin_dir, env)
    assert str(pinned) not in wrap, wrap

    # ...including when the COMMAND is bfs too, which is the case that
    # actually pins "argv[0] only". `du -sh bfs` never enters the branch, so
    # it cannot catch a loop that rewrites every matching argument -- and a
    # mutant doing exactly that passed the whole file until this row existed.
    # A directory called `bfs` is an ordinary thing to search.
    wrap = _wrap_of(walk_job, tmp_path, ["--", "bfs", "bfs", "-name", "y"],
                    bin_dir, env)
    assert wrap == _expect_bfs_wrap(pinned, "'bfs'", "'-name'", "'y'"), wrap

    # Same branch, with an operand that word-splitting would tear in half.
    wrap = _wrap_of(walk_job, tmp_path, ["--", "bfs", "/two words", "-name", "y"],
                    bin_dir, env)
    assert wrap == _expect_bfs_wrap(pinned, "'/two words'", "'-name'", "'y'"), wrap


def test_a_host_without_the_package_falls_back_to_bare_bfs_at_runtime(tmp_path):
    """The fallback has to prove itself by running, not just by reading.

    The wrap string always carries the `-x` test, because walk-job cannot
    know from the login node whether the compute node has the package.
    Executing the captured wrap payload on a "host" (this sandboxed PATH)
    where the pinned path does not exist is what proves the else branch
    lets the job's own PATH answer, rather than failing on a path that
    resolves nowhere.
    """
    bin_dir = _bin(tmp_path, sbatch=SBATCH_ECHO_WRAP, bfs=ARGV_DUMP)
    absent = tmp_path / "absent"
    script = stamp_walk_job(tmp_path, **{"trusted_binaries.bfs": str(absent)})[0]
    wrap = _wrap_of(script, tmp_path, ["--", "bfs", "/scratch/x", "-name", "y"], bin_dir)
    assert wrap == _expect_bfs_wrap(absent, "'/scratch/x'", "'-name'", "'y'"), wrap

    played = subprocess.run(
        [SH, "-c", wrap], capture_output=True, text=True,
        env={"PATH": "%s:/usr/bin:/bin" % bin_dir, "HOME": str(tmp_path)},
        cwd=str(tmp_path), timeout=30)
    assert played.returncode == 0, played.stderr
    assert played.stdout == "[1]=/scratch/x\n[2]=-name\n[3]=y\n", played.stdout
