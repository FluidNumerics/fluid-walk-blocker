"""What the wheel says it ships must be in git.

hatchling refuses to build when a force-include source is missing, and an
empty directory is not in git however present it is on one developer's disk.
Review round 2 on PR #1 found the whole CI test matrix failing on exactly
that while the suite passed locally.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib


def _force_includes():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        data = tomllib.load(fh)
    return data["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})


def test_there_is_at_least_one_force_include():
    assert _force_includes(), "VERSION is expected to be force-included"


@pytest.mark.parametrize("source", sorted(_force_includes()))
def test_every_wheel_force_include_source_is_tracked(source):
    tracked = subprocess.run(["git", "-C", ROOT, "ls-files", "--", source],
                             capture_output=True, text=True, check=True).stdout
    assert tracked.strip(), (
        "pyproject force-includes %r but git tracks nothing under it; "
        "hatchling will fail the build in any fresh checkout" % source)


def test_a_wheel_builds_from_only_the_tracked_files(tmp_path):
    """The check that fooled everyone: build where untracked directories do
    not exist. The tracked files are copied from the working tree, so an
    uncommitted fix is tested too; a `git clone` would test HEAD instead.
    Uses `uv build`, which CI and the workstation both have."""
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    names = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"],
                           capture_output=True, check=True).stdout.split(b"\0")
    tree = tmp_path / "tree"
    for name in filter(None, names):
        src = os.path.join(ROOT, name.decode())
        dst = tree / name.decode()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    r = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path / "w"), str(tree)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    assert any(n.endswith(".whl") for n in os.listdir(tmp_path / "w"))
