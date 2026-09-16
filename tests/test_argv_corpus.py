"""A GENERATED argv corpus, judged by the rule table -- and, once the shim
exists, by both consumers and compared.

The hand-written matrix in argv_cases.py carries a row per shape someone
thought to write down, which is what makes it readable and what limits it: it
finds the divergence you predicted. This file is the other half -- a corpus
built by combining flag fragments, so it covers shapes nobody chose.

It exists because two reviews in a row verified the table/shim agreement by
hand with a throwaway script, got `0 mismatches` both times, and left nothing
behind. A check that is re-derived every review and never committed is a check
the next review has to pay for again, and the one after that may simply skip.
The rule this file is the first instance of: a confirmed property becomes an
oracle, not a paragraph in a PR comment.

The corpus is DETERMINISTIC -- a fixed seed, and a fixed fragment table -- so a
failure names an argv that can be pasted into a shell, and so the suite does
not pass or fail depending on the day. Widening it is a matter of adding
fragments, and the `WALK_BLOCKER_CORPUS_SCALE` environment variable raises the
count per tool for a deeper run when someone is hunting.

Until Milestone 4 lands the generated shim, the agreement half is skipped at
collection and only the table half runs: every generated argv must be judged
without error, deterministically, with the two stdin readings differing only
where the grammar says they may.
"""

import os
import random

import pytest

from walk_blocker import search_rules as R
from argv_cases import FAST_CWD
from conftest import resolve_cwd


@pytest.fixture(autouse=True)
def _fixture_home(fixture_home):
    """Every test here judges paths under the fixture `/home`."""


# Fragments, grouped so a generated argv stays close to something a person
# could type. Each group contributes at most one fragment per argv, except
# FLAGS, which contributes several -- that is where the interactions live.
PREFIX_FLAGS = {
    "ugrep": ["-r", "-R", "--recursive", "-d recurse", "-d skip",
              "--directories=dereference-recurse", "-rl", "-l", ""],
    "grep": ["-r", "-R", "--recursive", "-d recurse", "-d skip", ""],
    "rg": ["", "--one-file-system", "--no-one-file-system"],
    "fd": ["", "--one-file-system"],
    "find": ["", "-xdev"],
    "du": ["", "-x"],
    "tree": ["", "-x"],
}

DEPTH_FLAGS = {
    "ugrep": ["", "--depth=2", "--depth=9", "--depth 2", "--depth=1,9",
              "--depth=9,1", "--depth=,5", "--depth=2,", "-2", "-9",
              "-l2", "-l9", "-l10", "-l3-5", "-l9-3", "-l5-", "-l0",
              "--depth=1 --depth=9", "--depth=9 --depth=1", "-lC3", "-lm3"],
    "grep": [""],
    "rg": ["", "--max-depth 2", "--max-depth 9", "--maxdepth 1"],
    "fd": ["", "-d 2", "-d 9", "--max-depth 1"],
    "find": ["", "-maxdepth 2", "-maxdepth 9",
             "-maxdepth 1 -maxdepth 9", "-maxdepth 9 -maxdepth 1"],
    "du": ["", "-d 2", "--max-depth 9"],
    "tree": ["", "-L 2", "-L 9"],
}

NOISE = {
    "ugrep": ["", "-I", "--hidden", "--exclude-dir=.git", "--ignore-files",
              "-e pat", "--", "-t py"],
    "grep": ["", "-I", "--include=*.py", "-e pat", "--", "--color"],
    "rg": ["", "-e pat", "--", "-g *.py"],
    "fd": ["", "-e py", "--", "-x echo {}"],
    "find": ["", "-name x", "--", "-exec echo {} ;"],
    "du": ["", "-sh", "--time"],
    "tree": ["", "-a", "--"],
}

# A flag as the FINAL token, with the value it wants missing. This group is
# here because its absence is a measured blind spot, not a hypothetical: an
# ad-hoc fuzz of many thousand argvs during one review reported zero mismatches
# while `grep -r pat /scratch --di` was diverging the whole time, purely
# because nothing in that corpus ever put a flag last. A generator that cannot
# produce a shape is not evidence about that shape, and "we fuzzed it" reads
# as though it were.
TRAILING = {
    "ugrep": ["", "--di", "--directories", "-d", "--depth", "-e"],
    "grep": ["", "--di", "--dir", "--directo", "--directories", "-d", "-e"],
    "rg": ["", "--max-depth", "-e"],
    "fd": ["", "-d", "--max-depth"],
    "find": ["", "-maxdepth", "-name"],
    "du": ["", "-d", "--max-depth"],
    "tree": ["", "-L"],
}


def _long_flags(profile):
    """Every long-option spelling this profile knows, for ambiguity checks."""
    seen = []
    for group in (profile.rec_flags, profile.value_flags, profile.pat_flags,
                  profile.depth_flags, profile.device_flags,
                  profile.device_off_flags, profile.exact_flags):
        seen.extend(f for f in group if f.startswith("--"))
    seen.extend(f for f, _values in profile.rec_value_flags
                if f.startswith("--"))
    return seen


def _is_abbreviated_directories_last(argv):
    """One known divergence's shape, and deliberately nothing adjacent to it.

    An abbreviated spelling of `--directories` as the final token with no
    value. The table reads the absent value as "skip" (ALLOW) while the shim
    leaves the action at `recurse` (REFUSE). The live behaviour is the shim's,
    so it is a false refusal on a command real grep rejects anyway -- no
    bypass.

    Matched on the MECHANISM, not on a `--d` prefix. The first version of this
    matcher accepted any final token starting `--d`, which is harmless only
    because `--directories` happens to be the sole abbreviatable
    rec_value_flag in the table today: `--devices`, `--dereference-recursive`
    and `--delay` all agree between the consumers. A future `--d*` flag with an
    unrelated divergence would have been silenced by it without ever being
    named. An xfail table that forgives more than it names is the same failure
    mode as a corpus that cannot generate a shape -- one hides a bug by never
    producing it, the other by pre-forgiving it.
    """
    profile = R.PROFILE_BY_NAME.get(argv[0]) if argv else None
    if len(argv) < 2 or profile is None:
        return False
    # The MECHANISM, not a tool allowlist: this is a rec-value flag whose
    # abbreviation loses its value, so the profile must actually have
    # `--directories` as one. find has no such flag and cannot have this bug.
    if "--directories" not in [f for f, _v in profile.rec_value_flags]:
        return False
    last = argv[-1]
    if not (last.startswith("--") and len(last) > 2
            and last != "--directories"
            and "--directories".startswith(last)):
        return False
    # ...and UNAMBIGUOUS among this profile's own long flags, which is what
    # getopt requires before it resolves an abbreviation at all.
    #
    # `--d` is the case this excludes, and excluding it matters: real grep
    # answers `option '--d' is ambiguous; possibilities: '--devices'
    # '--directories' '--dereference-recursive'` and walks nothing, and the two
    # consumers AGREE on it -- both refuse. Forgiving it would have this table
    # silence a shape that does not diverge. `--di` is unambiguous (the others
    # all begin `--de`) and is the real shape.
    others = [f for f in _long_flags(profile)
              if f != "--directories" and f.startswith(last)]
    return not others


# Shapes where the two consumers are KNOWN to disagree, each owned by an open
# issue. Listed rather than silently excluded: the corpus is worth more as a
# record of what is broken than as a green light, and an entry here is deleted
# by the fix, which tightens the test at the moment it can be tightened.
KNOWN_DIVERGENCES = {
    "abbreviated-directories-last": _is_abbreviated_directories_last,
}


def test_the_known_divergence_matcher_names_one_shape_and_not_its_neighbours():
    """The tightening needs its own oracle, or it is the bug one level up."""
    m = _is_abbreviated_directories_last
    assert m(["grep", "-r", "pat", "/scratch", "--di"])
    assert m(["grep", "-r", "pat", "/scratch", "--dir"])
    assert m(["rgrep", "pat", "/scratch", "--directorie"])
    for neighbour in ("--devices", "--dereference-recursive", "--delay",
                      "--depth", "--dxyz", "--directories"):
        assert not m(["grep", "-r", "pat", "/scratch", neighbour]), neighbour
    # `--d` is AMBIGUOUS and the two consumers agree on it (both refuse).
    assert not m(["grep", "-r", "pat", "/scratch", "--d"])
    assert m(["grep", "-r", "pat", "/scratch", "--di"])
    # ...and the shape only counts in FINAL position.
    assert not m(["grep", "-r", "--di", "skip", "pat", "/scratch"])
    # A tool with no `--directories` flag at all is not this shape.
    assert not m(["find", "/scratch", "--di"])


# Leading-mode tools take their operands FIRST; everything else takes them last.
LEADING = ("find",)

ROOTS = ["/scratch", "/home", "/home/someone", "/var/log", "/", "runs", ""]

PATTERN = {"ugrep": "pat", "grep": "pat", "rg": "pat", "fd": "pat",
           "find": None, "du": None, "tree": None}


def _argv(rng, tool):
    parts = [tool]
    flags = [rng.choice(PREFIX_FLAGS[tool]),
             rng.choice(DEPTH_FLAGS[tool]),
             rng.choice(NOISE[tool])]
    rng.shuffle(flags)
    root = rng.choice(ROOTS)
    pattern = PATTERN[tool]

    if tool in LEADING:
        # find's grammar: operands before the predicates.
        parts += ([root] if root else []) + " ".join(flags).split()
    else:
        parts += " ".join(flags).split()
        if pattern is not None:
            parts.append(pattern)
        if root:
            parts.append(root)
        elif root == "":
            # An empty operand is a real shape and must survive the split
            # above, which would otherwise drop it.
            parts.append("")
    trailing = rng.choice(TRAILING[tool])
    if trailing:
        parts.append(trailing)
    return parts


def _corpus(scale):
    """Deterministic. The seed is fixed so a failure is reproducible."""
    rng = random.Random(20260914)
    out = []
    for tool in sorted(PREFIX_FLAGS):
        for _ in range(scale):
            out.append(_argv(rng, tool))
    # Dedupe while keeping order, so a scale bump adds rows rather than
    # reshuffling the ones already known to pass.
    seen = set()
    unique = []
    for argv in out:
        key = tuple(argv)
        if key not in seen:
            seen.add(key)
            unique.append(argv)
    return unique


def pytest_generate_tests(metafunc):
    if "corpus_argv" in metafunc.fixturenames:
        scale = int(os.environ.get("WALK_BLOCKER_CORPUS_SCALE", "22"))
        corpus = _corpus(scale)
        metafunc.parametrize(
            "corpus_argv", corpus,
            ids=["-".join(a).replace("/", "_")[:60] or "empty" for a in corpus])


@pytest.mark.parametrize("cwd", ["/var/tmp", FAST_CWD], ids=["cheap", "fast"])
def test_generated_argv_is_judged_by_the_table(corpus_argv, cwd, mounts, policy, node_fs):
    """The table half: every generated argv gets a verdict, without raising,
    and the same verdict twice. A pipe on stdin can only take a root AWAY
    (ugrep and fzf walk the cwd only at a terminal), so a refusal with a pipe
    implies a refusal at a terminal, never the reverse.

    Every generated tool is one the table knows, or the corpus is exercising
    nothing.
    """
    assert corpus_argv[0] in R.PROFILE_BY_NAME
    real_cwd = resolve_cwd(cwd, node_fs)
    pipe = R.check(corpus_argv, real_cwd, mounts, policy, stdin_is_tty=False)
    again = R.check(corpus_argv, real_cwd, mounts, policy, stdin_is_tty=False)
    tty = R.check(corpus_argv, real_cwd, mounts, policy, stdin_is_tty=True)
    for verdict in (pipe, again, tty):
        assert verdict is None or isinstance(verdict, R.Refusal)
    assert (pipe is None) == (again is None), corpus_argv
    if pipe is not None:
        assert tty is not None, corpus_argv


@pytest.mark.skip(reason="shim arrives in Milestone 4")
@pytest.mark.parametrize("cwd", ["/var/tmp", FAST_CWD], ids=["cheap", "fast"])
def test_generated_argv_agrees_on_both_consumers(corpus_argv, cwd, mounts, policy, node_fs):
    """Table and generated shim must return the same verdict, argv by argv.

    The shim is driven with a PIPE on stdin, so the table is asked the same
    question with `stdin_is_tty=False`. Asking it with the default True would
    make every no-operand ugrep row diverge by construction rather than by
    defect -- the same reason those rows are not in the hand-written matrix.

    Re-enable by deleting the skip mark once `run_shim` exists in conftest;
    a mismatch matching a KNOWN_DIVERGENCES entry is an xfail, anything else
    a failure.
    """
    raise AssertionError("unreachable until the shim exists")
