"""Fixtures shared by the rule-table tests.

`src/` goes on sys.path here so the suite imports the checked-out package
whether or not it has been installed into the environment. The rule table is
copied verbatim into the node payload later, so it is imported as
`walk_blocker.search_rules` here and as a bare module there; nothing in it
depends on which (see its module docstring).

The mount table is a FIXTURE, not a copy of any site's (ADR-0014): two mounts
of one parallel-filesystem type, one NFS export, a local root and a tmpfs. The
policy beside it is likewise fictional. Cases that depend on the current
directory use the FAST_CWD / FAST_DEEP sentinels from `argv_cases`, which
resolve to real directories that the fixture table describes as expensive --
a shell derives `$PWD` from its actual working directory, so the shim half of
the suite (Milestone 4) can only be driven from a directory that exists.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from walk_blocker import search_rules as R  # noqa: E402
from argv_cases import FAST_CWD, FAST_DEEP  # noqa: E402

# The fixture mount table, in /proc/mounts field order. `fast` is the
# parallel filesystem (two mounts, one type), `nas:/archive` an NFS export
# whose source also satisfies the remoteness proxy, `/` local and cheap.
BASE_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
fast /scratch wekafs rw,relatime 0 0
fast /home wekafs rw,relatime 0 0
nas:/archive /archive nfs4 rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev 0 0
"""


def make_policy(**overrides):
    """The fixture policy: two listed types, the remoteness proxy on, a
    global ceiling of 2, and `/home` loosened to 4 -- the shape ADR-0007
    describes, with fictional values."""
    fields = dict(remote_fstypes=("wekafs", "nfs4"), remote_proxy=True,
                  maxdepth_allowed=2, unscoped_depth=2,
                  depth_allowance_max=8,
                  mounts={"/home": ("expensive", 4)})
    fields.update(overrides)
    return R.Policy(**fields)


@pytest.fixture(scope="session")
def policy():
    return make_policy()


@pytest.fixture(scope="session")
def node_fs(tmp_path_factory):
    """A fake node filesystem: real directories, described by a real mount table."""
    base = tmp_path_factory.mktemp("nodefs")
    fast_cwd = base / "fastroot"
    fast_deep = fast_cwd / "runs" / "set-01"
    fast_deep.mkdir(parents=True)

    mounts = base / "mounts"
    mounts.write_text(BASE_MOUNTS + "fast %s wekafs rw,relatime 0 0\n" % fast_cwd)

    return {
        "mounts": str(mounts),
        FAST_CWD: str(fast_cwd),
        FAST_DEEP: str(fast_deep),
    }


@pytest.fixture(scope="session")
def mounts(node_fs, policy):
    """The same table, as the rule table reads it: expensive rows only."""
    return R.read_mounts(node_fs["mounts"], policy)


@pytest.fixture
def fixture_home(monkeypatch):
    """`~` in an operand expands through HOME. Pin it to a fixture home under
    the fixture's `/home` mount, so the tilde rows judge the same path on
    every machine and never read the developer's real home. Requested by the
    rule-table modules (autouse there), not globally: the packaging test
    runs `uv`, which needs the real HOME."""
    monkeypatch.setenv("HOME", "/home/someone")


def resolve_cwd(cwd, node_fs):
    """A sentinel becomes a real directory; anything else is passed through."""
    return node_fs.get(cwd, cwd)
