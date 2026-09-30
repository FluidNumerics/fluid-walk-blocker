"""Every path the prose places under the install prefix is one the install
puts there.

The runbook told operators to run `python3 <prefix>/deploy.py --verify` and
`--uninstall` (#69). `deploy.py` is the installer and is never installed, so
both commands failed on every node, and the node's own copy of the runbook
carried them. Pinning those two lines would not see a third, so this reads
every `<prefix>/X`, `PREFIX/X` and `$PREFIX/X` in the tracked markdown and
requires `X` to be something the install creates: an entry of the
deployer's own `INSTALLED_ENTRIES`, or `bin`, the farm `install.sh` builds.

Tracked files only, by `git ls-files`: walking the tree would read a stale
README out of `.venv` or a second copy out of a worktree (#79).
`examples/payload/` is excluded because `build --check` already proves it
identical to these sources.
"""
import os
import re
import subprocess

import pytest

from _deploy_helpers import load_stamped_deploy, site_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX_PATH = re.compile(r"(?:<prefix>|\$PREFIX|\bPREFIX)/([A-Za-z0-9_.-]+)")
# Created under the prefix by install.sh, not copied by deploy.py.
CREATED_BY_INSTALL_SH = {"bin"}

deploy = load_stamped_deploy(site_values())


def _tracked_markdown():
    try:
        proc = subprocess.run(["git", "-C", ROOT, "ls-files", "-z", "--", "*.md"],
                              stdout=subprocess.PIPE, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip("not a git checkout, so tracked files are unknown: %s" % exc)
    return sorted(p for p in proc.stdout.decode().split("\0")
                  if p and not p.startswith("examples/payload/"))


def prefix_paths(text):
    """(line number, first component) for each prefix path in `text`."""
    return [(text.count("\n", 0, m.start()) + 1, m.group(1))
            for m in PREFIX_PATH.finditer(text)]


def test_the_scan_sees_the_shape_it_hunts():
    """Pins the regex, so a prefix path the scan cannot see is a test failure
    here rather than a silent pass below."""
    text = "python3 <prefix>/deploy.py\nls $PREFIX/bin\ncat PREFIX/site.lock.json\n"
    assert prefix_paths(text) == [(1, "deploy.py"), (2, "bin"), (3, "site.lock.json")]


def test_every_prefix_path_in_prose_is_installed():
    allowed = set(deploy.INSTALLED_ENTRIES) | CREATED_BY_INSTALL_SH
    files = _tracked_markdown()
    assert files, "git ls-files found no markdown"
    bad = []
    for rel in files:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            text = fh.read()
        bad.extend("%s:%d: <prefix>/%s" % (rel, line, name)
                   for line, name in prefix_paths(text) if name not in allowed)
    assert not bad, (
        "these paths are not under the install prefix, because the install "
        "does not put them there (deploy.py is run from the staged payload, "
        "never installed):\n  " + "\n  ".join(bad))
