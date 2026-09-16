"""The rule table, asserted on its own -- the half of the suite that needs no shim.

Every row of `argv_cases.CASES` is checked against `search_rules.check()`
here; `tests/test_shim_agrees.py` (Milestone 4) will check the same rows
against the generated `guard.sh`, because the whole design rests on those two
agreeing. The rest of this file pins the grammar rules the matrix cannot
express as a verdict, the per-mount depth policy, the two runtime test seams,
and every branch of `classify_mount()` (ADR-0016).
"""

import ast
import os

import pytest

from walk_blocker import search_rules as R
from argv_cases import CASES, FAST_CWD
from conftest import ROOT, make_policy, resolve_cwd


@pytest.fixture(autouse=True)
def _fixture_home(fixture_home):
    """Every test here judges paths under the fixture `/home`."""

SOURCE = os.path.join(ROOT, "src", "walk_blocker", "search_rules.py")


# --------------------------------------------------------------------------
# the matrix
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case_id,argv,cwd,expected,note",
                         CASES, ids=[c[0] for c in CASES])
def test_rule_table(case_id, argv, cwd, expected, note, mounts, policy, node_fs):
    """The table's own verdict, on the same paths the shim will see."""
    refusal = R.check(argv, resolve_cwd(cwd, node_fs), mounts, policy)
    assert (refusal is not None) is expected, "%s: %s" % (case_id, note)


def test_every_case_uses_a_fixture_path_or_a_sentinel():
    """A cwd in the matrix is a fixture path or a sentinel, never a path that
    exists only at a site (ADR-0014). The argv-grammar instructions say a
    real mount path in a row is a finding on its own."""
    fixture_prefixes = ("/home/", "/var/", "/tmp", "/scratch", "/archive", "/")
    for case_id, _argv, cwd, _expected, _note in CASES:
        assert cwd in (FAST_CWD, "<FAST_DEEP>") or cwd.startswith(fixture_prefixes), case_id
        assert "/mnt" not in cwd, case_id


# --------------------------------------------------------------------------
# the refusal, as check() reports it
# --------------------------------------------------------------------------

def test_refusal_carries_tool_root_mount_and_reason(mounts, policy):
    hit = R.check(["find", "/scratch/sub", "-name", "x"], "/var/tmp", mounts, policy)
    assert hit is not None
    assert hit.tool == "find"
    assert hit.root == "/scratch/sub"
    assert hit.mount == "/scratch"
    assert hit.fstype == "wekafs"
    assert hit.reason == "at_or_near_root"
    assert "Refusal(find" in repr(hit)


def test_refusal_names_the_most_severe_mount_a_descending_walk_enters(mounts, policy):
    """A walk from `/` descends into two mounts of the listed parallel type
    and one NFS export. Longest-first would name whichever mount point is
    deepest; the refusal ranks by `remote_fstypes` order so it cites the
    worst class, not the cheapest."""
    hit = R.check(["find", "/", "-name", "x"], "/var/tmp", mounts, policy)
    assert hit.reason == "descends_into"
    assert hit.fstype == "wekafs"
    assert hit.mount != "/archive"
    # Reversing the severity order reverses the pick, and nothing else.
    flipped = policy.replace(remote_fstypes=("nfs4", "wekafs"))
    assert R.check(["find", "/", "-name", "x"], "/var/tmp", mounts, flipped).mount == "/archive"


def test_the_root_reported_is_the_operand_as_typed(mounts, policy, node_fs):
    fast = resolve_cwd(FAST_CWD, node_fs)
    hit = R.check(["find", "runs", "-name", "x"], fast, mounts, policy)
    assert hit.root == "runs"
    assert hit.mount == fast


def test_a_device_bound_answers_descends_into_and_never_at_or_near_root(mounts, policy):
    assert R.check(["find", "/", "-xdev", "-name", "x"], "/var/tmp", mounts, policy) is None
    hit = R.check(["find", "/scratch", "-xdev", "-name", "x"], "/var/tmp", mounts, policy)
    assert hit is not None and hit.reason == "at_or_near_root"


def test_the_exit_code_constant_is_not_greps_no_match():
    """Exit 2, never 1. grep uses 1 for 'no match' and a caller must never
    read a block as an empty result."""
    assert R.EXIT_REFUSED == 2


def test_escape_hatch_and_seams_are_the_documented_names():
    assert R.ESCAPE_HATCH == "WALK_BLOCKER_UNSCOPED"
    assert R.FSTYPES_SEAM == "WALK_BLOCKER_FSTYPES"
    assert R.DEPTH_BY_MOUNT_SEAM == "WALK_BLOCKER_DEPTH_BY_MOUNT"


# --------------------------------------------------------------------------
# per-mount depth policy
# --------------------------------------------------------------------------

def test_the_home_allowance_covers_the_mount_point_deliberately(mounts, policy):
    """A whole-/home depth-4 walk is ALLOWED, and that is the intended call.

    The allowance is keyed on the mount, so it loosens `/home` itself and not
    only `/home/<user>`. Reviewed and kept (ADR-0007) on the grounds that
    bounded-and-finite is the property this guard cares about. Pinned because
    the rationale reads per-home, so the aggregate looks like an oversight to
    anyone who has not seen the decision. If it is ever narrowed, gate the
    override on depth_below >= 1 and change this test in the same commit.
    """
    for argv in (["find", "/home", "-maxdepth", "4"], ["tree", "-L", "4", "/home"]):
        assert R.check(argv, "/", mounts, policy) is None, argv
    # The ceiling still binds at the mount point; it is looser, not absent.
    assert R.check(["find", "/home", "-maxdepth", "5"], "/", mounts, policy) is not None
    # And it is /home-specific: the same walk on the other mount is refused.
    assert R.check(["find", "/scratch", "-maxdepth", "4"], "/", mounts, policy) is not None


def test_globally_bounded_is_the_only_place_the_global_ceiling_is_compared(policy):
    g = policy.maxdepth_allowed
    assert R.globally_bounded(g, policy) is True
    assert R.globally_bounded(g + 1, policy) is False
    assert R.globally_bounded(None, policy) is False
    prof = R.PROFILE_BY_NAME["find"]
    assert R.bounded(prof, ["find", "/x", "-maxdepth", str(g)], policy) is True
    assert R.bounded(prof, ["find", "/x", "-maxdepth", str(g + 1)], policy) is False
    assert R.bounded(prof, ["find", "/x"], policy) is False
    # The ceiling is the policy's, not a constant: a looser policy loosens it.
    assert R.globally_bounded(g + 1, policy.replace(maxdepth_allowed=g + 1)) is True


def test_check_reads_the_bounds_through_the_named_accessors(monkeypatch, mounts, policy):
    """Every caller goes through a named accessor, and bounded() -- which
    would rescan argv -- is not called by check() at all. Called ONCE each,
    so a scan added back by accident shows up here."""
    called = []
    for name in ("bounded", "depth_bound", "device_bounded"):
        real = getattr(R, name)

        def spy(profile, argv, *rest, _name=name, _real=real):
            called.append(_name)
            return _real(profile, argv, *rest)

        monkeypatch.setattr(R, name, spy)

    assert R.check(["find", "/scratch", "-maxdepth", "9"], "/", mounts, policy) is not None
    assert called == ["depth_bound", "device_bounded"], called
    called[:] = []
    assert R.check(["find", "/scratch", "-maxdepth", "2"], "/", mounts, policy) is None
    assert called == ["depth_bound"], called


def test_bounded_still_answers_the_global_question_over_argv(policy):
    prof = R.PROFILE_BY_NAME["find"]
    g = policy.maxdepth_allowed
    for argv in (
        ["find", "/x"],
        ["find", "/x", "-maxdepth", str(g)],
        ["find", "/x", "-maxdepth", str(g + 1)],
        ["find", "/x", "-maxdepth", "1", "-maxdepth", "9"],
        ["find", "/x", "-maxdepth", "9", "-maxdepth", "1"],
        ["find", "/x", "-maxdepth", "abc"],
    ):
        assert R.bounded(prof, argv, policy) is R.globally_bounded(
            R.depth_bound(prof, argv), policy), argv


def test_depth_allowance_falls_back_to_the_global_ceiling(policy):
    """An unlisted mount behaves exactly as before the table existed, which is
    what makes the override an override rather than a dependency."""
    assert R.depth_allowance("/scratch", policy) == policy.maxdepth_allowed
    assert R.depth_allowance("/no/such/mount", policy) == policy.maxdepth_allowed
    assert R.depth_allowance("/home", policy) == 4
    # A class override WITHOUT a maxdepth also falls back.
    cheap_only = policy.replace(mounts={"/home": ("expensive", None)})
    assert R.depth_allowance("/home", cheap_only) == policy.maxdepth_allowed


def test_depth_by_mount_parse_rejects_malformed_pairs():
    """A typo must drop out, not land in the map as a key nothing matches."""
    parsed = R._parse_depth_by_mount("/home=abc:novalue:=3::rel=2:/home=4", 8)
    assert parsed == {"/home": 4}


def test_an_out_of_range_allowance_falls_back_to_the_global_ceiling():
    """A fat-fingered digit must fail toward the RESTRICTIVE answer: dropped,
    not clamped, so the mount inherits the global ceiling."""
    for bad in ("/home=999999", "/home=-3", "/home=9"):
        assert R._parse_depth_by_mount(bad, 8) == {}, bad
    # The boundaries themselves are accepted, so the cap is inclusive.
    assert R._parse_depth_by_mount("/home=0", 8) == {"/home": 0}
    assert R._parse_depth_by_mount("/home=8", 8) == {"/home": 8}
    # ...and the cap is the argument, not a constant.
    assert R._parse_depth_by_mount("/home=8", 6) == {}
    # A trailing slash on the key is normalised to the mount point spelling.
    assert R._parse_depth_by_mount("/home/=3", 8) == {"/home": 3}


def test_per_mount_allowance_loosens_only_the_named_mount(mounts, policy):
    assert R.check(["find", "/home/me", "-maxdepth", "4"], "/", mounts, policy) is None
    assert R.check(["tree", "-L", "4", "/home/me"], "/", mounts, policy) is None
    for argv in (
        ["find", "/home/me", "-maxdepth", "5"],
        ["find", "/scratch", "-maxdepth", "4"],
        ["find", "/archive", "-maxdepth", "4"],
    ):
        assert R.check(argv, "/", mounts, policy) is not None, argv
    # unscoped_depth is untouched, so an unbounded whole-home walk is still
    # refused: a deeper BOUNDED walk is a different claim from an unbounded one.
    assert R.check(["find", "/home/me"], "/", mounts, policy) is not None


def test_a_bound_within_the_global_ceiling_never_resolves_roots(monkeypatch, mounts, policy):
    """The fast path, asserted rather than assumed."""
    def explode(*a, **kw):
        raise AssertionError("resolved_roots called for a globally-bounded walk")
    monkeypatch.setattr(R, "resolved_roots", explode)
    assert R.check(["find", "/scratch", "-maxdepth", "2"], "/", mounts, policy) is None
    assert R.check(["tree", "-L", "1", "/home"], "/", mounts, policy) is None


def test_a_descending_walk_is_judged_by_every_mount_it_enters(mounts, policy):
    """`/home`=4 must not loosen a walk that also enters `/scratch`."""
    for argv in (
        ["find", "/", "-maxdepth", "3"],
        ["find", "/", "-maxdepth", "4"],
        ["du", "-d", "3", "/"],
    ):
        assert R.check(argv, "/", mounts, policy) is not None, argv
    assert R.check(["find", "/", "-maxdepth", "2"], "/", mounts, policy) is None
    assert R.check(["find", "/home", "-maxdepth", "4"], "/", mounts, policy) is None


def test_root_depth_allowance_is_the_strictest_of_what_a_walk_enters(mounts, policy):
    near = R.offending_mount("/home", mounts, policy)
    assert near[2] == "at_or_near_root"
    assert R.root_depth_allowance("/home", mounts, near, policy) == 4

    desc = R.offending_mount("/", mounts, policy)
    assert desc[2] == "descends_into"
    assert R.root_depth_allowance("/", mounts, desc, policy) == policy.maxdepth_allowed
    assert [row[0] for row in R.descended_mounts("/", mounts)] == sorted(
        [row[0] for row in mounts], key=len, reverse=True)


def test_the_descending_verdict_does_not_depend_on_mount_order(tmp_path, policy):
    """Two mounts of one type tie for a walk from `/`; verdict AND named mount
    must be the same whichever order /proc/mounts lists them in."""
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
        for argv in (["find", "/", "-maxdepth", "4"],
                     ["find", "/", "-maxdepth", "9"]):
            hit = R.check(argv, "/", table, policy)
            assert hit is not None, (label, argv)
            seen.setdefault(tuple(argv), set()).add(hit.mount)
    for argv, named in seen.items():
        assert len(named) == 1, (argv, named)


# --------------------------------------------------------------------------
# depth grammar
# --------------------------------------------------------------------------

def test_an_unparseable_depth_still_has_no_numeric_value():
    assert R.depth_bound(R.PROFILE_BY_NAME["find"], ["find", "/x", "-maxdepth", "abc"]) is None


@pytest.mark.parametrize("bad_value", ["", "-", "--", "-L", "-e", "-f", "word"])
def test_a_malformed_depth_value_is_allowed_not_refused(mounts, policy, bad_value):
    """find validates the argument and exits before opening anything
    (findutils 4.8.0: "Expected a positive decimal integer argument to
    -maxdepth"), so a command that never walks is never expensive."""
    argv = ["find", "/scratch", "-maxdepth", bad_value, "2", "-f", "/tmp/cheap"]
    assert R.depth_malformed(R.PROFILE_BY_NAME["find"], argv) is True
    assert R.check(argv, "/", mounts, policy) is None


def test_a_depth_flag_with_no_value_at_all_is_allowed():
    prof = R.PROFILE_BY_NAME["find"]
    assert R.depth_malformed(prof, ["find", "/scratch", "-maxdepth"]) is True


def test_trees_dangling_last_L_is_allowed_not_refused(mounts, policy):
    argv = ["tree", "-L", "2", "/scratch", "-L"]
    assert R.depth_malformed(R.PROFILE_BY_NAME["tree"], argv) is True
    assert R.check(argv, "/", mounts, policy) is None


def test_a_malformed_occurrence_is_not_undone_by_an_earlier_valid_one():
    prof = R.PROFILE_BY_NAME["find"]
    assert R.depth_malformed(prof, ["find", "/x", "-maxdepth", "2", "-maxdepth", "bad"]) is True
    assert R.depth_malformed(prof, ["find", "/x", "-maxdepth", "bad", "-maxdepth", "2"]) is True


def test_a_malformed_depth_inside_a_cluster_is_allowed():
    assert R.depth_malformed(R.PROFILE_BY_NAME["tree"], ["tree", "/scratch", "-xL"]) is True


@pytest.mark.parametrize("hostile", ["-1", "+1", " 1", "1 ", "-10", "+10"])
def test_a_signed_or_whitespace_depth_is_malformed_not_a_small_bound(mounts, policy, hostile):
    """A bare `int()` accepts all six; GNU find rejects every one. The
    generated shim can only test bytes, so the table must be as strict."""
    prof = R.PROFILE_BY_NAME["find"]
    assert R.depth_malformed(prof, ["find", "/x", "-maxdepth", hostile]) is True
    assert R.check(["find", "/scratch", "-maxdepth", hostile, "-name", "x"],
                   "/var/tmp", mounts, policy) is None


def test_every_rejected_depth_spelling_reads_as_no_traversal():
    """A malformed depth must not depend on WHICH spelling was malformed;
    this reaches Layer 2 through is_traversal(), which the matrix cannot see."""
    prof = R.PROFILE_BY_NAME["ugrep"]
    rejected = (
        ["ugrep", "-d", "skip", "--depth=9", "--depth=5", "pat", "/scratch"],
        ["ugrep", "-d", "skip", "-l9-3", "pat", "/scratch"],
        ["ugrep", "-d", "skip", "--depth=5,3", "pat", "/scratch"],
        ["ugrep", "-d", "skip", "-l5-", "pat", "/scratch"],
        ["ugrep", "-d", "skip", "--depth=,9", "--depth=5", "pat", "/scratch"],
    )
    for argv in rejected:
        assert R._scan_bounds(prof, argv) == (None, False, True), argv
        assert R.is_traversal(prof, argv) is False, argv
    assert R.is_traversal(prof, ["ugrep", "-d", "skip", "--depth=9", "pat", "/scratch"]) is True


def test_ugrep_with_no_operand_splits_on_whether_stdin_is_a_terminal(mounts, policy, node_fs):
    prof = R.PROFILE_BY_NAME["ugrep"]
    fast = resolve_cwd(FAST_CWD, node_fs)
    assert R.roots(prof, ["ugrep", "pat"], fast, stdin_is_tty=True) == [fast]
    assert R.roots(prof, ["ugrep", "pat"], fast, stdin_is_tty=False) == []
    assert R.check(["ugrep", "pat"], fast, mounts, policy, stdin_is_tty=True) is not None
    assert R.check(["ugrep", "pat"], fast, mounts, policy, stdin_is_tty=False) is None
    # The long-lived log-filter shape: no operand, a pipe on stdin.
    assert R.check(["ugrep", "--line-buffered", "-E", "pat"], fast, mounts, policy,
                   stdin_is_tty=False) is None


def test_ugreps_cwd_fallback_is_unbounded_not_the_depth_1_default(mounts, policy, node_fs):
    prof = R.PROFILE_BY_NAME["ugrep"]
    fast = resolve_cwd(FAST_CWD, node_fs)
    assert R.depth_bound(prof, ["ugrep", "pat", "/scratch"]) == 1
    assert R.depth_bound(prof, ["ugrep", "pat"]) is None
    assert R.depth_bound(prof, ["ugrep", "--depth=2", "pat"]) == 2
    assert R.check(["ugrep", "pat"], fast, mounts, policy, stdin_is_tty=True) is not None
    assert R.check(["ugrep", "--depth=2", "pat"], fast, mounts, policy, stdin_is_tty=True) is None


def test_the_depth_digit_charset_excludes_every_value_taking_letter():
    prof = R.PROFILE_BY_NAME["ugrep"]
    required = "ABCDJMNOdefgt"
    optional = "?KQZm"
    for letter in required:
        assert "-" + letter in prof.value_flags, letter
        assert letter not in prof.depth_digits, letter
    for letter in optional:
        assert "-" + letter not in prof.value_flags, letter
        assert letter not in prof.depth_digits, letter
    for char in "lnrRIGE0123456789-,":
        assert char in prof.depth_digits, char


# --------------------------------------------------------------------------
# leading-mode grammar
# --------------------------------------------------------------------------

def test_tool_is_resolved_from_argv_not_from_comm():
    """`comm` is 15 bytes the process may overwrite, and the thing overwriting
    it is usually a wrapper, not the tool. argv[0] first; comm only as the
    fallback for an empty cmdline."""
    assert R.tool_from_argv(["bfs", "-S", "dfs", "/"], comm="3.2.1") == "bfs"
    assert R.tool_from_argv(["/home/someone/.local/bin/bfs", "/"], "3.2.1") == "bfs"
    assert R.tool_from_argv([], comm="find") == "find"
    assert R.tool_from_argv(["python3", "walk.py"], comm="python3") is None
    assert R.tool_from_argv([], comm=None) is None


def test_bfs_leading_flags_do_not_end_the_operand_scan(mounts, policy):
    """Every flag `bfs --help` (4.1.1) documents before the paths, plus the
    glued-option-then-root-flag composition that broke on its own."""
    for argv in (
        ["bfs", "-S", "dfs", "/scratch"],
        ["bfs", "-regextype", "findutils-default", "/scratch"],
        ["bfs", "-j8", "/scratch"],
        ["bfs", "-O3", "/scratch"],
        ["bfs", "-D", "search", "/scratch"],
        ["bfs", "-E", "/scratch"],
        ["bfs", "-X", "/scratch"],
        ["bfs", "-s", "/scratch"],
        ["bfs", "-d", "/scratch"],
        ["bfs", "-x", "/scratch"],
        ["bfs", "-f", "/scratch"],
        ["bfs", "-S", "dfs", "-E", "-j4", "-regextype", "posix-egrep", "/scratch"],
        ["bfs", "-O3", "-f", "/scratch"],
        ["bfs", "-j8", "-f", "/scratch"],
    ):
        hit = R.check(argv, "/var/tmp", mounts, policy)
        assert hit is not None, argv
        assert hit.mount == "/scratch", (argv, hit.mount)


def test_an_attached_root_flag_is_not_a_root_in_leading_mode():
    prof = R.PROFILE_BY_NAME["bfs"]
    assert R.roots(prof, ["bfs", "-f=/tmp/cheap", "-name", "x"], "/fast") == ["/fast"]
    assert R.roots(prof, ["bfs", "-f", "/tmp/cheap", "-name", "x"], "/fast") == ["/tmp/cheap"]


def test_no_leading_mode_profile_abbreviates():
    for profile in R.PROFILES:
        if profile.root_mode == "leading":
            assert not profile.abbreviates, profile.names


def test_leading_profiles_differ_only_in_what_dashdash_means():
    leading = [p for p in R.PROFILES if p.root_mode == "leading"]
    assert len(leading) >= 2, "the split collapsed back to one profile"
    allowed_to_differ = {"names", "dashdash", "root_flags", "value_flags"}
    reference = leading[0]
    for profile in leading[1:]:
        for field, value in vars(reference).items():
            if field in allowed_to_differ:
                continue
            assert vars(profile)[field] == value, (
                "leading profiles differ on %s: %r has %r, %r has %r" % (
                    field, reference.names, value, profile.names, vars(profile)[field]))
    assert {p.dashdash for p in leading} == {"predicate", "end_of_options"}
    assert {p.root_flags for p in leading} == {(), ("-f",)}


def test_cluster_letters_matches_find_prefix_flags():
    prof = R.PROFILE_BY_NAME["find"]
    assert prof.cluster_letters == frozenset(f[1:] for f in R.FIND_PREFIX_FLAGS)
    assert prof.cluster_letters == R.PROFILE_BY_NAME["bfs"].cluster_letters


def test_dashdash_is_read_only_in_leading_mode():
    for profile in R.PROFILES:
        if profile.root_mode != "leading":
            assert profile.dashdash == "predicate", profile.names
    grepish = R.Profile(("greplike",), rec_flags=("-r",), skip_pos=1,
                        dashdash="end_of_options")
    assert R.roots(grepish, ["greplike", "-r", "--", "-r", "/scratch"], "/var/tmp") == ["/scratch"]


def test_a_single_letter_device_flag_is_no_longer_a_bypass():
    prof = R.PROFILE_BY_NAME["bfs"]
    assert "-x" in prof.device_flags
    assert R.device_bounded(prof, ["bfs", "-xdev", "/"]) is True
    assert R.device_bounded(prof, ["bfs", "-x", "/"]) is True
    argv = ["bfs", "-S", "dfs", "-regextype", "findutils-default", "/", "-name", "x"]
    assert R.device_bounded(prof, argv) is False


def test_a_genuine_cluster_still_splits():
    prof = R.PROFILE_BY_NAME["bfs"]
    assert R.roots(prof, ["bfs", "-HL", "/scratch", "-maxdepth", "1"], "/tmp") == ["/scratch"]
    assert R.roots(prof, ["bfs", "-H", "-L", "/scratch", "-maxdepth", "1"], "/tmp") == ["/scratch"]


def test_an_unmodelled_leading_option_is_detectable():
    prof = R.PROFILE_BY_NAME["bfs"]
    assert R.unclaimed_absolute_root(prof, ["bfs", "-Q", "z", "/", "-name", "x"]) == "/"
    assert R.unclaimed_absolute_root(prof, ["find", "-name", "foo"]) is None
    assert R.unclaimed_absolute_root(prof, ["find", "/scratch", "-newer", "/etc/fstab"]) is None
    assert R.unclaimed_absolute_root(prof, ["bfs", "-S", "dfs", "/"]) is None
    assert R.unclaimed_absolute_root(
        R.PROFILE_BY_NAME["grep"], ["grep", "-r", "/etc/passwd"]) is None


def test_roots_can_report_what_it_actually_charged():
    prof = R.PROFILE_BY_NAME["find"]
    assert R.roots(prof, ["find", "-name", "x"], "/var/tmp") == ["/var/tmp"]
    assert R.roots(prof, ["find", "-name", "x"], "/var/tmp", fallback=False) == []
    assert R.roots(prof, ["find", "/scratch"], "/var/tmp", fallback=False) == ["/scratch"]


# --------------------------------------------------------------------------
# the other profiles' grammar pins
# --------------------------------------------------------------------------

def test_an_empty_walker_root_adds_no_root_on_the_stdin_branch(mounts, policy, node_fs):
    prof = R.PROFILE_BY_NAME["fzf"]
    fast = resolve_cwd(FAST_CWD, node_fs)
    empty = ["fzf", "--walker-root", ""]
    assert R.roots(prof, empty, "/anywhere", stdin_is_tty=False) == []
    assert R.check(empty, fast, mounts, policy, stdin_is_tty=False) is None
    assert R.check(empty, fast, mounts, policy, stdin_is_tty=True) is None
    assert R.check(["fzf"], fast, mounts, policy, stdin_is_tty=True) is not None


def test_an_output_only_depth_flag_still_consumes_its_value():
    prof = R.PROFILE_BY_NAME["du"]
    for argv in (
        ["du", "-d", "2", "/tmp/x"],
        ["du", "--max-depth", "2", "/tmp/x"],
        ["du", "--max-depth=2", "/tmp/x"],
        ["du", "-d2", "/tmp/x"],
        ["du", "-hd", "1", "/tmp/x"],
        ["du", "-hd1", "/tmp/x"],
        ["du", "--max-d", "2", "/tmp/x"],
    ):
        assert R.roots(prof, argv, "/fast-cwd") == ["/tmp/x"], argv
    assert not prof.depth_flags
    assert prof.output_only_depth_flags == ("-d", "--max-depth")
    assert "du" in R.UNBOUNDABLE and "grep" in R.UNBOUNDABLE and "find" not in R.UNBOUNDABLE


def test_dus_device_bound_is_untouched_by_losing_its_depth_flag(mounts, policy):
    for argv in (["du", "-x", "/"], ["du", "-x", "-d1", "/var"],
                 ["du", "--one-file-system", "-h", "/"], ["du", "-xd9", "/"]):
        assert R.check(argv, "/var/tmp", mounts, policy) is None, argv
    assert R.check(["du", "-sx", "/scratch"], "/var/tmp", mounts, policy) is not None


def test_an_abbreviated_root_flag_still_supplies_a_root():
    """The grammar belongs to getopt_long, not to the tool that once used it;
    asserted against a constructed profile so the arm cannot rot unnoticed."""
    profile = R.Profile(("dbtool",), always=True, abbreviates=True,
                        root_flags=("-U", "--database-root"), default_root="/")
    assert R.roots(profile, ["dbtool", "--database-r", "/var/tmp/things"], "/var/tmp") == ["/var/tmp/things"]
    assert R.roots(profile, ["dbtool", "--database-r=/var/tmp/things"], "/var/tmp") == ["/var/tmp/things"]
    assert R.roots(profile, ["dbtool", "--database-root", "/var/tmp/things"], "/var/tmp") == ["/var/tmp/things"]
    assert R.roots(profile, ["dbtool"], "/var/tmp") == ["/"]


def test_fzf_value_flags_were_audited_by_asking_fzf(mounts, policy):
    fast = "/scratch/x"
    assert R.check(["fzf", "--nth", "--walker-max-depth", "1"], fast, mounts, policy) is not None
    assert R.check(["fzf", "--color", "--walker-max-depth", "1"], fast, mounts, policy) is None
    assert R.check(["fzf", "--walker-max-depth", "1"], fast, mounts, policy) is None
    profile = R.PROFILE_BY_NAME["fzf"]
    assert "--color" not in profile.value_flags
    for flag in ("--nth", "--filter", "--height", "--with-nth", "--tiebreak"):
        assert flag in profile.value_flags, flag


def test_fzf_and_sk_are_unwrapped_by_default_and_the_site_decides():
    """fzf is reached through keybindings, where `exit 2` is an invisible
    no-op; the table's default leaves it alone, the site's list is what the
    build applies, and Layer 2 sees every profile regardless."""
    assert R.DEFAULT_UNWRAPPED == frozenset({"fzf", "sk"})
    default = R.wrapped_names(R.DEFAULT_UNWRAPPED)
    assert "fzf" not in default and "sk" not in default
    assert "find" in default and "grep" in default and "ugrep" in default
    assert "fzf" in R.PROFILE_BY_NAME and "sk" in R.PROFILE_BY_NAME
    # A site may wrap them, or leave more alone; one profile is one decision.
    assert "fzf" in R.wrapped_names(set())
    assert "sk" not in R.wrapped_names({"fzf"})
    assert "grep" not in R.wrapped_names({"egrep"})
    assert R.wrapped_profiles(R.DEFAULT_UNWRAPPED) == tuple(p for p in R.PROFILES if p.wrapped)


def test_is_stream_filter_takes_the_sites_set():
    assert R.is_stream_filter(["tail", "-F", "x"], {"tail"}) is True
    assert R.is_stream_filter(["/usr/bin/tail", "-F", "x"], {"tail"}) is True
    assert R.is_stream_filter(["tail", "-F", "x"], set()) is False
    assert R.is_stream_filter([], {"tail"}) is False
    assert R.is_stream_filter([""], {"tail"}) is False


# --------------------------------------------------------------------------
# Policy.from_env -- the two audited test seams
# --------------------------------------------------------------------------

def test_from_env_with_nothing_set_returns_the_defaults_unchanged(monkeypatch, policy):
    monkeypatch.delenv(R.FSTYPES_SEAM, raising=False)
    monkeypatch.delenv(R.DEPTH_BY_MOUNT_SEAM, raising=False)
    got = R.Policy.from_env(policy)
    assert repr(got) == repr(policy)


def test_from_env_a_bad_depth_entry_is_dropped_toward_the_restrictive_value(monkeypatch, policy):
    """`/home=444` is a typo, not a policy: the runtime seam drops it and the
    mount falls back to the global ceiling. (The build-time config fails
    loudly on the same input; that is config.py's job.)"""
    monkeypatch.setenv(R.DEPTH_BY_MOUNT_SEAM, "/home=444")
    got = R.Policy.from_env(policy)
    assert got.mounts["/home"] == ("expensive", None)
    assert R.depth_allowance("/home", got) == policy.maxdepth_allowed
    # The original is untouched: from_env returns a copy.
    assert policy.mounts["/home"] == ("expensive", 4)


def test_from_env_a_good_depth_entry_applies(monkeypatch, policy, mounts):
    monkeypatch.setenv(R.DEPTH_BY_MOUNT_SEAM, "/home=3:/scratch=5")
    got = R.Policy.from_env(policy)
    assert R.depth_allowance("/home", got) == 3
    assert R.depth_allowance("/scratch", got) == 5
    # A mount the defaults did not list is added as expensive with that depth;
    # a listed one keeps its class.
    assert got.mounts["/scratch"] == ("expensive", 5)
    assert R.check(["find", "/scratch", "-maxdepth", "5"], "/", mounts, got) is None
    assert R.check(["find", "/home", "-maxdepth", "4"], "/", mounts, got) is not None


def test_from_env_an_empty_depth_variable_clears_every_allowance(monkeypatch, policy):
    monkeypatch.setenv(R.DEPTH_BY_MOUNT_SEAM, "")
    assert R.depth_allowance("/home", R.Policy.from_env(policy)) == policy.maxdepth_allowed


def test_from_env_fstypes_replaces_the_type_list(monkeypatch, policy, node_fs):
    monkeypatch.setenv(R.FSTYPES_SEAM, "nfs4:fuse.*")
    got = R.Policy.from_env(policy)
    assert got.remote_fstypes == ("nfs4", "fuse.*")
    # ...and an empty value leaves the compiled list alone.
    monkeypatch.setenv(R.FSTYPES_SEAM, "")
    assert R.Policy.from_env(policy).remote_fstypes == policy.remote_fstypes


# --------------------------------------------------------------------------
# classify_mount -- every branch (ADR-0016)
# --------------------------------------------------------------------------

def test_classify_a_listed_type_is_expensive(policy):
    assert R.classify_mount(("fast", "/scratch", "wekafs", "rw,relatime"), policy) == "expensive"


def test_classify_an_unlisted_remote_type_by_source_is_expensive(policy):
    assert R.classify_mount(("host:/x", "/x", "newfs", "rw"), policy) == "expensive"


def test_classify_an_unlisted_remote_type_by_netdev_option_is_expensive(policy):
    assert R.classify_mount(("cluster", "/x", "newfs", "rw,_netdev,relatime"), policy) == "expensive"


def test_classify_an_unlisted_remote_type_by_addr_option_is_expensive(policy):
    assert R.classify_mount(("cluster", "/x", "newfs", "rw,addr=10.0.0.1"), policy) == "expensive"  # site-literal-ok: RFC 1918 placeholder in a fixture option string


def test_classify_a_local_unknown_type_is_cheap(policy):
    assert R.classify_mount(("tmpfs", "/run", "tmpfs", "rw,nosuid,nodev"), policy) == "cheap"
    assert R.classify_mount(("/dev/loop0", "/snap/x", "squashfs", "ro,nodev"), policy) == "cheap"
    # `_netdev` must be a whole option, and `addr=` a prefix of one: a
    # substring match would guard `/dev/sda1` mounted with `nodev,x-addr=`.
    assert R.classify_mount(("/dev/sda1", "/", "ext4", "rw,my_netdev,x-addr=1"), policy) == "cheap"


def test_classify_a_glob_type_matches_fuse_variants():
    fuse = make_policy(remote_fstypes=("fuse.*",), remote_proxy=False)
    assert R.classify_mount(("sshfs#u@h:", "/x", "fuse.sshfs", "rw"), fuse) == "expensive"
    assert R.classify_mount(("fast", "/scratch", "wekafs", "rw"), fuse) == "cheap"


def test_classify_a_cheap_override_on_a_listed_type_wins(policy):
    small = policy.replace(mounts={"/archive": ("cheap", None)})
    assert R.classify_mount(("nas:/archive", "/archive", "nfs4", "rw"), small) == "cheap"
    # ...and it is exact: a sibling path is still judged by its type.
    assert R.classify_mount(("nas:/other", "/archive2", "nfs4", "rw"), small) == "expensive"


def test_classify_an_expensive_override_with_maxdepth_feeds_depth_allowance(policy):
    """A local type made expensive by hand, with its own ceiling: the class
    comes from the override and the depth from the same row."""
    big_local = policy.replace(mounts={"/data": ("expensive", 6)})
    entry = ("/dev/md0", "/data", "xfs", "rw,noatime")
    assert R.classify_mount(entry, policy) == "cheap"
    assert R.classify_mount(entry, big_local) == "expensive"
    assert R.depth_allowance("/data", big_local) == 6
    assert R.depth_allowance("/data", policy) == policy.maxdepth_allowed


def test_classify_remote_proxy_off_makes_the_source_and_option_tests_inert(policy):
    strict = policy.replace(remote_proxy=False)
    assert R.classify_mount(("host:/x", "/x", "newfs", "rw,_netdev,addr=h"), strict) == "cheap"
    # The type list still applies.
    assert R.classify_mount(("host:/x", "/x", "nfs4", "rw"), strict) == "expensive"


def test_read_mounts_returns_only_the_expensive_rows_longest_first(node_fs, policy, mounts):
    points = [row[0] for row in mounts]
    assert "/" not in points and "/run" not in points
    assert set(points) == {"/scratch", "/home", "/archive", node_fs[FAST_CWD]}
    assert points == sorted(points, key=len, reverse=True)
    # Each row carries the four fields classify_mount() needed, mount point first.
    for row in mounts:
        assert len(row) == 4
    archive = next(row for row in mounts if row[0] == "/archive")
    assert archive[1:] == ("nfs4", "nas:/archive", "rw,relatime")


def test_read_mounts_with_no_table_has_no_opinion(tmp_path, policy):
    assert R.read_mounts(str(tmp_path / "missing"), policy) == []


def test_read_mounts_honours_a_cheap_override(node_fs, policy):
    small = policy.replace(mounts={"/archive": ("cheap", None)})
    assert "/archive" not in [row[0] for row in R.read_mounts(node_fs["mounts"], small)]


def test_read_mounts_guards_an_unlisted_remote_mount(tmp_path, policy):
    table = tmp_path / "mounts"
    table.write_text("/dev/sda1 / ext4 rw 0 0\nsrv:/big /big newfs rw,_netdev 0 0\n")
    assert [row[0] for row in R.read_mounts(str(table), policy)] == ["/big"]
    assert R.read_mounts(str(table), policy.replace(remote_proxy=False)) == []


# --------------------------------------------------------------------------
# the module's own constraints
# --------------------------------------------------------------------------

def _source_tree():
    with open(SOURCE, encoding="utf-8") as fh:
        return fh.read()


def test_the_table_imports_nothing_from_the_package():
    """It is copied verbatim into the node payload, where the package does
    not exist."""
    text = _source_tree()
    assert "import walk_blocker" not in text
    assert "from walk_blocker" not in text
    assert "from ." not in text.replace("from .", "from .") or "from ." not in text
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not node.module.startswith("walk_blocker"), ast.dump(node)
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in ("os", "re", "fnmatch"), alias.name


def test_the_table_reads_the_environment_only_in_from_env():
    """The node never reads configuration (ADR-0013); the two seams are the
    documented exception, and they live in one method."""
    tree = ast.parse(_source_tree())
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Policy":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "from_env":
                    allowed.update(range(item.lineno, item.end_lineno + 1))
    assert allowed, "Policy.from_env not found"
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv")
                and isinstance(node.value, ast.Name) and node.value.id == "os"):
            hits.append(node.lineno)
    assert hits, "from_env is expected to read os.environ"
    assert all(line in allowed for line in hits), hits


def test_the_table_is_python_39_syntax():
    ast.parse(_source_tree(), feature_version=(3, 9))
