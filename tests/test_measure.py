"""The benchmark's gates, driven by a shim that is slower on purpose.

`measure.sh` is the only thing standing between a `guard.sh` regression and a
node where that script runs in front of every grep, find and du for every
user, and until this file nothing exercised it -- it was linted by CI and
executed by nobody. The standard the benchmark applies to the shim applies to
the gate as much as to the number it guards: a performance claim nothing
enforces is a comment, not a budget.

**Nothing here asserts on a machine's timing.** The readings are meaningless
at the warmup and N these tests use, and that is deliberate: what is under test
is the gate arithmetic, the pair bookkeeping, and the ways a run is refused. In
single-guard mode the oracle for "slower" is a copy of the rendered shim with a
spin loop welded into it, so the sign of the overhead is known before the clock
is read. Where a test needs exact values -- every ratio-mode test that reaches
a reading, and the decimal budget test -- a scripted `date` stands in for the
clock on `PATH`, and the numbers asserted are the ones it was given.

The shim under test is RENDERED by the real renderer from the fixture policy
and the fixture site (`rendered_shim` in conftest), not read from a checked-in
file: the tree carries the template and the rule table, and `guard.sh` is a
build product (ADR-0013). Its compiled mount table is the fixture's, so the
guarded bench's operands under the tmp directory are judged cheap on every
machine and the allowed path is the one timed.
"""

import os
import re
import shutil
import subprocess

import pytest

from conftest import ROOT, SHIM_SH

MEASURE_SH = os.path.join(ROOT, "node", "shim", "measure.sh")

# Enough spin to dominate the measurement noise at any load. At a few
# microseconds per iteration in dash this adds several ms to a fast path that
# costs a low single-digit number of ms, so the overhead is unambiguously
# positive without the test ever saying by how much.
SPIN = 5000

# The real readings use the script's default warmup and N. These do not,
# because they are not readings.
FAST_ENV = {"WALK_BLOCKER_MEASURE_WARMUP": "1"}

# A drift ceiling chosen so the discard can never fire, for the tests whose
# subject is a verdict rather than the discard -- which has its own test below.
#
# A ceiling that looks generous -- 95 % -- is not one: at WARMUP=1 and N=1 two
# single-call readings of the *same* stub binary are not stable to within
# 95 %, and a run was caught where one read over 98 % and failed the ratio
# assertion on the discard path instead. 998.9 is the largest allowance
# measure.sh accepts (issue #223): drift is capped at 999, which is also the
# reading of a baseline that printed 0.00, so an allowance at the cap would
# keep a reading that measured nothing and is refused. It is not a proof of
# impossibility. Drift is 100*|a-b|/a over the FIRST baseline reading, so a
# second reading below the first never exceeds 100 %; a run is still refused
# when the second reads about eleven times the first, or the first reads 0.00,
# which a stub that runs `exit 0` reaches only under a stall that would make
# the run's verdict meaningless anyway.
NO_DISCARD = {"WALK_BLOCKER_MEASURE_DRIFT_PCT": "998.9"}

# What a `date` stub printed on its first call before issue #222: the counter
# file it reads does not exist yet, and a failed `<` reports itself before a
# later `2>/dev/null` takes effect.
STUB_NOISE = "No such file"


def run_measure(args, env=None, timeout=300):
    # The developer's own shell must not leak a knob or a shim seam into the
    # run: every WALK_BLOCKER_* variable is scrubbed, then the test's own
    # values applied.
    e = {k: v for k, v in os.environ.items() if not k.startswith("WALK_BLOCKER_")}
    e.update(FAST_ENV)
    e.update(env or {})
    return subprocess.run(
        [SHIM_SH, MEASURE_SH, *args],
        capture_output=True, text=True, timeout=timeout, env=e,
    )


@pytest.fixture(scope="module")
def guard(rendered_shim):
    """The rendered shim, as a path, for the runs whose outcome does not
    depend on its overhead being positive: refusals before timing, and runs
    meant to discard. See `slow_guard` for the rest."""
    return rendered_shim["guard"]


def _weld(directory, rendered_shim, line):
    """A copy of the rendered guard.sh, in `directory`, that runs `line` and
    then does the same work.

    The line goes *inside* the copy rather than into a wrapper that execs it,
    because the shim reads its own `$0` to learn which tool it is standing in
    for -- a wrapper would hand it the wrapper's name and measure a different
    code path, or none."""
    directory.mkdir(exist_ok=True)
    path = directory / "guard.sh"
    lines = rendered_shim["guard_text"].split("\n")
    assert lines[0].startswith("#!"), "guard.sh no longer starts with a shebang"
    path.write_text("\n".join([lines[0], line, *lines[1:]]))
    path.chmod(0o755)
    return str(path)


@pytest.fixture(scope="module")
def slow_guard(tmp_path_factory, rendered_shim):
    """A copy of the rendered guard.sh that does the same work, slower.

    Used by every single-guard test whose expected outcome requires the
    measured overhead to be POSITIVE (#47). `measure.sh` refuses, before any
    gate, when an overhead lands at or below zero, because a guarded run no
    slower than the bare one measured nothing. Against the plain guard that
    refusal is not hypothetical: its real fast path costs about the same as
    one scheduling hiccup, so on a contended runner the subtraction can land
    negative and a test asserting a budget verdict gets the refusal instead.

    Measured on a developer workstation under synthetic CPU load, NOT at a
    deployment -- 25 runs per arm: the plain guard returned a minimum overhead
    of -4.54 ms and took that refusal 3 times; this spun copy returned a
    minimum of +26.01 ms and took it none. Re-run that comparison if the
    shim's fast path changes, or if SPIN moves. Raising the iteration count
    does NOT substitute -- under load the spread grows with the count rather
    than shrinking, because the noise is correlated stalls rather than
    per-iteration jitter. The spin works because it adds deterministic work to
    the shim side only, and contention dilates that work just as it dilates
    the noise.
    """
    loop = (
        "_measure_spin=0\n"
        "while [ \"$_measure_spin\" -lt %d ]; do "
        "_measure_spin=$((_measure_spin + 1)); done" % SPIN
    )
    return _weld(tmp_path_factory.mktemp("slow"), rendered_shim, loop)


# --------------------------------------------------------------------------
# ratio mode, on a scripted clock
# --------------------------------------------------------------------------
#
# The ratio tests below assert on arithmetic and reporting -- which pair is
# charged to which guard, the median, the ceilings, the floor discard -- so
# they read a clock scripted per guard rather than the machine's. Every
# overhead, floor and ratio is then known before the run, the assertions are
# exact, and contention on the runner has nothing left to move (issue #63).
# Real guards with spin welded in were the earlier oracle; both readings were
# still timings, and a plain reference's overhead could land at or below zero
# and cost the run a pair.

# One bench's duration, in microseconds, for the readings no ratio is drawn
# from. The two baselines differ so an overhead taken from the wrong one
# changes the numbers; each half's two fast-path baselines agree, so drift is
# 0.0 % and never the reason a pair goes.
_BASE_US = 1000
_G_BASE_US = 1500


def _half(fast_us, guarded_us, floor_us):
    """One `measure_guard()` call's eight bench durations, in the order it
    benches them: baseline, shim, `python3 -S` floor, `python3` floor,
    baseline again, guarded baseline, guarded with one operand, with ten."""
    return [_BASE_US, _BASE_US + fast_us, floor_us, 2 * floor_us, _BASE_US,
            _G_BASE_US, _G_BASE_US + guarded_us, _G_BASE_US + guarded_us + 900]


# The pairs most of the tests below run: per pair, (fast overhead, guarded
# overhead, `python3 -S` floor) in microseconds. The reference reads the same
# every pair; the candidate's ratios are 1.100, 1.500, 1.200 on the fast path
# (median 1.200, out of order so a median that forgot to sort reads 1.500) and
# 1.300, 1.250, 1.400 on the guarded one (median 1.300). The two medians
# differ, so a gate reading the other's median decides differently, and the
# candidate's floor is 0.0 %, 25.0 % and 10.0 % off the reference's.
REF_PAIRS = [(1000, 1000, 10000)] * 3
CAND_PAIRS = [(1100, 1300, 10000), (1500, 1250, 12500), (1200, 1400, 11000)]


@pytest.fixture
def ratio_clock(tmp_path, rendered_shim):
    """Two guards and a `date` that times each of them as scripted.

    Returns a function taking the reference's and the candidate's per-pair
    `(fast, guarded, floor)` overheads in microseconds, and giving back the
    reference path, the candidate path and the env to run under.

    Each guard is the rendered `guard.sh` with one line welded in that writes
    its own name to a tag file, so it does the same work and passes the same
    reached-the-binary probes. `measure_guard()` probes before it benches, so
    by the first clock read of a half the tag names the guard that half is
    timing. The stub reads the tag on every end call and looks the duration up
    by guard, pair and bench, so the clock follows the guard and not the
    position: swapping the arguments, or the order a pair runs its halves in,
    moves the readings with the guards, as a real clock would. A clock
    scripted by position alone would hand the same numbers to whichever guard
    ran first, and the direction test below would pass against a gate that
    ignored its arguments.

    `guard.sh` itself never reads this `date`: it resolves `SG_DATE` by
    absolute path (see `degenerate_clock`).
    """
    def make(ref_pairs, cand_pairs):
        tag = tmp_path / "tag"
        paths = {}
        for name in ("ref", "cand"):
            paths[name] = _weld(tmp_path / name, rendered_shim,
                                "echo %s > %s" % (name, tag))
        binned = tmp_path / "clockbin"
        binned.mkdir()
        state = tmp_path / "calls"
        table = []
        for name, pairs in (("ref", ref_pairs), ("cand", cand_pairs)):
            for p, half in enumerate(pairs):
                for b, d in enumerate(_half(*half)):
                    table.append("D_%s_%d_%d=%d" % (name, p, b, d))
        # Calls alternate start, end; a start advances 1 ms and an end the
        # scripted duration, so at N=1 each bench reads exactly that. Bench i
        # of the run is call 2i+1's; eight benches make a half, two a pair.
        stub = binned / "date"
        stub.write_text(
            "#!/bin/sh\n" + "\n".join(table) + "\n"
            "k=0; t=0; [ ! -f %s ] || read -r k t < %s\n"
            "if [ $((k %% 2)) -eq 1 ]; then\n"
            "    read -r g < %s\n"
            "    i=$((k / 2))\n"
            '    eval "d=\\${D_${g}_$((i / 16))_$((i %% 8)):-1000}"\n'
            "    t=$((t + d * 1000))\n"
            "else\n"
            "    t=$((t + 1000 * 1000))\n"
            "fi\n"
            'echo "$((k + 1)) $t" > %s\n'
            'echo "$t"\n' % (state, state, tag, state)
        )
        stub.chmod(0o755)
        env = {"PATH": str(binned) + os.pathsep + os.environ["PATH"]}
        return paths["ref"], paths["cand"], env
    return make


def _lines(text, prefix):
    return [ln for ln in text.splitlines() if ln.startswith(prefix)]


def test_the_ratio_gate_refuses_a_candidate_that_got_slower(ratio_clock):
    """The gate that survives a change of machine: same machine, same minute,
    two shims."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "1.15"], env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "measure.sh: fast-path ratio 1.200x exceeds the" in r.stderr.splitlines()
    assert "1.15x ceiling" in r.stderr


def test_a_ceiling_the_candidate_clears_passes(ratio_clock):
    """The inverse, on the same pair of shims -- otherwise the test above
    would pass just as well against a gate that always fails. Both ceilings,
    because the guarded gate needs the same inverse and this run is already
    paid for. The fast ceiling sits between the two medians, so a fast gate
    that read the guarded median would refuse."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "1.25", "1.35"], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fast path within the 1.25x ceiling" in r.stdout.splitlines()
    assert "guarded path within the 1.35x ceiling" in r.stdout.splitlines()


def test_every_pair_charges_the_slowdown_to_the_candidate(ratio_clock):
    """Pair 1 runs the reference first and pair 2 runs the candidate first, so
    a ratio computed from the *order* rather than from the identity would come
    out inverted on pair 2. The clock follows the guards, so every pair must
    read the candidate's scripted slowdown."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100"], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(r.stdout, "per-pair fast ratios:") == \
        ["per-pair fast ratios: 1.100 1.500 1.200"], r.stdout


def test_the_comparison_runs_in_the_direction_the_arguments_name(ratio_clock):
    """Reference and candidate swapped: the same two files must now read as a
    speed-up, which is the only way to tell the ratio from its reciprocal."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", cand, ref, "1", "3", "100"], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(r.stdout, "per-pair fast ratios:") == \
        ["per-pair fast ratios: 0.909 0.667 0.833"], r.stdout


def test_the_guarded_half_of_the_ratio_gate_refuses_on_its_own(ratio_clock):
    """The guarded path is the only one `find`, `du`, `rg`, `fd` and `tree`
    ever take, and until this test nothing exercised its ceiling: mutating the
    guarded comparison so it could never fire left every other test green.

    The fast ceiling is set where it cannot fire and the guarded one where it
    must, so a pass here is the two gates being independent and not a run that
    failed for the other reason. The guarded ceiling sits between the two
    medians, so a guarded gate that read the fast median would pass."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "1.25"], env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "measure.sh: guarded ratio 1.300x exceeds the" in r.stderr.splitlines()
    assert "1.25x ceiling" in r.stderr
    assert "fast path within the 100x ceiling" in r.stdout.splitlines()


def test_a_pairs_argument_that_is_not_a_number_says_which_argument(guard):
    """dash's `[ abc -lt 3 ]` prints "Illegal number" and returns non-zero, so
    a typo'd PAIRS used to satisfy the guard, run zero pairs, and fail with the
    message that blames the machine."""
    r = run_measure(["--against", guard, guard, "1", "abc", "1.5"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "PAIRS must be a whole number, not 'abc'" in r.stderr
    assert "usable pairs" not in r.stdout


@pytest.fixture
def degenerate_clock(tmp_path):
    """A `date` that advances by exactly one second per call.

    Every reading then comes out identical, so the baseline-drift check sees
    0.0 % and passes every pair -- and every overhead is exactly 0.00, which
    is the shape the ratio line mishandles. A `date` with no `%N` support does
    NOT reach that line: its readings make drift print the 999.0 % cap and
    the pairs are discarded as drift, which is why this stub counts instead.

    `guard.sh` is unaffected by it either way: it resolves `SG_DATE` by
    absolute path precisely so a shadowed `date` cannot write a timestamp of
    its choosing into an audit record.
    """
    binned = tmp_path / "clockbin"
    binned.mkdir()
    counter = tmp_path / "ns"
    stub = binned / "date"
    # One second in nanoseconds, written as a product so no line here carries
    # a long run of digits for the IP-hygiene gate to read as an identifier.
    stub.write_text(
        "#!/bin/sh\n"
        'f=%s\n'
        'step=$((1000000 * 1000))\n'
        'n=$(cat "$f" 2>/dev/null || echo "$step")\n'
        "n=$((n + step))\n"
        'echo "$n" > "$f"\n'
        'echo "$n"\n' % counter
    )
    stub.chmod(0o755)
    return str(binned)


def test_a_clock_that_did_not_measure_is_refused_rather_than_passed(
        guard, degenerate_clock):
    """The failure this exists to prevent, in the thing meant to prevent it.

    Before the fix this run reported `3/3 usable pairs`, a median of 0.000x
    and `fast path within the 1.05x ceiling`, and exited 0 -- a clean pass for
    a run that measured nothing, because `(r > 0 ? c/r : 0)` folds a
    non-positive reference in as an ordinary 0.000 that clears any ceiling.

    What this does NOT reproduce: the other route to the same line, where
    ordinary noise at a low N puts a shim reading under its own baseline. That
    is closed by the same condition and has not been observed in repeated
    runs at WARMUP=0/N=1, so it is covered by construction and by no test,
    and saying otherwise would be a coverage claim the test cannot back.
    """
    env = {"PATH": degenerate_clock + os.pathsep + os.environ["PATH"]}
    r = run_measure(["--against", guard, guard, "1", "3", "1.05", "1.05"],
                    env=env)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "an overhead at or below zero" in r.stdout
    # "baseline drift" alone would match the per-run table row that every
    # reading prints; the DISCARDED prefix is what names the refusal.
    assert "DISCARDED: baseline drift" not in r.stdout, \
        "refused for the wrong reason"
    assert "within the" not in r.stdout, "it reported a pass"


def test_an_even_number_of_pairs_takes_the_mean_of_the_middle_two(ratio_clock):
    """Every other `--against` test passes PAIRS=3, so `median()`'s even
    branch was exercised by nothing -- the harness could not emit the shape.

    The candidate's fast ratios are 3.000, 1.200, 2.000 and 1.500, so the
    median is 1.750. An odd-count median reads 1.500, one taken before
    sorting reads 1.600, and a mean of all four 1.925."""
    cand = [(3000, 1300, 10000), (1200, 1300, 10000),
            (2000, 1300, 10000), (1500, 1300, 10000)]
    ref, cand, env = ratio_clock(REF_PAIRS * 2, cand)
    r = run_measure(["--against", ref, cand, "1", "4", "100", "100"], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "=== 4/4 usable pairs ===" in r.stdout.splitlines()
    assert re.search(r"^fast-path ratio \(median\) +1\.750 x$", r.stdout, re.M), \
        r.stdout


def test_a_run_with_too_few_usable_pairs_fails_as_unmeasurable(guard):
    """A machine too busy to measure on must produce an error, not a median of
    whatever survived. The threshold is set to -1 rather than 0 because a pair
    whose two baselines read *identically* has 0.0 % drift and would otherwise
    survive, which is not the behaviour under test."""
    r = run_measure(["--against", guard, guard, "1", "3", "1.5"],
                    env={"WALK_BLOCKER_MEASURE_DRIFT_PCT": "-1"})
    # 3, not the gate failure's 1: a caller reading only the status must be
    # able to tell "compared nothing" from "got slower" (issue #63).
    assert r.returncode == 3, r.stdout + r.stderr
    assert "only 0 pairs survived" in r.stderr
    assert "statement about the machine" in r.stderr


def test_a_reference_without_its_exec_bit_says_so(guard, rendered_shim, tmp_path):
    """`git show BASE:path/to/guard.sh > ref.sh` lands mode 0644, so this is
    the first thing anyone wiring the gate into CI will hit. It must read as a
    missing exec bit and not as a measurement."""
    ref = tmp_path / "ref.sh"
    ref.write_text(rendered_shim["guard_text"])
    ref.chmod(0o644)
    r = run_measure(["--against", str(ref), guard, "1", "3", "1.5"])
    assert r.returncode == 1, r.stdout + r.stderr
    assert "did not reach the binary behind it" in r.stderr
    assert "executable" in r.stderr


def test_asking_for_fewer_pairs_than_the_gate_needs_is_refused_up_front(guard):
    """Not just refused -- refused *before* the benchmark runs, and with an
    exit code that separates a bad argument from a slow shim. The message this
    replaces blamed the machine and told the operator to re-run when it was
    quieter, which a PAIRS=2 request will never fix."""
    r = run_measure(["--against", guard, guard, "1", "2", "1.5"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "PAIRS=2 is below the 3 pairs" in r.stderr
    assert "machine has nothing to do with this one" in r.stderr
    assert "ms/call" not in r.stdout, "it measured something first"


# --------------------------------------------------------------------------
# the slow-filesystem refusal, driven by a fixture mount table
# --------------------------------------------------------------------------
#
# measure.sh reads its mount table through WALK_BLOCKER_MEASURE_MOUNTS so the
# suite can describe the directory the rendered guard sits in as a parallel
# filesystem without needing one to point at. The table is a FIXTURE in
# /proc/mounts field order (ADR-0014): the type names are vocabulary, the
# device names are made up, and the only real path in it is the tmp directory
# under test.

def _mount_table(tmp_path, subject):
    """A mount table that puts `subject`'s directory on a wekafs mount and
    everything else on a local root."""
    table = tmp_path / "mounts"
    table.write_text(
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "fast %s wekafs rw,relatime 0 0\n" % os.path.dirname(subject))
    return {"WALK_BLOCKER_MEASURE_MOUNTS": str(table)}


@pytest.fixture
def slow_table(tmp_path, guard):
    """The table for runs whose subject is the plain rendered guard."""
    return _mount_table(tmp_path, guard)


@pytest.fixture
def spun_table(tmp_path, slow_guard):
    """The same table, naming the SPUN guard instead.

    The two guards live in different temporary directories, so a run measuring
    `slow_guard` against `slow_table` would find its subject on the local root
    rather than on the wekafs entry -- and the two tests below, which exist to
    show the filesystem check letting an unlisted type through, would pass
    without the check ever having something to let through. Vacuous, and green.
    """
    return _mount_table(tmp_path, slow_guard)


def test_a_guard_on_an_expensive_filesystem_is_refused_before_timing(guard, slow_table):
    """The defect this exists for, measured at a reference deployment: the
    SAME guard.sh timed roughly twice as slow per call from a clone on the
    parallel filesystem as from local disk. The slow reading failed a budget
    and reported "the fast path grew". Nothing had grown -- every iteration
    re-read the script over the filesystem this project exists because it is
    slow.

    The default slow list is what refuses here: wekafs is on it, and no
    override is passed."""
    r = run_measure([guard, "1", "1000", "1000"], env=slow_table)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "wekafs" in r.stderr and "expensive operation" in r.stderr
    assert guard in r.stderr
    assert "ms/call" not in r.stdout, "it timed something before refusing"


def test_the_reference_guard_is_checked_too(guard, rendered_shim, slow_table, tmp_path):
    """A ratio run compares the INSTALLED guard on local disk against a new one
    in the operator's clone, so the asymmetry hides in whichever side sits on
    the slow filesystem. Both sides are checked: here the candidate is a copy
    on the local root and only the reference is on the slow mount, so a check
    that looked at the candidate alone would pass."""
    cand = tmp_path / "cand" / "guard.sh"
    cand.parent.mkdir()
    cand.write_text(rendered_shim["guard_text"])
    cand.chmod(0o755)
    r = run_measure(["--against", guard, str(cand), "1", "3", "1.5"], env=slow_table)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "expensive operation" in r.stderr
    assert guard in r.stderr and str(cand) not in r.stderr


def test_a_filesystem_not_on_the_list_is_fine(slow_guard, spun_table):
    """The inverse -- otherwise the two tests above would pass against a check
    that refused everything. Same table, a list that does not name wekafs.

    Reaches a real reading, so it takes the spun guard and the table that
    names it; see `slow_guard`."""
    r = run_measure([slow_guard, "1", "1000", "1000"],
                    env=dict(NO_DISCARD, WALK_BLOCKER_MEASURE_SLOW_FSTYPES="nosuchfs",
                             **spun_table))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "within the 1000 ms budget" in r.stdout


def test_the_filesystem_check_can_be_disabled(slow_guard, spun_table):
    """For a machine where every filesystem is one of these."""
    r = run_measure([slow_guard, "1", "1000", "1000"],
                    env=dict(NO_DISCARD, WALK_BLOCKER_MEASURE_SLOW_FSTYPES="",
                             **spun_table))
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_drifting_single_guard_run_refuses_instead_of_judging_the_shim(slow_guard):
    """What a node hit first: baseline drift of a fifth, and the run reported
    "shim overhead exceeds the budget" -- a verdict about the code, from a
    reading whose own two measurements of the same bare binary disagreed by
    that much.

    Exit 2, with the usage errors rather than the gate failures: "this
    measured nothing" is not "the shim got slower", and the difference has to
    survive being read by something other than a human.

    Takes the spun guard because positivity is checked BEFORE drift: against
    the plain guard a contended run can refuse with "an overhead came back at
    or below zero" instead, failing this test for the wrong reason while it
    appears to be about drift."""
    r = run_measure([slow_guard, "1", "1000", "1000"],
                    env={"WALK_BLOCKER_MEASURE_DRIFT_PCT": "-1"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "disagree" in r.stderr
    assert "not a statement about the shim" in r.stderr.replace("\n", " ") \
        or "Nothing here is a statement about the shim" in r.stderr
    assert "budget" not in r.stderr, "it judged the shim anyway"


def test_a_non_numeric_n_is_refused_before_anything_is_timed(guard):
    """`N` reaches `[ "$_i" -lt "$N" ]`, which in dash prints "Illegal number"
    and runs the loop zero times, and then `awk -v n="abc"` divides by zero
    and dies -- mid-run, after the warmup has already been paid."""
    r = run_measure([guard, "abc", "1000", "1000"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "N must be a whole number, not 'abc'" in r.stderr
    assert "ms/call" not in r.stdout, "it measured something first"


def test_a_zero_n_is_refused_too(guard):
    """`0` is a whole number and still divides by zero."""
    r = run_measure([guard, "0", "1000", "1000"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "N must be at least 1, not '0'" in r.stderr


def test_a_non_numeric_ceiling_is_refused_rather_than_compared_as_a_string(guard):
    """The sharp end of argument validation, and not for the reason first
    written down.

    A bad COUNT dies loudly in dash. A bad CEILING does not die at all: it
    reaches `awk -v b="$CEILING"` in `BEGIN{exit !(o > b)}`. `b+0` is 0, which
    makes "awk coerces it and the gate fires on everything" feel settled -- but
    the comparison never goes through arithmetic. `b` is not a numeric string,
    so `o > b` compares as STRINGS and the answer turns on the first byte:
    `abc`, `3.o` and `nan` never fire, `-1` and `1.5x` do.

    Measured on GNU awk. So the real damage is a silent PASS -- a shim that
    regressed reporting "within budget" and exiting 0 -- which is worse than
    firing too often, because that gets fixed."""
    r = run_measure(["--against", guard, guard, "1", "3", "abc"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "MAX_RATIO must be a number, not 'abc'" in r.stderr
    assert "can pass silently" in r.stderr
    assert "ms/call" not in r.stdout


def test_a_non_numeric_budget_is_refused_in_single_guard_mode_too(guard):
    """The same coercion, through the other mode's ceilings."""
    r = run_measure([guard, "1", "not-a-number"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "BUDGET_MS must be a number" in r.stderr


def test_the_env_knobs_are_validated_too(guard):
    """The defect the validator once reproduced inside itself.

    `require_threshold` was added because an unvalidated ceiling reaches
    `awk -v` and compares as a string, passing silently. The same change then
    added a FIFTH threshold -- the floor ceiling -- and did not route it
    through, so a typo produced no refusal AND no discard: an operator who
    believes they turned the floor gate on gets silence.

    All three env knobs carry the same hazard, so all three are checked."""
    for var, name in (("WALK_BLOCKER_MEASURE_FLOOR_PCT", "FLOOR_MAX_PCT"),
                      ("WALK_BLOCKER_MEASURE_DRIFT_PCT", "DRIFT_MAX_PCT")):
        r = run_measure([guard, "1", "1000", "1000"], env={var: "abc"})
        assert r.returncode == 2, (var, r.stdout + r.stderr)
        assert "%s must be a number, not 'abc'" % name in r.stderr
    r = run_measure([guard, "1", "1000", "1000"],
                    env={"WALK_BLOCKER_MEASURE_WARMUP": "q"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "WARMUP must be a whole number, not 'q'" in r.stderr


def test_a_negative_threshold_is_still_accepted(slow_guard):
    """And the inverse, because the validator must not break the idiom it
    shares the file with: a negative threshold means "discard everything",
    which is how the drift and floor gates are driven deliberately. It fires
    always -- noisy rather than silent, and noisy gets fixed.

    Named for the validator rather than for a discard on purpose. The `-1`
    usages elsewhere are tests of the discard MECHANISM and pass through the
    validator incidentally; if someone later tightens the charset, those fail
    with a message about drift and the next reader looks in the wrong
    function.

    The trap this pins is the empty case. `''` means "gate not enabled", so it
    returns early -- and if a stripped-empty value reached that branch, a bare
    `-` would read as "not enabled" and silently disable the gate, which is
    the silent-pass defect reproduced inside its own fix. Emptiness is tested
    before stripping."""
    r = run_measure([slow_guard, "1", "1000", "1000"],
                    env=dict(NO_DISCARD, WALK_BLOCKER_MEASURE_FLOOR_PCT="-1"))
    assert r.returncode == 0, r.stdout + r.stderr
    for bad in ("-", "--1", "-.", "1-2"):
        r = run_measure([slow_guard, "1", "1000", "1000"],
                        env={"WALK_BLOCKER_MEASURE_DRIFT_PCT": bad})
        assert r.returncode == 2, (bad, r.stdout + r.stderr)
        assert "must be a number, not '%s'" % bad in r.stderr, bad


def test_a_warmup_of_zero_is_a_legitimate_ask(guard):
    """WARMUP=0 skips the warmup, which probe runs do on purpose -- so the
    count validator takes a floor per argument rather than one hardcoded 1.
    N=0 still divides by zero and is still refused."""
    r = run_measure([guard, "1", "1000", "1000"],
                    env=dict(NO_DISCARD, WALK_BLOCKER_MEASURE_WARMUP="0"))
    assert "must be at least" not in r.stderr, r.stderr


def test_a_decimal_ceiling_is_accepted(slow_guard):
    """The inverse: the real ones are decimals -- a budget in ms with a
    fraction, a ratio like 1.25x -- and a validator that rejected them would
    be worse than none."""
    r = run_measure([slow_guard, "1", "1000.5", "1000.5"], env=NO_DISCARD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "within the 1000.5 ms budget" in r.stdout


@pytest.fixture
def scripted_clock(tmp_path):
    """A `date` whose every bench reads a duration chosen in advance.

    Calls alternate start, end, start, end, one pair per `bench()`. A start
    call advances the clock by 1 ms and an end call by the next duration in the
    list, in microseconds, so at N=1 each bench's reading is that duration
    exactly. Single-guard mode benches eight times, in this order: baseline,
    shim, two python floors, baseline again, guarded baseline, guarded with
    one operand, guarded with ten. The list gives the shim overhead as
    1.25 ms, the guarded overhead as 1.35 ms and the drift as 8.0 %, under
    the 12 % ceiling the test sets, so the budget gates are the only thing
    left to decide the run.

    Every value that feeds a gate is distinct on purpose. With equal
    overheads, a gate reading the other gate's overhead would pass; with
    equal baselines, an overhead taken from the wrong baseline would too.
    """
    return _scripted_date(tmp_path, "1000 2250 1000 1000 1080 1500 2850 3750")


def _scripted_date(tmp_path, durations):
    """The `date` stub behind `scripted_clock`, reading `durations` (bench
    readings in microseconds, space-separated, in bench order)."""
    binned = tmp_path / "clockbin"
    binned.mkdir()
    counter = tmp_path / "calls"
    stub = binned / "date"
    stub.write_text(
        "#!/bin/sh\n"
        "set -- %s\n"
        "k=0; t=0; [ ! -f %s ] || read -r k t < %s\n"
        "if [ $((k %% 2)) -eq 1 ]; then\n"
        '    eval "d=\\${$((k / 2 + 1)):-1000}"\n'
        "    t=$((t + d * 1000))\n"
        "else\n"
        "    t=$((t + 1000 * 1000))\n"
        "fi\n"
        'echo "$((k + 1)) $t" > %s\n'
        'echo "$t"\n' % (durations, counter, counter, counter)
    )
    stub.chmod(0o755)
    return str(binned)


@pytest.mark.parametrize("budget, guarded_budget, rc, says", [
    ("1.3", "1.4", 0, ["within the 1.3 ms budget",
                       "guarded path within the 1.4 ms budget"]),
    ("1.2", "1000", 1, ["measure.sh: shim overhead 1.25 ms exceeds the"]),
    ("1000", "1.3", 1, ["measure.sh: guarded overhead 1.35 ms exceeds the"]),
    ("1.25", "1.35", 0, ["within the 1.25 ms budget",
                         "guarded path within the 1.35 ms budget"]),
], ids=["both-clear", "fast-refuses", "guarded-refuses", "at-both-budgets"])
def test_a_decimal_budget_is_compared_as_a_decimal(
        guard, scripted_clock, budget, guarded_budget, rc, says):
    """`test_a_decimal_ceiling_is_accepted` proves the validator lets a
    decimal through, and its pass line echoes the argument verbatim -- so a
    gate that truncated or rounded either side inside the `awk` comparison
    would still pass it. Here the overheads are known, 1.25 fast and 1.35
    guarded: each clears its 1.3 or 1.4 budget only if the budget keeps its
    fraction (1 would refuse), and exceeds 1.2 or 1.3 only if the overhead
    keeps its own (1 would pass). Each gate gets both directions.

    `at-both-budgets` sits on each gate's boundary (issue #148): a refusal
    says the overhead "exceeds" the budget, so an overhead equal to it is
    within it, and a `>=` in either gate refuses this case.

    The two 1.3 budgets -- fast in `both-clear`, guarded in
    `guarded-refuses` -- sit between the two overheads, so a gate that read
    the other gate's overhead decides those two cases the other way."""
    # The ceiling is set rather than inherited, so the 8.0 % scripted drift is
    # under it by construction and not by whatever the default is today.
    env = {"PATH": scripted_clock + os.pathsep + os.environ["PATH"],
           "WALK_BLOCKER_MEASURE_DRIFT_PCT": "12"}
    r = run_measure([guard, "1", budget, guarded_budget], env=env)
    # The clock under test is the scripted one, not the machine's.
    for row, value in (("shim overhead", r"1\.25 ms/call"),
                       ("guarded overhead", r"1\.35 ms/call"),
                       ("baseline drift", r"8\.0 %")):
        assert re.search(r"^%s +%s$" % (row, value), r.stdout, re.M), r.stdout
    assert r.returncode == rc, r.stdout + r.stderr
    # Issue #222: the clock stub's first call reads no counter file, quietly.
    assert STUB_NOISE not in r.stderr, r.stderr
    lines = (r.stderr if rc else r.stdout).splitlines()
    for line in says:
        assert line in lines, r.stdout + r.stderr


@pytest.mark.parametrize("durations, row, other", [
    ("1000 1000 1000 1000 1080 1500 2850 3750",
     ("shim overhead", r"0\.00"), ("guarded overhead", r"1\.35")),
    ("1000 2250 1000 1000 1080 1500 1500 3750",
     ("guarded overhead", r"0\.00"), ("shim overhead", r"1\.25")),
], ids=["shim-at-zero", "guarded-at-zero"])
def test_an_overhead_of_exactly_zero_is_refused(
        guard, tmp_path, durations, row, other):
    """Issue #148. The positivity discard refuses "an overhead at or below
    zero", so an overhead of exactly 0.00 ms is refused, with the other
    overhead positive so that only the one comparison decides. Mutation:
    `f > 0` or `g > 0` to `>=`, and the zero overhead clears its budget and
    the run exits 0."""
    env = {"PATH": _scripted_date(tmp_path, durations) + os.pathsep
           + os.environ["PATH"], "WALK_BLOCKER_MEASURE_DRIFT_PCT": "12"}
    r = run_measure([guard, "1", "1000", "1000"], env=env)
    for name, value in (row, other):
        assert re.search(r"^%s +%s ms/call$" % (name, value), r.stdout,
                         re.M), r.stdout
    assert r.returncode == 2, r.stdout + r.stderr
    assert "at or below zero" in r.stderr, r.stderr
    assert "within the" not in r.stdout, "it reported a pass"


def test_drift_exactly_at_its_ceiling_is_not_over_it(guard, scripted_clock):
    """Issue #148. The drift discard is strict: the refusal says the two
    baseline readings disagree by "over" the ceiling. The scripted drift is
    exactly 8.0 %, and an 8 % ceiling lets the run through to its budgets.
    Mutation: `(d>m)` to `(d>=m)`, and the run is refused with exit 2."""
    env = {"PATH": scripted_clock + os.pathsep + os.environ["PATH"],
           "WALK_BLOCKER_MEASURE_DRIFT_PCT": "8"}
    r = run_measure([guard, "1", "1000", "1000"], env=env)
    assert re.search(r"^baseline drift +8\.0 %$", r.stdout, re.M), r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    assert "disagree" not in r.stderr, r.stderr


def test_a_guard_that_does_not_exist_says_so_rather_than_blaming_the_mode(
        tmp_path):
    """`abspath()` built `/<basename>` out of a failed `cd`, so a mistyped
    path became a plausible-looking absolute one, the symlink farm pointed at
    nothing, and the run failed with the exec-bit diagnosis -- sending the
    reader to `chmod` a file that is not there.

    The mode failure is a real and common one (`git show` does not preserve
    it), which is exactly why the two must not share a message."""
    missing = str(tmp_path / "nope" / "guard.sh")
    r = run_measure([missing, "1", "1000", "1000"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "does not exist" in r.stderr
    assert missing in r.stderr
    assert "did not reach the binary behind it" not in r.stderr, \
        "diagnosed as the exec bit"


def test_a_missing_reference_is_named_as_the_reference(guard, tmp_path):
    """And says WHICH of the two, since --against takes both."""
    missing = str(tmp_path / "gone.sh")
    r = run_measure(["--against", missing, guard, "1", "3", "1.5"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "reference guard does not exist" in r.stderr


def test_single_guard_mode_refuses_a_clock_that_did_not_measure(
        guard, degenerate_clock):
    """Under a clock that returns the same value every call, both overheads
    are exactly 0.00 and both budgets were cleared -- a pass reported for a
    run that measured nothing.

    The check has to come BEFORE the drift refusal: identical readings make
    drift read as the 999 cap, so the drift message would fire first and
    say the two readings disagree by 999.0 % when they agree exactly."""
    env = {"PATH": degenerate_clock + os.pathsep + os.environ["PATH"]}
    r = run_measure([guard, "1", "1000", "1000"], env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "at or below zero" in r.stderr
    assert "disagree" not in r.stderr, "refused for the wrong reason"
    assert "within the" not in r.stdout, "it reported a pass"


def test_every_pair_reports_how_far_apart_the_halves_were(ratio_clock):
    """Each half already measured a `python3 -S` floor and once threw it
    away. It measures no part of the shim -- it is that half's reading of how
    fast the machine was -- and at a reference deployment the per-pair ratio
    tracked it almost exactly, which means a pair whose halves disagree is
    comparing two machines."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _lines(r.stdout, "pair ") == [
        "pair 1 ratio: fast 1.100x (1.00 -> 1.10 ms), guarded 1.300x"
        " (1.00 -> 1.30 ms), halves 0.0% apart on the machine",
        "pair 2 ratio: fast 1.500x (1.00 -> 1.50 ms), guarded 1.250x"
        " (1.00 -> 1.25 ms), halves 25.0% apart on the machine",
        "pair 3 ratio: fast 1.200x (1.00 -> 1.20 ms), guarded 1.400x"
        " (1.00 -> 1.40 ms), halves 10.0% apart on the machine",
    ], r.stdout
    assert re.search(r"^fast-path ratio \(spread\) +1\.100-1\.500$",
                     r.stdout, re.M), r.stdout
    # Issue #222: the clock stub's first call reads no counter file, quietly.
    assert STUB_NOISE not in r.stderr, r.stderr


def test_the_floor_discard_is_off_unless_asked_for(ratio_clock):
    """Reporting is the default; discarding is opt-in. Any ceiling tight
    enough to catch the mismatch seen at a reference deployment would have
    thrown away half of that run's pairs and failed a deploy that should have
    passed, so the number is left to be chosen from a record rather than
    guessed from one run. Pair 2's halves are a quarter apart here, and it
    still counts."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "=== 3/3 usable pairs ===" in r.stdout.splitlines()
    assert "about how fast the machine is" not in r.stdout


def test_the_floor_discard_fires_when_it_is_asked_for(ratio_clock):
    """A 20 % ceiling discards pair 2, whose halves are 25.0 % apart, and
    keeps pairs 1 and 3, at 0.0 % and 10.0 % -- so the discard is pinned to
    the pair over the ceiling and not to every pair, which is all a negative
    ceiling could show. Two survivors is below the three the gate needs."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"],
                    env=dict(env, WALK_BLOCKER_MEASURE_FLOOR_PCT="20"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert _lines(r.stdout, "pair 2 DISCARDED") == [
        "pair 2 DISCARDED: the two halves disagree by 25.0 % about how fast"
        " the machine is (floors 10.00 vs 12.50 ms), over the 20 % allowed"
        " -- a ratio between them would be measuring the machine"], r.stdout
    assert len(_lines(r.stdout, "pair 1 ratio:")) == 1, r.stdout
    assert len(_lines(r.stdout, "pair 3 ratio:")) == 1, r.stdout
    assert "only 2 pairs survived" in r.stderr


def test_a_ratio_equal_to_its_ceiling_is_within_it(ratio_clock):
    """Issue #148. Both ratio gates are strict: a refusal says the median
    "exceeds" the ceiling, so a median equal to it passes. The medians are
    exactly 1.200 and 1.300 here, and the ceilings are those same numbers.
    Mutation: `o > b` to `o >= b` in either gate, and that gate refuses."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "1.2", "1.3"], env=env)
    assert re.search(r"^fast-path ratio \(median\) +1\.200 x$", r.stdout,
                     re.M), r.stdout
    assert re.search(r"^guarded ratio \(median\) +1\.300 x$", r.stdout,
                     re.M), r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fast path within the 1.2x ceiling" in r.stdout.splitlines()
    assert "guarded path within the 1.3x ceiling" in r.stdout.splitlines()


def test_halves_exactly_the_floor_allowance_apart_are_kept(ratio_clock):
    """Issue #148. The floor discard is strict: a pair is discarded when its
    halves are "over" the allowance. Pair 2's halves are exactly 25.0 % apart,
    so a 25 % allowance keeps all three pairs. Mutation: `d > m` to `d >= m`,
    and pair 2 is discarded, leaving two pairs and exit 3."""
    ref, cand, env = ratio_clock(REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"],
                    env=dict(env, WALK_BLOCKER_MEASURE_FLOOR_PCT="25"))
    assert "halves 25.0% apart on the machine" in r.stdout, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    assert "=== 3/3 usable pairs ===" in r.stdout.splitlines()
    assert "DISCARDED" not in r.stdout, r.stdout


# The reference half's `python3 -S` floor reads exactly 0.00 on every pair, and
# everything else reads as in REF_PAIRS, so each pair survives the positivity
# and drift checks and reaches the floor line with a zero denominator.
ZERO_FLOOR_REF_PAIRS = [(1000, 1000, 0)] * 3


def test_a_zero_reference_floor_reports_the_sentinel(ratio_clock):
    """Issue #197. The floor comparison divides by the reference half's
    floor, which the positivity check does not read, so a reference floor of
    0.00 reaches it on a pair that is otherwise kept. The guard prints the
    999 sentinel in place of the division. Mutation: `r > 0` to `r >= 0` on
    that line, and awk divides by zero instead of printing 999.0 -- gawk
    stops with an error and the percentage comes back empty."""
    ref, cand, env = ratio_clock(ZERO_FLOOR_REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [ln.rsplit(", halves ", 1)[1] for ln in _lines(r.stdout, "pair ")] \
        == ["999.0% apart on the machine"] * 3, r.stdout


def test_a_zero_reference_floor_is_discarded_by_the_floor_check(ratio_clock):
    """Issue #197, the consumer of the same sentinel: with a 20 % floor
    allowance, 999.0 % is over it and every pair is discarded, naming the
    sentinel and the zero floor. The sentinel is a finite number, not an
    infinity, so an allowance of 999 or more would keep these pairs, and
    measure.sh refuses one (issue #223, tested below). Under the `r >= 0`
    mutant gawk leaves the percentage empty, the discard never fires, and
    the run exits 0."""
    ref, cand, env = ratio_clock(ZERO_FLOOR_REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"],
                    env=dict(env, WALK_BLOCKER_MEASURE_FLOOR_PCT="20"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert _lines(r.stdout, "pair 1 DISCARDED") == [
        "pair 1 DISCARDED: the two halves disagree by 999.0 % about how fast"
        " the machine is (floors 0.00 vs 10.00 ms), over the 20 % allowed"
        " -- a ratio between them would be measuring the machine"], r.stdout
    assert len(_lines(r.stdout, "pair ")) == 3, r.stdout
    assert all("disagree by 999.0 %" in ln for ln in _lines(r.stdout, "pair ")), \
        r.stdout
    assert "only 0 pairs survived" in r.stderr


@pytest.mark.parametrize("var, name", [
    ("WALK_BLOCKER_MEASURE_FLOOR_PCT", "FLOOR_MAX_PCT"),
    ("WALK_BLOCKER_MEASURE_DRIFT_PCT", "DRIFT_MAX_PCT"),
], ids=["floor", "drift"])
@pytest.mark.parametrize("allowance", ["1000", "999", "999.0"])
def test_an_allowance_at_or_over_the_cap_is_refused(
        ratio_clock, var, name, allowance):
    """Issue #223. Both percentages stop at 999, and 999 is also what a
    reading that measured nothing prints, so an allowance of 999 or more
    would keep exactly those pairs. The reproduction from the issue -- a zero
    reference floor and a floor allowance of 1000 -- kept every pair and
    exited 0; it is now refused, exit 2, before anything is timed. 999 itself
    is refused too, since a pair at the cap is not over it. Mutation: `>=`
    to `>` in `require_below_cap`, and the 999 cases run."""
    ref, cand, env = ratio_clock(ZERO_FLOOR_REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"],
                    env=dict(env, **{var: allowance}))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "measure.sh: %s must be below 999, not '%s'." % (name, allowance) \
        in r.stderr.splitlines(), r.stderr
    assert "ms/call" not in r.stdout, "it measured something first"


def test_the_largest_floor_allowance_still_discards_a_zero_floor(ratio_clock):
    """Issue #223. 998 is accepted, and a reference floor of 0.00 reads as
    the cap, which is over it: every pair is discarded, so the floor check,
    once on, can never keep a pair whose reference measured nothing."""
    ref, cand, env = ratio_clock(ZERO_FLOOR_REF_PAIRS, CAND_PAIRS)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"],
                    env=dict(env, WALK_BLOCKER_MEASURE_FLOOR_PCT="998"))
    assert r.returncode == 3, r.stdout + r.stderr
    discards = _lines(r.stdout, "pair ")
    assert len(discards) == 3, r.stdout
    assert all("DISCARDED: the two halves disagree by 999.0 %" in ln
               and "over the 998 % allowed" in ln for ln in discards), r.stdout
    assert "only 0 pairs survived" in r.stderr


def test_a_real_floor_disagreement_over_the_cap_prints_the_cap(ratio_clock):
    """Issue #223. A candidate floor eleven times the reference's is
    1000.0 % apart, just over the cap; with no allowance set it is reported,
    as the cap, 999.0, the same as a floor that measured nothing. The next
    test discards it. Mutation: drop the cap, or cap only above 1000, and
    the line reads 1000.0."""
    ref_pairs = [(1000, 1000, 1000)] * 3
    cand_pairs = [(1100, 1300, 11000)] * 3
    ref, cand, env = ratio_clock(ref_pairs, cand_pairs)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [ln.rsplit(", halves ", 1)[1] for ln in _lines(r.stdout, "pair ")] \
        == ["999.0% apart on the machine"] * 3, r.stdout


def test_a_real_floor_disagreement_over_the_cap_is_discarded(ratio_clock):
    """The same pairs as above, under the largest allowance accepted."""
    ref_pairs = [(1000, 1000, 1000)] * 3
    cand_pairs = [(1100, 1300, 11000)] * 3
    ref, cand, env = ratio_clock(ref_pairs, cand_pairs)
    r = run_measure(["--against", ref, cand, "1", "3", "100", "100"],
                    env=dict(env, WALK_BLOCKER_MEASURE_FLOOR_PCT="998"))
    assert r.returncode == 3, r.stdout + r.stderr
    assert _lines(r.stdout, "pair 1 DISCARDED") == [
        "pair 1 DISCARDED: the two halves disagree by 999.0 % about how fast"
        " the machine is (floors 1.00 vs 11.00 ms), over the 998 % allowed"
        " -- a ratio between them would be measuring the machine"], r.stdout
    assert "only 0 pairs survived" in r.stderr


def test_a_real_drift_over_the_cap_prints_the_cap_and_is_refused(tmp_path, guard):
    """Issue #223, the drift percentage. The second baseline reads eleven
    times the first, 1000.0 % apart and just over the cap; it prints as the
    cap, 999.0, and the largest drift allowance accepted, 998, refuses the
    run as drift with exit 2. Both overheads are positive, so drift is what
    decides it. Mutation: drop the cap, or cap only above 1000, and the
    table row reads 1000.0."""
    env = {"PATH": _scripted_date(tmp_path,
                                  "1000 2250 1000 1000 11000 1500 2850 3750")
           + os.pathsep + os.environ["PATH"],
           "WALK_BLOCKER_MEASURE_DRIFT_PCT": "998"}
    r = run_measure([guard, "1", "1000", "1000"], env=env)
    assert re.search(r"^baseline drift +999\.0 %$", r.stdout, re.M), r.stdout
    assert r.returncode == 2, r.stdout + r.stderr
    assert "  by 999.0 %, over the 998 % this gate allows." \
        in r.stderr.splitlines(), r.stderr
    assert "within the" not in r.stdout, "it reported a pass"


def test_a_zero_baseline_reads_as_the_drift_cap(tmp_path, guard):
    """Issue #223. A first baseline that printed 0.00 leaves nothing to
    divide by, and drift reads as the cap, 999.0: "not measured", which the
    largest allowance accepted still refuses. Both overheads are positive,
    so the positivity check lets the run reach drift. Mutation: read the
    zero denominator as 0 instead of the cap, and the run passes its
    budgets and exits 0."""
    env = {"PATH": _scripted_date(tmp_path,
                                  "0 1250 1000 1000 0 1500 2850 3750")
           + os.pathsep + os.environ["PATH"],
           "WALK_BLOCKER_MEASURE_DRIFT_PCT": "998"}
    r = run_measure([guard, "1", "1000", "1000"], env=env)
    assert re.search(r"^baseline drift +999\.0 %$", r.stdout, re.M), r.stdout
    assert re.search(r"^shim overhead +1\.25 ms/call$", r.stdout, re.M), \
        r.stdout
    assert r.returncode == 2, r.stdout + r.stderr
    assert "  by 999.0 %, over the 998 % this gate allows." \
        in r.stderr.splitlines(), r.stderr


def test_exactly_the_minimum_of_usable_pairs_is_compared(ratio_clock):
    """Issue #134. `test_the_floor_discard_fires_when_it_is_asked_for` pins
    that two survivors, one fewer than MIN_USABLE_PAIRS, are refused. This is
    the other side: four pairs, pair 2 discarded by a 20 % allowance, and the
    three that survive -- exactly the minimum -- are compared, their median
    taken over the survivors only (1.100, 1.200, 1.300: median 1.200; pair
    2's 1.500 would move it). Mutation: refuse at `-le` instead of `-lt`, or
    raise the minimum to four, and this run is refused with exit 3."""
    ref, cand, env = ratio_clock(REF_PAIRS + REF_PAIRS[:1],
                                 CAND_PAIRS + [(1300, 1350, 10000)])
    r = run_measure(["--against", ref, cand, "1", "4", "100", "100"],
                    env=dict(env, WALK_BLOCKER_MEASURE_FLOOR_PCT="20"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "=== 3/4 usable pairs ===" in r.stdout.splitlines()
    assert len(_lines(r.stdout, "pair 2 DISCARDED")) == 1, r.stdout
    assert _lines(r.stdout, "per-pair fast ratios:") == \
        ["per-pair fast ratios: 1.100 1.200 1.300"], r.stdout
    assert re.search(r"^fast-path ratio \(median\) +1\.200 x$", r.stdout,
                     re.M), r.stdout


def test_against_without_a_reference_is_a_usage_error():
    r = run_measure(["--against"])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "usage:" in r.stderr


def test_the_absolute_budget_still_gates_in_single_guard_mode(slow_guard):
    """The ratio gate does not replace this one. A 0 ms budget is exceeded by
    any shim at all, which is the point: the arithmetic is what is under test,
    not the shim's speed.

    And because the shim's speed is not the subject, the subject may as well
    be a shim that is unambiguously slow -- which is what stops a contended
    runner turning this into the positivity refusal instead (#47)."""
    r = run_measure([slow_guard, "1", "0", "0"], env=NO_DISCARD)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "exceeds the" in r.stderr and "0 ms budget" in r.stderr
    # Both budgets are 0, and the guarded gate would refuse with the same exit
    # and the same three fragments if the fast gate never fired. Only the fast
    # gate puts "shim overhead" on STDERR; the table row of that name goes to
    # stdout.
    assert "shim overhead" in r.stderr, "the guarded gate gated, not the fast one"


def test_a_budget_the_shim_clears_passes(slow_guard):
    r = run_measure([slow_guard, "1", "1000", "1000"], env=NO_DISCARD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "within the 1000 ms budget" in r.stdout
    assert "guarded path within the 1000 ms budget" in r.stdout


def test_the_benchmark_carries_no_placeholder_and_runs_under_both_shells():
    """measure.sh is copied into the payload verbatim, so it must carry no
    `@@` for the build's placeholder check to trip on, and it runs on the
    node under whichever shell is `/bin/sh` there (ADR-0015)."""
    for name in ("measure.sh", "measure-flags.sh"):
        path = os.path.join(ROOT, "node", "shim", name)
        with open(path) as fh:
            assert "@@" not in fh.read(), name
        for shell in sorted({SHIM_SH, shutil.which("bash") or "bash"}):
            r = subprocess.run([shell, "-n", path], capture_output=True, text=True)
            assert r.returncode == 0, (shell, name, r.stderr)
