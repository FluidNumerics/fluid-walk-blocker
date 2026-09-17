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

Two halves. The table half: every generated argv must be judged without
error, deterministically, with the two stdin readings differing only where the
grammar says they may. The agreement half: the rendered shim, driven under the
node's shell, must return the table's verdict for every generated argv.
"""

import os
import random

import pytest

from walk_blocker import search_rules as R
from argv_cases import FAST_CWD
from conftest import resolve_cwd, run_shim


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


# Shapes where the two consumers are KNOWN to disagree, each owned by an open
# issue. Listed rather than silently excluded: the corpus is worth more as a
# record of what is broken than as a green light, and an entry here is deleted
# by the fix, which tightens the test at the moment it can be tightened.
#
# EMPTY, and that is the state to keep it in. The one entry it ever held --
# an abbreviated `--directories` in final position, where the table read the
# absent value as "skip" and the shim left the action at recurse -- was
# deleted by the fix that made the abbreviated branch treat a missing value
# the way the exact spelling already did (GNU grep 3.6, measured; the rows
# are in `argv_cases.py` under "dir-action-with-no-value-abbreviated-long").
# An entry added here needs an open issue and a matcher that names ONE shape
# and nothing adjacent to it: the matcher that lived here started out
# forgiving any final token beginning `--d`, which would have silenced an
# unrelated future divergence without ever naming it.
KNOWN_DIVERGENCES = {}


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


@pytest.mark.parametrize("cwd", ["/var/tmp", FAST_CWD], ids=["cheap", "fast"])
def test_generated_argv_agrees_on_both_consumers(corpus_argv, cwd, mounts, policy, node_fs, shim_env):
    """Table and generated shim must return the same verdict, argv by argv.

    The shim is driven with a PIPE on stdin, so the table is asked the same
    question with `stdin_is_tty=False`. Asking it with the default True would
    make every no-operand ugrep row diverge by construction rather than by
    defect -- the same reason those rows are not in the hand-written matrix.

    A mismatch matching a KNOWN_DIVERGENCES entry is an xfail, anything else
    a failure.
    """
    real_cwd = resolve_cwd(cwd, node_fs)
    table = R.check(corpus_argv, real_cwd, mounts, policy, stdin_is_tty=False) is not None

    result = run_shim(shim_env, corpus_argv, cwd=cwd)
    shim = result.returncode == R.EXIT_REFUSED

    if table != shim:
        for issue, matches in KNOWN_DIVERGENCES.items():
            if matches(corpus_argv):
                pytest.xfail("known divergence: %s" % issue)

    assert table == shim, (
        "table=%s shim=%s for: %s (cwd %s)\nstderr: %s"
        % ("REFUSE" if table else "ALLOW", "REFUSE" if shim else "ALLOW",
           " ".join(repr(a) for a in corpus_argv), real_cwd,
           result.stderr.decode()[:400]))
    if not shim:
        assert result.returncode == 0, result.stderr.decode()
        assert b"RAN" in result.stdout, "guard did not exec the real binary"
