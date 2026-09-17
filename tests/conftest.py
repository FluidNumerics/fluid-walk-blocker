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


# --------------------------------------------------------------------------
# the rendered shim (Milestone 4)
# --------------------------------------------------------------------------
#
# The point of the shim half of the suite is that `search_rules.py` and the
# GENERATED `guard.sh` agree. That only means anything if the shim under test
# is rendered by the real renderer from the real template, run as `sh`, with
# argv arriving the way a shell delivers it after expansion -- so every case
# runs the rendered file.
#
# No side effects outside the tmp tree, including the journal. The test site
# points `trusted_binaries.logger` at a path that does not exist, so no test
# ever reaches the real logger: a genuine record carries valid JSON and
# journald's own `_UID`, indistinguishable from a real override, and emitting
# one from a test run would pollute the trail this repo says will drive the
# `--kill` decision. The sink stays an absolute path with no environment
# override in production, because resolving it through the caller's PATH was
# a bypass; rendering a test site is how the suite opts out.

import copy      # noqa: E402
import shutil    # noqa: E402
import subprocess  # noqa: E402

from walk_blocker import __version__, config  # noqa: E402
from walk_blocker.render import shim as render_shim_module  # noqa: E402

EXAMPLE_SITE = os.path.join(ROOT, "examples", "site.example.toml")


def make_site(mount_table, **overrides):
    """The FIXTURE site: the example site's defaults, with the mount table
    pointed at the fake one, the logger pointed at nothing, and one extra
    job tool so the refusal text's configurable line has something to show.
    `overrides` are dotted keys (`"slurm.extra_job_tools"`)."""
    data = copy.deepcopy(config.load_site(EXAMPLE_SITE).data)
    data["filesystems"]["mount_table"] = mount_table
    data["trusted_binaries"]["logger"] = "/nonexistent/logger"
    data["slurm"]["extra_job_tools"] = ["lookup-job"]
    for dotted, value in overrides.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return data


def render_to(directory, policy, site):
    """Render both artifacts into `directory`; return their paths and text."""
    guard_text = render_shim_module.render_shim(policy, site, __version__)
    names_text = render_shim_module.render_wrapped_names(policy, site, __version__)
    guard = os.path.join(directory, "guard.sh")
    names = os.path.join(directory, "wrapped_names.sh")
    with open(guard, "w") as fh:
        fh.write(guard_text)
    os.chmod(guard, 0o755)
    with open(names, "w") as fh:
        fh.write(names_text)
    return {"guard": guard, "names": names, "guard_text": guard_text,
            "names_text": names_text, "policy": policy, "site": site}


@pytest.fixture(scope="session")
def rendered_shim(tmp_path_factory, policy, node_fs):
    """`guard.sh` and `wrapped_names.sh`, rendered once from the fixture
    policy and the fixture site, with the fake mount table compiled in."""
    base = tmp_path_factory.mktemp("rendered")
    return render_to(str(base), policy, make_site(node_fs["mounts"]))


def _stub_tools(bin_dir, names):
    """Stub binaries that announce themselves on stdout, so a test can tell
    "the guard allowed it and execed the real tool" from "the guard fell
    over". Stubs and not the real tools on purpose: an allowed case in this
    suite must never actually walk this workstation's filesystem."""
    for name in names:
        stub = os.path.join(bin_dir, name)
        with open(stub, "w") as fh:
            fh.write("#!/bin/sh\nprintf 'RAN %s\\n' \"$0\"\nexit 0\n")
        os.chmod(stub, 0o755)


def build_shim_env(base, guard_path, names, node_fs):
    """A PATH with the shim symlinked under every wrapped name, in front of
    stub binaries of the same names."""
    shim_dir = os.path.join(base, "shim")
    bin_dir = os.path.join(base, "bin")
    os.makedirs(shim_dir)
    os.makedirs(bin_dir)
    _stub_tools(bin_dir, names)
    for name in names:
        os.symlink(guard_path, os.path.join(shim_dir, name))
    return {"shim_dir": shim_dir, "bin_dir": bin_dir,
            "mounts": node_fs["mounts"], "node_fs": node_fs}


def wrapped_names_for(site):
    return R.wrapped_names(set(site["shim"]["unwrapped_tools"]))


@pytest.fixture(scope="session")
def shim_env(tmp_path_factory, rendered_shim, node_fs):
    """The rendered shim on a PATH in front of stubs: what most tests drive."""
    base = tmp_path_factory.mktemp("shim-env")
    return build_shim_env(str(base), rendered_shim["guard"],
                          wrapped_names_for(rendered_shim["site"]), node_fs)


@pytest.fixture
def make_shim_env(tmp_path, node_fs, policy):
    """Render a shim from a DIFFERENT policy or site and put it on a PATH.

    `make_shim_env(policy=..., mount_table=..., **site_overrides)`. Used where
    a test needs an override table or a type list the fixture policy does
    not carry -- the classify_mount rows, chiefly -- so the rendered artifact,
    not a hand-edited copy, is what gets exercised."""
    counter = [0]

    def build(policy=policy, mount_table=None, **site_overrides):
        counter[0] += 1
        base = tmp_path / ("env%d" % counter[0])
        base.mkdir()
        site = make_site(mount_table or node_fs["mounts"], **site_overrides)
        rendered = render_to(str(base), policy, site)
        env = build_shim_env(str(base / "farm"), rendered["guard"],
                             wrapped_names_for(site), node_fs)
        env["rendered"] = rendered
        if mount_table:
            env["mounts"] = mount_table
        return env

    return build


@pytest.fixture
def shim_variant(tmp_path, rendered_shim, node_fs):
    """A copy of the rendered shim with named constants rewritten.

    The trusted sinks (SG_LOGGER, SG_AWK, SG_ID, SG_DATE) are absolute paths
    precisely so that restricting PATH cannot hide them -- which means a test
    wanting the fallback branch has to point the constant at something that
    does not exist. Copying the rendered file keeps that honest: the logic
    under test is still the rendered logic, only the constant differs, and
    the rewrite is asserted to have landed rather than silently no-opping
    into a test that proves nothing. Values are written as given, so quote
    them the way the template does (`SG_AWK="'/nonexistent/awk'"`) or pass
    a bare path, which is equivalent for the paths used here."""
    counter = [0]

    def build(**constants):
        text = rendered_shim["guard_text"]
        for name, value in constants.items():
            marker = "\n%s=" % name
            assert marker in text, "no %s= line in the shim to rewrite" % name
            start = text.index(marker) + 1
            end = text.index("\n", start)
            text = "%s%s=%s%s" % (text[:start], name, value, text[end:])
            assert "\n%s=%s\n" % (name, value) in text, name

        counter[0] += 1
        base = tmp_path / ("variant%d" % counter[0])
        base.mkdir()
        guard = base / "guard.sh"
        guard.write_text(text)
        guard.chmod(0o755)
        env = build_shim_env(str(base / "farm"), str(guard),
                             wrapped_names_for(rendered_shim["site"]), node_fs)
        env["guard_text"] = text
        return env

    return build


@pytest.fixture
def logger_stub(tmp_path):
    """A `logger` on PATH that records how it was called.

    Only reachable by a shim whose SG_LOGGER does not exist -- which the
    fixture site guarantees -- so this exercises the documented fallback
    rather than shadowing the trusted path."""
    bin_dir = tmp_path / "logger-bin"
    bin_dir.mkdir()
    log = tmp_path / "logger-calls.txt"
    stub = bin_dir / "logger"
    stub.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n".format(log=log))
    stub.chmod(0o755)
    log.write_text("")
    return {"bin": str(bin_dir), "log": log}


# The shell the shim is driven under. `/bin/sh` is dash on the enterprise
# Debian-family node this was written for and bash on many workstations, so
# executing the shim file through its shebang would run it under bash
# everywhere and never once under the shell production uses. CI installs dash
# for exactly this reason, and a test makes the fallback visible rather than
# silent: a suite that quietly tests the wrong shell is worse than one that
# says which shell it tested.
SHIM_SH = shutil.which("dash") or "sh"

# The runtime seams, scrubbed from the inherited environment before every
# run: a test that does not pass its own `env=` must not silently inherit one
# from the developer's shell. The fake mount table is COMPILED into the
# rendered shim, so WALK_BLOCKER_MOUNTS is not set here either -- the seam
# stays free for the tests that exercise it.
SHIM_SEAMS = ("WALK_BLOCKER_UNSCOPED", "WALK_BLOCKER_FSTYPES",
              "WALK_BLOCKER_DEPTH_BY_MOUNT", "WALK_BLOCKER_MOUNTS",
              "WALK_BLOCKER_AUDIT")


def shim_invocation(shim_env, argv, cwd="/", env=None):
    """(argv-after-the-shell, environment, cwd) for one shim run.

    Split out of `run_shim` so a test that has to put something in FRONT of
    the shell -- `strace`, say -- drives the same environment and the same
    argv shape rather than a second, drifting copy of them.
    """
    real_cwd = resolve_cwd(cwd, shim_env["node_fs"])
    if not os.path.isdir(real_cwd):
        # The case does not depend on the cwd -- every root in it is absolute.
        # Anything that does depend on it must use a sentinel.
        real_cwd = "/"

    environ = dict(os.environ)
    for seam in SHIM_SEAMS:
        environ.pop(seam, None)
    environ.update({
        "PATH": "%s:%s" % (shim_env["shim_dir"], shim_env["bin_dir"]),
        "WALK_BLOCKER_SHIM_DIR": shim_env["shim_dir"],
        "HOME": "/home/someone",
    })
    environ.pop("PWD", None)
    if env:
        environ.update(env)
    # Named explicitly rather than left to the shebang. The shim path is still
    # the first argument, so `$0` is unchanged and the shim reads the same
    # tool name out of its own basename that it would through a PATH lookup.
    command = ([os.path.join(shim_env["shim_dir"], argv[0])] + list(argv[1:]))
    return command, environ, real_cwd


def run_shim(shim_env, argv, cwd="/", env=None, stdin=b""):
    """Invoke the shim the way the shell would: argv[0] is the tool name."""
    command, environ, real_cwd = shim_invocation(shim_env, argv, cwd, env)
    return subprocess.run(
        [SHIM_SH] + command,
        capture_output=True, env=environ, cwd=real_cwd, input=stdin,
    )
