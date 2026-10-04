"""`walk-blocker provenance`: does this payload's configuration come from
reviewed history?

The command exists because a payload proves only that it is internally
consistent. Every test here is written against a throwaway repository built in
`tmp_path`, never against a real site's history, and the configurations are
fictional (ADR-0014).

Two tests carry the whole design and are marked where they sit:

* an uncommitted edit must NOT be found -- that is the gap this closes;
* a blob that entered the branch only through a merge resolution MUST be found
  -- that is the hole in the obvious `git log -- <file>` implementation, which
  history simplification can drop, and a provenance check with a blind spot in
  the property it guarantees is worse than none because it is believed.
"""
import json
import os
import shutil
import subprocess

import pytest

from walk_blocker import provenance as P

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git is not installed")

CONFIG_A = b'schema_version = 1\n[site]\ndisplay_name = "first"\n'
CONFIG_B = b'schema_version = 1\n[site]\ndisplay_name = "second"\n'
CONFIG_C = b'schema_version = 1\n[site]\ndisplay_name = "third"\n'


def _git(repo, *args, **kw):
    """Run git with the ambient environment neutralised.

    A CI container has no global git identity and may have no default branch
    name, so both are supplied on every call rather than written into a config
    the test would then have to clean up.
    """
    env = dict(os.environ)
    env["HOME"] = str(repo)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    base = ["git", "-C", str(repo),
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]
    done = subprocess.run(base + list(args), capture_output=True, env=env, **kw)
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout.decode()


def _commit(repo, data, message, name="site.toml"):
    (repo / name).write_bytes(data)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _unrelated(repo, message):
    """A commit that does not touch the configuration -- a payload rebuild, in
    the shape the site repository actually uses."""
    return _commit(repo, message.encode() + b"\n", message, name="other.txt")


@pytest.fixture
def repo(tmp_path):
    """A repository with `origin/main` standing in for a reviewed branch.

    `origin/main` is created as a real remote-tracking ref rather than a local
    branch, because the command's default ref is `origin/main` and a test that
    quietly used a local branch would not exercise the default anyone runs.
    """
    work = tmp_path / "repo"
    work.mkdir()
    _git(work, "init", "-q")
    _commit(work, CONFIG_A, "first configuration")
    _commit(work, CONFIG_B, "second configuration")
    head = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "update-ref", "refs/remotes/origin/main", head)
    return work


def _run(capsys, **kw):
    kw.setdefault("repo", None)
    code = P.provenance(**kw)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------
# finding what is there
# --------------------------------------------------------------------------

def test_the_commit_whose_configuration_matches_is_found(repo, capsys):
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_B), repo=str(repo))
    assert code == P.EXIT_FOUND, out
    assert "REVIEWED" in out
    assert "and is the configuration at origin/main now" in out


def test_an_older_commit_is_found_not_only_the_tip(repo, capsys):
    """Kills an implementation that only inspects `<ref>:<file>`."""
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_A), repo=str(repo))
    assert code == P.EXIT_FOUND, out
    assert "first configuration" in out
    assert "NOT the configuration at origin/main now" in out


def test_the_report_names_where_the_configuration_entered_not_every_commit(repo, capsys):
    """A configuration that stops changing is carried by every later commit.
    Listing them buries the one useful line."""
    _unrelated(repo, "a payload rebuild")
    _unrelated(repo, "another payload rebuild")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_B), repo=str(repo))
    assert code == P.EXIT_FOUND, out
    assert out.count("REVIEWED") == 1
    assert "second configuration" in out
    assert "unchanged in 2 later commit(s)" in out


# --------------------------------------------------------------------------
# refusing what is not there -- the point of the command
# --------------------------------------------------------------------------

def test_an_uncommitted_edit_is_not_found(repo, capsys):
    """THE ORACLE for the gap this closes. Kills an implementation that hashes
    the working tree instead of the committed blob."""
    (repo / "site.toml").write_bytes(CONFIG_C)
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_C), repo=str(repo))
    assert code == P.EXIT_NOT_FOUND, out
    assert "NOT FOUND" in out
    assert "origin/main" in out


def test_a_commit_only_on_another_branch_is_not_found_from_the_reviewed_ref(repo, capsys):
    """THE OTHER ORACLE. Kills `--all`, and kills defaulting the ref to HEAD:
    the configuration is committed, and reachable from HEAD, and still must not
    count as reviewed."""
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, CONFIG_C, "a configuration nobody reviewed")
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_C), repo=str(repo))
    assert code == P.EXIT_NOT_FOUND, out

    # and it IS found when the reviewed ref is moved to include it, which
    # proves the refusal above was about reachability and not about the bytes
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_C), repo=str(repo))
    assert code == P.EXIT_FOUND, out


def test_a_configuration_reachable_only_through_a_merged_branch_is_found(repo, capsys):
    """Kills the obvious `git log -- <file>` implementation, and this is the
    reason the implementation is not obvious.

    A side branch sets the configuration to C. Main changes something else. The
    merge keeps main's configuration, so the merge commit is TREESAME to main
    for this path and history simplification follows only that parent -- the
    commit that introduced C vanishes from a path-filtered log while remaining
    perfectly reachable from the reviewed branch.

    A payload built from C was reviewed: it was merged into the branch. An
    implementation built on `git log` calls it unreviewed, which is a false
    alarm in the one direction that trains an operator to ignore the tool.
    Verified by hand: in this shape `git log --format=%H main -- site.toml`
    returns a single commit and it is not the one that introduced C.
    """
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, CONFIG_C, "side sets a third configuration")
    _git(repo, "checkout", "-q", "main")
    _unrelated(repo, "main changes something else")
    _git(repo, "merge", "-q", "--no-commit", "-X", "ours", "side")
    (repo / "site.toml").write_bytes(CONFIG_B)
    _git(repo, "add", "site.toml")
    _git(repo, "commit", "-m", "merge side, keeping the reviewed configuration")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", head)

    # the premise: a path-filtered log cannot see where C came from
    listed = _git(repo, "log", "--format=%H", "main", "--", "site.toml").split()
    assert side not in listed, "the simplification hole did not reproduce"

    # the claim: this command can
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_C), repo=str(repo))
    assert code == P.EXIT_FOUND, out
    assert "NOT the configuration at origin/main now" in out


# --------------------------------------------------------------------------
# the defaults, which are load-bearing
# --------------------------------------------------------------------------

def test_the_default_ref_is_the_reviewed_branch_and_not_head():
    """Defaulting to HEAD would validate a payload against the operator's own
    unreviewed working state, which is the whole thing being guarded."""
    assert P.DEFAULT_REF == "origin/main"
    assert "HEAD" not in P.DEFAULT_REF


# --------------------------------------------------------------------------
# a broken environment is never a finding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("broken", [
    "missing-repo", "not-a-repo", "bad-ref", "file-absent-everywhere",
    "no-payload", "payload-not-json", "payload-without-site-sha256",
])
def test_a_broken_environment_is_exit_2_and_never_a_traceback(
        broken, repo, tmp_path, capsys):
    """Exit 1 means unreviewed. A broken environment must never read as one,
    and must never print a traceback."""
    kw = {"sha256": P.sha256_bytes(CONFIG_B), "repo": str(repo)}
    if broken == "missing-repo":
        kw["repo"] = str(tmp_path / "nowhere")
    elif broken == "not-a-repo":
        plain = tmp_path / "plain"
        plain.mkdir()
        kw["repo"] = str(plain)
    elif broken == "bad-ref":
        kw["ref"] = "origin/no-such-branch"
    elif broken == "file-absent-everywhere":
        kw["path"] = "not-the-configuration.toml"
    else:
        payload = tmp_path / "payload"
        payload.mkdir()
        if broken == "payload-not-json":
            (payload / "site.lock.json").write_text("{")
        elif broken == "payload-without-site-sha256":
            (payload / "site.lock.json").write_text('{"files": {}}')
        kw = {"payload": str(payload), "repo": str(repo)}

    code, out, err = _run(capsys, **kw)
    assert code == P.EXIT_ERROR, (out, err)
    assert "Traceback" not in err
    assert err.startswith("walk-blocker provenance: ")
    assert out == ""


def test_git_missing_from_the_path_is_exit_2_and_never_a_traceback(
        repo, tmp_path, monkeypatch, capsys):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, _out, err = _run(capsys, sha256=P.sha256_bytes(CONFIG_B), repo=str(repo))
    assert code == P.EXIT_ERROR
    assert "Traceback" not in err
    assert "cannot run git" in err


def test_an_unresolvable_ref_names_both_remedies_and_the_branches_that_exist(
        repo, capsys):
    """A repository whose reviewed branch is not called the default is the
    likelier case, and `git fetch` does nothing for it. The message must name
    --ref as well, and say what the branches here are called."""
    _git(repo, "update-ref", "refs/remotes/upstream/trunk",
         _git(repo, "rev-parse", "HEAD").strip())
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD",
         "refs/remotes/origin/main")
    code, _out, err = _run(capsys, sha256=P.sha256_bytes(CONFIG_B),
                           repo=str(repo), ref="origin/no-such-branch")
    assert code == P.EXIT_ERROR
    assert "git fetch" in err
    assert "--ref" in err
    listed = err.split("Branches here: ", 1)[1].strip()
    # Remote-tracking branches first, the remote's HEAD symref skipped.
    assert listed == "origin/main, upstream/trunk, main", listed


def test_the_branches_listed_for_an_unresolvable_ref_are_bounded(repo, capsys):
    head = _git(repo, "rev-parse", "HEAD").strip()
    for i in range(P.BRANCHES_SHOWN + 3):
        _git(repo, "update-ref", "refs/remotes/many/b%02d" % i, head)
    code, _out, err = _run(capsys, sha256=P.sha256_bytes(CONFIG_B),
                           repo=str(repo), ref="origin/no-such-branch")
    assert code == P.EXIT_ERROR
    listed = err.split("Branches here: ", 1)[1].strip()
    assert listed.endswith(", ..."), listed
    assert len(listed[:-len(", ...")].split(", ")) == P.BRANCHES_SHOWN


def _unresolvable(repo, capsys):
    return _run(capsys, sha256=P.sha256_bytes(CONFIG_B), repo=str(repo),
                ref="origin/no-such-branch")


def test_every_remote_head_symref_is_skipped_without_hiding_that_more_exist(
        repo, capsys):
    """Each remote may carry a `HEAD` symref. A listing that budgets for one
    of them shows fewer names than it could and drops the `...` while more
    branches exist; this shape is what a fixed `--count` window gets wrong."""
    head = _git(repo, "rev-parse", "HEAD").strip()
    for remote in ("alpha", "beta", "gamma"):
        for branch in ("x1", "x2"):
            _git(repo, "update-ref",
                 "refs/remotes/%s/%s" % (remote, branch), head)
        _git(repo, "symbolic-ref", "refs/remotes/%s/HEAD" % remote,
             "refs/remotes/%s/x1" % remote)
    _git(repo, "update-ref", "-d", "refs/heads/main")
    code, _out, err = _unresolvable(repo, capsys)
    assert code == P.EXIT_ERROR
    listed = err.split("Branches here: ", 1)[1].strip()
    assert listed == ("alpha/x1, alpha/x2, beta/x1, beta/x2, gamma/x1, ..."
                      ), listed


@pytest.mark.parametrize("extra", [0, 1])
def test_the_ellipsis_appears_exactly_when_one_branch_more_exists(
        repo, capsys, extra):
    head = _git(repo, "rev-parse", "HEAD").strip()
    # The fixture holds origin/main and main; these make exactly enough, or
    # exactly one more than is shown.
    for i in range(P.BRANCHES_SHOWN - 2 + extra):
        _git(repo, "update-ref", "refs/remotes/upstream/b%d" % i, head)
    code, _out, err = _unresolvable(repo, capsys)
    assert code == P.EXIT_ERROR
    listed = err.split("Branches here: ", 1)[1].strip()
    assert listed.endswith(", ...") == bool(extra), listed
    shown = listed[:-len(", ...")] if extra else listed
    assert len(shown.split(", ")) == P.BRANCHES_SHOWN, listed


def test_the_unresolvable_ref_line_has_no_doubled_period(repo, capsys):
    _code, _out, err = _unresolvable(repo, capsys)
    first = err.splitlines()[0]
    assert first.endswith("no such commit or ref in %s" % repo), first


@pytest.mark.parametrize("how", ["raises", "exits-non-zero"])
def test_a_failed_branch_listing_drops_the_hint_and_keeps_the_error(
        repo, capsys, monkeypatch, how):
    """Best effort: the listing decorates an error and must never replace it.
    Only the second listing (local branches) fails, so a half-built hint
    from the first cannot pass for a dropped one."""
    real = P._git

    def failing(where, args, timeout, stdin=None):
        if args[0] == "for-each-ref" and args[-1] == "refs/heads":
            if how == "raises":
                raise P.ProvenanceError("git for-each-ref did not finish")
            return subprocess.CompletedProcess(args, 128, b"", b"")
        return real(where, args, timeout, stdin)

    monkeypatch.setattr(P, "_git", failing)
    code, _out, err = _unresolvable(repo, capsys)
    assert code == P.EXIT_ERROR
    assert "no such commit or ref in %s" % repo in err
    assert "git fetch" in err and "--ref" in err
    assert "Branches here" not in err, err
    assert "Traceback" not in err


# --------------------------------------------------------------------------
# reading the hash from a payload, and the machine-readable form
# --------------------------------------------------------------------------

def test_the_hash_can_come_from_a_built_payloads_lock(repo, tmp_path, capsys):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "site.lock.json").write_text(json.dumps(
        {"site_sha256": P.sha256_bytes(CONFIG_B), "files": {}}))
    code, out, _err = _run(capsys, payload=str(payload), repo=str(repo))
    assert code == P.EXIT_FOUND, out


def test_json_output_names_the_commit_and_the_hash(repo, capsys):
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_B),
                           repo=str(repo), as_json=True)
    assert code == P.EXIT_FOUND
    data = json.loads(out)
    assert data["site_sha256"] == P.sha256_bytes(CONFIG_B)
    assert data["ref"] == "origin/main"
    assert data["current_at_ref"] is True
    assert len(data["matches"]) == 1


def test_only_the_distinct_versions_of_the_file_are_ever_read(repo, monkeypatch):
    """The cost argument, measured rather than claimed.

    The first version of this test asserted the REPORTED distinct count, which
    is derived from the same dict the deduplication builds and so cannot tell
    whether anything was deduplicated. It passed with the deduplication
    removed. It now counts the reads themselves: a history of eight commits
    over a configuration that changed twice must read two blobs.
    """
    for i in range(6):
        _unrelated(repo, "payload rebuild %d" % i)
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", head)

    reads = []
    real = P.blob_sha256

    def counting(repo_path, blob_id, timeout=P.DEFAULT_TIMEOUT):
        reads.append(blob_id)
        return real(repo_path, blob_id, timeout)

    monkeypatch.setattr(P, "blob_sha256", counting)
    result = P.search(str(repo), "origin/main", "site.toml",
                      P.sha256_bytes(CONFIG_B))
    assert result["searched"] == 8
    assert result["distinct"] == 2
    assert len(reads) == 2, "read %d blobs for 2 distinct versions" % len(reads)
    assert len(set(reads)) == len(reads), "the same blob was read twice"


@pytest.mark.parametrize("bad", ["", "not-a-hash", "0" * 63, "0" * 65,
                                 "g" * 64, "  ", "0" * 32 + "Z" * 32])
def test_a_value_that_cannot_be_a_digest_is_refused_not_searched_for(
        bad, repo, capsys):
    """Searching for it would report NOT FOUND, which reads as "your
    configuration was never reviewed" when the truth is "you mistyped an
    argument". A false alarm in that direction is what teaches an operator to
    stop believing the tool.

    The empty string also used to reach the payload reader with no payload and
    raise a TypeError: a traceback, which this tree never prints."""
    code, out, err = _run(capsys, sha256=bad, repo=str(repo))
    assert code == P.EXIT_ERROR, (out, err)
    assert "Traceback" not in err
    assert "not a sha256" in err
    assert out == ""


def test_an_uppercase_digest_is_accepted(repo, capsys):
    """Refusing the shape must not refuse a correct value written the other
    way; `sha256sum` output is lowercase but a person may paste either."""
    code, out, _err = _run(capsys, sha256=P.sha256_bytes(CONFIG_B).upper(),
                           repo=str(repo))
    assert code == P.EXIT_FOUND, out


def test_a_payload_whose_recorded_digest_is_not_one_is_refused(
        repo, tmp_path, capsys):
    """The tool is pointed at records that may have been hand-edited, so the
    value it reads out of one gets the same check as the value typed in."""
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "site.lock.json").write_text(
        json.dumps({"site_sha256": "nonsense", "files": {}}))
    code, _out, err = _run(capsys, payload=str(payload), repo=str(repo))
    assert code == P.EXIT_ERROR
    assert "Traceback" not in err
    assert "not a sha256" in err
