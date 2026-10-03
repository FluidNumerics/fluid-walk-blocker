"""The files a prose or reference scan reads: tracked ones, never a walk.

A walk of the checkout reads whatever happens to sit under it -- an ignored
`.venv` holding a non-editable install's copy of `README.md`, a review
checkout under `.claude/worktrees/` -- each a snapshot behind or ahead of this
tree, so a scan failed on files that are not part of it (issue #79, issue
#70). `git ls-files` lists what the repository holds, which is what every such
scan means; `tools/check_no_site_literals.py` enumerates the same way.

`examples/payload/` is always excluded: it is a build product, `build --check`
already proves it identical to its sources, and scanning both reports every
finding twice.

An empty or failed listing fails the calling test rather than skipping it. A
scan over nothing passes every assertion under it, and CI checks the tree out
as a git repository, so a listing that comes back empty there is a broken
environment, not a clean tree.
"""
import subprocess

import pytest

from conftest import ROOT

PAYLOAD = "examples/payload/"


def tracked_files(*pathspecs, root=ROOT, suffixes=None):
    """Sorted repository-relative paths git tracks under `root`, limited to
    `pathspecs` when any are given and to names ending in `suffixes` when
    that is given, outside `examples/payload/`."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--"] + list(pathspecs),
                              capture_output=True)
    except OSError as exc:
        pytest.fail("cannot run git to list tracked files: %s" % exc)
    if proc.returncode != 0:
        pytest.fail("git ls-files failed under %s (exit %d): %s"
                    % (root, proc.returncode, proc.stderr.decode(errors="replace").strip()))
    files = sorted(rel for rel in proc.stdout.decode("utf-8").split("\0")
                   if rel and not rel.startswith(PAYLOAD)
                   and (suffixes is None or rel.endswith(suffixes)))
    if not files:
        pytest.fail("git ls-files listed no %sfiles for %r under %s; a scan over "
                    "nothing would pass vacuously"
                    % ("%r " % (suffixes,) if suffixes else "", list(pathspecs), root))
    return files
