"""Tie a built payload back to a reviewed commit of the site configuration.

A payload records the configuration it was compiled from: `site.lock.json`
carries `site_sha256`, the hash of the site file's own bytes (ADR-0013). That
proves the payload is internally consistent. It does not say whether anyone
reviewed the configuration, and that is the question an operator has before a
deploy: *are these the bytes we agreed to?*

This answers it by content, never by location. Given a payload and a git
repository, it looks for a commit reachable from a reviewed ref whose site file
hashes to the payload's `site_sha256`.

**Why a commit and never a repository URL.** A site's configuration repository
is expected to change hands, and its URL with it. A pinned URL becomes false on
the day the intended handover succeeds, and a check that fails exactly when the
planned workflow happens is a check someone disables. A forge's rename redirect
is a convenience, not a security boundary: the vacated name can be claimed by
anyone. A URL also answers the wrong question -- where bytes were fetched from,
rather than whether those bytes were reviewed -- and it is wrong in both
directions, rejecting an identical mirror and accepting a hostile repository
served at the expected name. A commit is a content address over the whole
history, checked locally, with no network and no trusted third party.

**Why not `git log -- <file>`.** History simplification. A merge commit whose
resolved configuration matches neither parent can be simplified away and never
listed, so a blob that entered the reviewed branch through a conflict
resolution would be reported as unreviewed. A provenance check with a blind
spot in the one property it exists to guarantee is worse than no check, because
it is believed. This walks every reachable commit instead, which costs a
handful of subprocesses because only the distinct blobs are ever read.

Nothing here runs during `walk-blocker build`, and nothing here runs on a node.
The build must never acquire a dependency on git.
"""
import hashlib
import json
import os
import subprocess

from .render import manifest

# The house triple, as in `build`: 0 answered, 1 a difference, 2 cannot answer.
EXIT_FOUND, EXIT_NOT_FOUND, EXIT_ERROR = 0, 1, 2

# Never `HEAD`. Defaulting to the working state would validate a payload
# against the very unreviewed bytes this command exists to catch; opting out of
# review has to be typed out.
DEFAULT_REF = "origin/main"
DEFAULT_FILE = "site.toml"
DEFAULT_TIMEOUT = 30.0

_CHUNK = 65536


class ProvenanceError(Exception):
    """A broken environment, never a finding. The message is the whole report."""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _git(repo, args, timeout, stdin=None):
    """Run git, or raise `ProvenanceError` naming the category and nothing else.

    git's own stderr is deliberately not echoed: a category and an exit code
    are enough for the caller, and passing a subprocess's diagnostics through
    is how a tool starts reporting someone else's vocabulary as its own. Same
    discipline as the IP-hygiene gate.
    """
    try:
        done = subprocess.run(["git", "-C", repo] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              input=stdin, timeout=timeout)
    except OSError as exc:
        raise ProvenanceError("cannot run git (%s); is it installed and on PATH?"
                              % type(exc).__name__)
    except subprocess.TimeoutExpired:
        raise ProvenanceError(
            "git %s did not finish within %gs; is the repository on a "
            "filesystem that is not answering?" % (args[0], timeout))
    return done


def resolve_ref(repo, ref, timeout=DEFAULT_TIMEOUT):
    """The commit `ref` names, distinguishing a bad repo from a bad ref."""
    if not os.path.isdir(repo):
        raise ProvenanceError("%s: not a directory" % repo)
    inside = _git(repo, ["rev-parse", "--git-dir"], timeout)
    if inside.returncode != 0:
        raise ProvenanceError("%s: not a git repository" % repo)
    done = _git(repo, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"], timeout)
    if done.returncode != 0:
        raise ProvenanceError(
            "%s: no such commit or ref in %s. If it is a remote-tracking "
            "branch, it may need `git fetch`." % (ref, repo))
    return done.stdout.decode("ascii", "replace").strip()


def reachable_commits(repo, ref, timeout=DEFAULT_TIMEOUT):
    """Every commit reachable from `ref`, newest first.

    Not `--all`: that would sweep in local branches nobody reviewed, which is
    the opposite of the question.
    """
    done = _git(repo, ["rev-list", ref], timeout)
    if done.returncode != 0:
        raise ProvenanceError("cannot list commits reachable from %s" % ref)
    return done.stdout.decode("ascii", "replace").split()


def blob_at_each(repo, commits, path, timeout=DEFAULT_TIMEOUT):
    """{commit: blob id} for `path`, omitting commits where it does not exist.

    One `cat-file --batch-check` for the whole set: it reports each object's id
    without reading any content, so a long history costs one subprocess rather
    than one per commit. Output is in input order, and a path absent at a
    commit prints a line ending `missing`.
    """
    if not commits:
        return {}
    query = "".join("%s:%s\n" % (c, path) for c in commits).encode("utf-8")
    done = _git(repo, ["cat-file", "--batch-check"], timeout, stdin=query)
    if done.returncode != 0:
        raise ProvenanceError("cannot read %s across the history of this repository"
                              % path)
    out = {}
    lines = done.stdout.decode("utf-8", "replace").splitlines()
    for commit, line in zip(commits, lines):
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "blob":
            out[commit] = fields[0]
    return out


def blob_sha256(repo, blob_id, timeout=DEFAULT_TIMEOUT):
    """The sha256 of a blob's bytes, as the file would be on disk."""
    done = _git(repo, ["cat-file", "blob", blob_id], timeout)
    if done.returncode != 0:
        raise ProvenanceError("cannot read object %s" % blob_id)
    return sha256_bytes(done.stdout)


def describe(repo, commit, timeout=DEFAULT_TIMEOUT):
    """(short sha, date, subject) for a commit, for the report."""
    done = _git(repo, ["show", "-s", "--format=%h%x00%ad%x00%s",
                       "--date=short", commit], timeout)
    if done.returncode != 0:
        return (commit[:9], "", "")
    parts = done.stdout.decode("utf-8", "replace").rstrip("\n").split("\0")
    while len(parts) < 3:
        parts.append("")
    return (parts[0], parts[1], parts[2])


def payload_site_sha256(payload_dir):
    """The `site_sha256` a built payload recorded."""
    lock_path = os.path.join(payload_dir, manifest.FILENAME)
    try:
        lock = manifest.read_manifest(lock_path)
    except OSError:
        raise ProvenanceError("%s: cannot be read" % lock_path)
    except ValueError:
        raise ProvenanceError("%s: not valid JSON; is this a built payload?"
                              % lock_path)
    if not isinstance(lock, dict) or not isinstance(lock.get("site_sha256"), str):
        raise ProvenanceError("%s: no site_sha256; is this a built payload?"
                              % lock_path)
    return lock["site_sha256"]


def search(repo, ref, path, want, timeout=DEFAULT_TIMEOUT):
    """Find the commits on `ref` whose `path` hashes to `want`.

    Returns a dict: the ref's own commit, how many were searched, how many
    distinct versions of the file exist on that ref, and the matching commits
    newest first.
    """
    ref_commit = resolve_ref(repo, ref, timeout)
    commits = reachable_commits(repo, ref, timeout)
    at = blob_at_each(repo, commits, path, timeout)
    if not at:
        raise ProvenanceError(
            "%s exists at no commit reachable from %s. Is --file right? It is "
            "read relative to the repository root." % (path, ref))

    # Only distinct blobs are ever read: a history of hundreds of commits over
    # a file that changed three times costs three reads.
    digests = {}
    for blob in set(at.values()):
        digests[blob] = blob_sha256(repo, blob, timeout)

    matches = [c for c in commits if digests.get(at.get(c)) == want]
    current = at.get(ref_commit)
    return {
        "ref": ref,
        "ref_commit": ref_commit,
        "file": path,
        "site_sha256": want,
        "searched": len(commits),
        "distinct": len(digests),
        "matches": matches,
        "current_at_ref": current is not None and digests.get(current) == want,
    }


def render(result, repo, timeout=DEFAULT_TIMEOUT):
    """The report, as lines."""
    lines = [
        "site_sha256 %s" % result["site_sha256"],
        "searched    %s -> %s, %d commit(s), %d distinct %s"
        % (result["ref"], result["ref_commit"][:9], result["searched"],
           result["distinct"], result["file"]),
    ]
    if not result["matches"]:
        lines.append(
            "NOT FOUND   no commit reachable from %s carries a %s with this hash."
            % (result["ref"], result["file"]))
        lines.append(
            "            The payload was built from a configuration that is not "
            "in the")
        lines.append(
            "            reviewed history: an uncommitted edit, an unpushed "
            "commit, or")
        lines.append(
            "            another branch. If %s is stale, fetch and try again."
            % result["ref"])
        return lines
    # `matches` is newest first, so the last entry is where this configuration
    # ENTERED reviewed history. That is the answer; the commits after it are
    # merely commits where the configuration did not change, and listing them
    # is noise that makes the one useful line harder to find.
    short, date, subject = describe(repo, result["matches"][-1], timeout)
    lines.append("REVIEWED    %s  %s  %s" % (short, date, subject))
    lines.append("            entered %s there, unchanged in %d later commit(s)"
                 % (result["ref"], len(result["matches"]) - 1))
    if result["current_at_ref"]:
        lines.append("            and is the configuration at %s now"
                     % result["ref"])
    else:
        lines.append("            but is NOT the configuration at %s now: the "
                     "reviewed" % result["ref"])
        lines.append("            configuration has moved on, and this payload "
                     "is behind it")
    return lines


def provenance(payload=None, sha256=None, repo=".", ref=DEFAULT_REF,
               path=DEFAULT_FILE, timeout=DEFAULT_TIMEOUT, as_json=False,
               out=None, err=None):
    """Answer the question and return an exit code."""
    import sys
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        want = sha256 if sha256 else payload_site_sha256(payload)
        result = search(repo, ref, path, want, timeout)
    except ProvenanceError as exc:
        err.write("walk-blocker provenance: %s\n" % exc)
        return EXIT_ERROR
    if as_json:
        out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        out.write("\n".join(render(result, repo, timeout)) + "\n")
    return EXIT_FOUND if result["matches"] else EXIT_NOT_FOUND
