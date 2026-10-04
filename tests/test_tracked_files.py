"""`_tracked.tracked_files`, the listing every prose scan reads from.

The scans it feeds failed on a clean checkout because a walk read a stale
installed `README.md` out of an ignored `.venv` and a second copy of the docs
out of an untracked review checkout (issue #79, issue #70). These plant both
shapes in a scratch repository, so the fix is pinned without depending on what
happens to sit under the real checkout.
"""
import subprocess

import pytest

from _tracked import tracked_files


def _git(root, *args):
    subprocess.run(["git", "-C", str(root)] + list(args), check=True, capture_output=True)


def _write(root, relative, text="ADR-0001 through 0001\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    for relative in ("README.md", "docs/adr/0001-x.md", "docs/notes.txt", "tools/gate.py",
                     "examples/payload/README.md"):
        _write(tmp_path, relative)
    _write(tmp_path, ".gitignore", ".venv/\n")
    _git(tmp_path, "add", "--", "README.md", "docs", "tools", "examples", ".gitignore")
    # The two shapes from the issues: an ignored virtualenv holding an older
    # release's README, and an untracked review checkout of the whole tree.
    _write(tmp_path, ".venv/lib/python3.X/site-packages/walk_blocker/README.md")
    _write(tmp_path, ".claude/worktrees/pr-1/README.md")
    _write(tmp_path, ".claude/worktrees/pr-1/docs/adr/0001-x.md")
    return tmp_path


def test_a_scan_reads_tracked_files_and_not_an_ignored_venv_or_an_untracked_worktree(repo):
    assert tracked_files(root=repo) == [".gitignore", "README.md", "docs/adr/0001-x.md",
                                        "docs/notes.txt", "tools/gate.py"]


def test_pathspecs_and_suffixes_narrow_the_listing(repo):
    assert tracked_files("*.md", root=repo) == ["README.md", "docs/adr/0001-x.md"]
    # `docs/notes.txt` is tracked under a scanned pathspec, so only the suffix
    # filter keeps it out of the next listing.
    assert tracked_files("docs", root=repo) == ["docs/adr/0001-x.md", "docs/notes.txt"]
    assert tracked_files("docs", ".claude", root=repo, suffixes=(".md",)) == [
        "docs/adr/0001-x.md"]


def test_a_tracked_file_deleted_from_the_working_tree_is_left_out(repo):
    # `git ls-files` still lists it until the deletion is committed; a scan
    # that opened it would fail on a file the tree no longer holds.
    (repo / "docs" / "adr" / "0001-x.md").unlink()
    assert tracked_files("docs", root=repo) == ["docs/notes.txt"]


def test_a_listing_that_finds_nothing_fails_rather_than_passing_vacuously(repo):
    with pytest.raises(pytest.fail.Exception, match="vacuously"):
        tracked_files(".claude", root=repo)


def test_a_directory_that_is_not_a_repository_fails_rather_than_passing(tmp_path):
    _write(tmp_path, "README.md")
    with pytest.raises(pytest.fail.Exception, match="git ls-files failed"):
        tracked_files(root=tmp_path)
