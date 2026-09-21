"""The benchmark's gates, driven by a shim that is slower on purpose.

`measure.sh` is the only thing standing between a `guard.sh` regression and a
node where that script runs in front of every grep, find and du for every
user, and until this file nothing exercised it -- it was linted by CI and
executed by nobody. The standard the benchmark applies to the shim applies to
the gate as much as to the number it guards: a performance claim nothing
enforces is a comment, not a budget.

**Nothing here asserts on a timing.** The readings are meaningless at the
warmup and N these tests use, and that is deliberate: what is under test is the
gate arithmetic, the pair bookkeeping, and the ways a run is refused. The
oracle for "slower" is a copy of the rendered shim with a spin loop welded into
it, so the direction of the comparison is known before the clock is read.

The shim under test is RENDERED by the real renderer from the fixture policy
and the fixture site (`rendered_shim` in conftest), not read from a checked-in
file: the tree carries the template and the rule table, and `guard.sh` is a
build product (ADR-0013). Its compiled mount table is the fixture's, so the
guarded bench's operands under the tmp directory are judged cheap on every
machine and the allowed path is the one timed.
"""

import os
import shutil
import subprocess

import pytest

from conftest import ROOT, SHIM_SH

MEASURE_SH = os.path.join(ROOT, "node", "shim", "measure.sh")

# Enough spin to dominate the measurement noise at any load. At a few
# microseconds per iteration in dash this adds several ms to a fast path that
# costs a low single-digit number of ms, so the candidate is unambiguously
# slower without the test ever saying by how much.
SPIN = 5000

# The real readings use the script's default warmup and N. These do not,
# because they are not readings.
FAST_ENV = {"WALK_BLOCKER_MEASURE_WARMUP": "1"}

# A drift ceiling chosen so the discard can never fire, for the tests whose
# subject is the ratio rather than the discard -- which has its own test below.
#
# A ceiling that looks generous -- 95 % -- is not one: at WARMUP=1 and N=1 two
# single-call readings of the *same* stub binary are not stable to within
# 95 %, and a run was caught where one read over 98 % and failed the ratio
# assertion on the discard path instead. 1000000 is not a proof of
# impossibility either -- measure.sh computes drift as
# `(a > 0 ? 100*|a-b|/a : 999)`, where the 999 is the degenerate reading of a
# baseline that printed 0.00 and NOT a cap, and the general form is unbounded
# as the baseline approaches zero. It is a bound with a number attached: it
# sits above the degenerate case, and above 100*|a-b|/a for any baseline down
# to 0.01 ms unless the other reading is 100 ms, which a stub that runs `exit
# 0` does not reach.
NO_DISCARD = {"WALK_BLOCKER_MEASURE_DRIFT_PCT": "1000000"}


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
    """The rendered shim, as a path -- the reference in every ratio run and
    the subject of every single-guard one."""
    return rendered_shim["guard"]


@pytest.fixture(scope="module")
def slow_guard(tmp_path_factory, rendered_shim):
    """A copy of the rendered guard.sh that does the same work, slower.

    The spin goes *inside* the copy rather than into a wrapper that execs it,
    because the shim reads its own `$0` to learn which tool it is standing in
    for -- a wrapper would hand it the wrapper's name and measure a different
    code path, or none.

    Used by the ratio tests, and -- since #47 -- by every single-guard test
    whose expected outcome requires the measured overhead to be POSITIVE.
    `measure.sh` refuses, before any gate, when an overhead lands at or below
    zero, because a guarded run no slower than the bare one measured nothing.
    Against the plain guard that refusal is not hypothetical: its real fast
    path costs about the same as one scheduling hiccup, so on a contended
    runner the subtraction can land negative and a test asserting a budget
    verdict gets the refusal instead.

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
    path = tmp_path_factory.mktemp("slow") / "guard.sh"
    lines = rendered_shim["guard_text"].split("\n")
    assert lines[0].startswith("#!"), "guard.sh no longer starts with a shebang"
    spin = (
        "_measure_spin=0\n"
        "while [ \"$_measure_spin\" -lt %d ]; do "
        "_measure_spin=$((_measure_spin + 1)); done" % SPIN
    )
    path.write_text("\n".join([lines[0], spin, *lines[1:]]))
    path.chmod(0o755)
    return str(path)


def test_the_ratio_gate_refuses_a_candidate_that_got_slower(guard, slow_guard):
    """The gate that survives a change of machine: same machine, same minute,
    two shims."""
    r = run_measure(["--against", guard, slow_guard, "1", "3", "1.05"],
                    env=NO_DISCARD)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "exceeds the" in r.stderr and "1.05x ceiling" in r.stderr


def test_a_ceiling_the_candidate_clears_passes(guard, slow_guard):
    """The inverse, on the same pair of shims -- otherwise the test above
    would pass just as well against a gate that always fails. Both ceilings,
    because the guarded gate needs the same inverse and this run is already
    paid for."""
    r = run_measure(["--against", guard, slow_guard, "1", "3", "100", "100"],
                    env=NO_DISCARD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fast path within the 100x ceiling" in r.stdout
    assert "guarded path within the 100x ceiling" in r.stdout


def test_every_pair_charges_the_slowdown_to_the_candidate(guard, slow_guard):
    """Pair 1 runs the reference first and pair 2 runs the candidate first, so
    a ratio computed from the *order* rather than from the identity would come
    out above 1 on odd pairs and below 1 on even ones. With a candidate that is
    slower by construction, every pair must read above 1."""
    r = run_measure(["--against", guard, slow_guard, "1", "3", "100"],
                    env=NO_DISCARD)
    assert r.returncode == 0, r.stdout + r.stderr
    line = [ln for ln in r.stdout.splitlines()
            if ln.startswith("per-pair fast ratios:")]
    assert line, r.stdout
    ratios = [float(x) for x in line[0].split(":")[1].split()]
    assert len(ratios) == 3, line
    assert all(x > 1.0 for x in ratios), ratios


def test_the_comparison_runs_in_the_direction_the_arguments_name(guard, slow_guard):
    """Reference and candidate swapped: the same two files must now read as a
    speed-up, which is the only way to tell the ratio from its reciprocal."""
    r = run_measure(["--against", slow_guard, guard, "1", "3", "100"],
                    env=NO_DISCARD)
    assert r.returncode == 0, r.stdout + r.stderr
    line = [ln for ln in r.stdout.splitlines()
            if ln.startswith("per-pair fast ratios:")]
    ratios = [float(x) for x in line[0].split(":")[1].split()]
    assert all(x < 1.0 for x in ratios), ratios


def test_the_guarded_half_of_the_ratio_gate_refuses_on_its_own(guard, slow_guard):
    """The guarded path is the only one `find`, `du`, `rg`, `fd` and `tree`
    ever take, and until this test nothing exercised its ceiling: mutating the
    guarded comparison so it could never fire left every other test green.

    The fast ceiling is set where it cannot fire and the guarded one where it
    must, so a pass here is the two gates being independent and not a run that
    failed for the other reason."""
    r = run_measure(["--against", guard, slow_guard, "1", "3", "100", "1.01"],
                    env=NO_DISCARD)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "guarded ratio" in r.stderr and "1.01x ceiling" in r.stderr
    assert "fast path within the 100x ceiling" in r.stdout


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
    NOT reach that line: its readings make the drift sentinel print 999 % and
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


def test_an_even_number_of_pairs_takes_the_mean_of_the_middle_two(guard, slow_guard):
    """Every other `--against` test passes PAIRS=3, so `median()`'s even
    branch was exercised by nothing -- the harness could not emit the shape.

    The assertion recomputes the median from the per-pair ratios the run
    printed, rather than hardcoding one, because the ratios themselves are
    timings and only their ordering is stable."""
    r = run_measure(["--against", guard, slow_guard, "1", "4", "100", "100"],
                    env=NO_DISCARD)
    assert r.returncode == 0, r.stdout + r.stderr
    ratios = sorted(
        float(x) for ln in r.stdout.splitlines()
        if ln.startswith("per-pair fast ratios:")
        for x in ln.split(":")[1].split())
    assert len(ratios) == 4, r.stdout
    reported = [ln for ln in r.stdout.splitlines()
                if ln.startswith("fast-path ratio (median)")]
    got = float(reported[0].split()[-2])
    assert abs(got - (ratios[1] + ratios[2]) / 2) < 0.002, (got, ratios)


def test_a_run_with_too_few_usable_pairs_fails_as_unmeasurable(guard):
    """A machine too busy to measure on must produce an error, not a median of
    whatever survived. The threshold is set to -1 rather than 0 because a pair
    whose two baselines read *identically* has 0.0 % drift and would otherwise
    survive, which is not the behaviour under test."""
    r = run_measure(["--against", guard, guard, "1", "3", "1.5"],
                    env={"WALK_BLOCKER_MEASURE_DRIFT_PCT": "-1"})
    assert r.returncode == 1, r.stdout + r.stderr
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
    drift compute as the 999 sentinel, so the drift message would fire first
    and say the two readings disagree by 999 % when they agree exactly."""
    env = {"PATH": degenerate_clock + os.pathsep + os.environ["PATH"]}
    r = run_measure([guard, "1", "1000", "1000"], env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "at or below zero" in r.stderr
    assert "disagree" not in r.stderr, "refused for the wrong reason"
    assert "within the" not in r.stdout, "it reported a pass"


def test_every_pair_reports_how_far_apart_the_halves_were(guard, slow_guard):
    """Each half already measured a `python3 -S` floor and once threw it
    away. It measures no part of the shim -- it is that half's reading of how
    fast the machine was -- and at a reference deployment the per-pair ratio
    tracked it almost exactly, which means a pair whose halves disagree is
    comparing two machines."""
    r = run_measure(["--against", guard, slow_guard, "1", "3", "100", "100"],
                    env=NO_DISCARD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "apart on the machine" in r.stdout
    assert "fast-path ratio (spread)" in r.stdout


def test_the_floor_discard_is_off_unless_asked_for(guard, slow_guard):
    """Reporting is the default; discarding is opt-in. Any ceiling tight
    enough to catch the mismatch seen at a reference deployment would have
    thrown away half of that run's pairs and failed a deploy that should have
    passed, so the number is left to be chosen from a record rather than
    guessed from one run."""
    r = run_measure(["--against", guard, slow_guard, "1", "3", "100", "100"],
                    env=NO_DISCARD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "about how fast the machine is" not in r.stdout


def test_the_floor_discard_fires_when_it_is_asked_for(guard, slow_guard):
    """-1 discards every pair, the same way the drift test drives its own
    threshold, so the mechanism is pinned without depending on a real
    mismatch appearing."""
    r = run_measure(["--against", guard, slow_guard, "1", "3", "100", "100"],
                    env=dict(NO_DISCARD, WALK_BLOCKER_MEASURE_FLOOR_PCT="-1"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "about how fast the machine is" in r.stdout
    assert "only 0 pairs survived" in r.stderr


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
