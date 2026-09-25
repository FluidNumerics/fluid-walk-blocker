"""deploy.py's orchestration: the root+approval gate, what it hands to
install.sh and systemd, and the literals it reads instead of arguments.

install.sh's own correctness (the hooks, the symlink farm, verification) is
tests/test_install.py's job. Here we check that deploy.py asks for the right
things in the right order, refuses the same things in preview and in
execute, and writes correct unit files -- so systemctl calls are recorded
rather than actually run (no real systemd in CI), matching how `_is_root` is
monkeypatched rather than requiring real root.

The module under test is the STAMPED deployer (see `_deploy_helpers`): the
in-tree `node/deploy.py` with a fictional site's values compiled in,
imported from a scratch directory the way the payload lays it out. Every
path a test moves a constant to is under tmp; every literal the shape tests
read is the example site's (ADR-0014).
"""

import argparse
import contextlib
import functools
import grp
import io
import json
import os
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from _deploy_helpers import (EXAMPLE_SITE, REQUIRED_SOURCES, ROOT,
                             load_stamped_deploy, site_values, source_text,
                             stamped_text, write_stamped_deploy)
from _install_helpers import Layout, stamped_install
import walk_blocker
from walk_blocker import build, stamp

VALUES = site_values()
deploy = load_stamped_deploy(VALUES)

# The test user's primary group: what the suite's spools are chgrp'd to.
SPOOL_GROUP = grp.getgrgid(os.getgid()).gr_name

# The interpreter the node actually has (ADR-0015), where this machine has
# one; the test interpreter otherwise, so the check still runs.
NODE_PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable

PATH_FLAGS = ("--prefix", "--unit-dir", "--spool-dir", "--audit",
              "--bashrc-file", "--zshenv-file", "--fish-conf-file")


def recording_run(calls, active_units=(), enabled_units=()):
    """A `run` stub that records commands and models systemd honestly.

    Both state queries are answered with the state WORD, because that is
    what deploy.py reads. For `is-active`, a non-zero exit can also mean a
    bus error, so the exit code alone cannot distinguish "not running" from
    "could not tell". Defaults model the common case: nothing running,
    nothing enabled. Pass `active_units` for a unit that refused to stop,
    `enabled_units` for one whose `disable` left the enablement symlink
    behind -- the shape that used to slip past an is-active-only check.
    """
    def fake_run(cmd, check=True, capture=True, dry_run=False, env=None):
        calls.append(cmd)
        returncode, stdout = 0, ""
        if cmd[:2] == ["systemctl", "is-active"]:
            if cmd[-1] in active_units:
                returncode, stdout = 0, "active\n"
            else:
                returncode, stdout = 3, "inactive\n"
        elif cmd[:2] == ["systemctl", "show"]:
            if cmd[2] in enabled_units:
                returncode, stdout = 0, "enabled\n"
            else:
                returncode, stdout = 0, "disabled\n"
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    return fake_run


def pass_uninstall_checks(monkeypatch, prefix):
    """Satisfy the uninstall guards: trusted chain, and a marked directory.

    The chain is stubbed for the same reason as on the install path -- a tmp
    tree's ancestors are user-owned -- but the marker is a real file, since
    that check is about what is on disk.
    """
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    os.makedirs(prefix, exist_ok=True)
    open(os.path.join(prefix, deploy.PAYLOAD_MARKER), "w").close()
    # The teardown runs the DEPLOYED helper, so it has to be there.
    staged = os.path.join(prefix, "shim")
    os.makedirs(staged, exist_ok=True)
    for name in ("install.sh", "wrapped_names.sh"):
        open(os.path.join(staged, name), "w").close()


def pass_prefix_checks(monkeypatch):
    """Stub the path-trust checks to their post-install answers.

    Ownership is driven by real uids, and a test running as an ordinary user
    in a tmp directory can satisfy neither the chain nor `unowned_by`: every
    ancestor of `tmp_path` is owned by the test user, which is exactly what
    the chain check is for. Each has its own tests below.

    WHAT THIS STUB HIDES: with it in place no test can exercise the REAL
    traversability check through `system_execute`/`system_preview`. Use
    `pass_ownership_checks()` with the `traversable_root` fixture when the
    caller-level behaviour of the real check is what is under test.
    """
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    # pytest's tmp_path parent is 0700, so it is genuinely not traversable
    # by other users -- the check is right and the fixture is not the shape
    # it judges.
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    # not_a_default() is deliberately NOT stubbed: the autouse _test_paths
    # fixture moves the constants to this tmp tree, so the real check runs
    # and passes for the right reason.


def pass_ownership_checks(monkeypatch):
    """`pass_prefix_checks()` MINUS the traversability stub. Ownership
    cannot be satisfied by an unprivileged test, so those two stay stubbed;
    traversability can be, given a tree whose ancestors really are
    traversable (`traversable_root`), and leaving it real is the point."""
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])


def unowned_by_here(tree):
    """`deploy.unowned_by` driven against the running user's own uid. The
    check exists to prove root ownership, which a test cannot create; what
    it can prove is the logic -- symlink escape, unreadable subtree, loose
    mode -- without privilege. Same dependency-injection reasoning ADR-0004
    records for `_is_root`."""
    return deploy.unowned_by(str(tree), uid=os.getuid())


@pytest.fixture(autouse=True)
def _test_paths(tmp_path, monkeypatch):
    """Point the compiled locations at this test's tmp tree.

    deploy.py has no path flags, so THIS is the entire test seam: the code
    reads module constants and the tests move them (ADR-0005). Autouse,
    because a test that forgot them would try to install into the example
    site's literal prefix for real.
    """
    monkeypatch.setattr(deploy, "DEFAULT_PREFIX", str(tmp_path / "prefix"))
    monkeypatch.setattr(deploy, "DEFAULT_UNIT_DIR", str(tmp_path / "unit-dir"))
    monkeypatch.setattr(deploy, "DEFAULT_SPOOL_DIR", str(tmp_path / "var-log"))
    monkeypatch.setattr(deploy, "DEFAULT_BASHRC_FILE", str(tmp_path / "bashrc"))
    monkeypatch.setattr(deploy, "DEFAULT_ZSHENV_FILE", str(tmp_path / "zshenv"))
    monkeypatch.setattr(deploy, "DEFAULT_FISH_CONF_FILE",
                        str(tmp_path / "fish-conf.fish"))
    # stage_payload() stages under STAGING_PARENT, which is not writable by
    # a test in production. Same seam as the six above.
    staging_parent = tmp_path / "run"
    staging_parent.mkdir(exist_ok=True)
    monkeypatch.setattr(deploy, "STAGING_PARENT", str(staging_parent))
    # The reader group (ADR-0025) must resolve on the machine running the
    # suite, so it is the test user's own primary group -- the one a non-root
    # `fchown` may always name -- and the listed service groups are none.
    monkeypatch.setattr(deploy, "DEFAULT_SPOOL_GROUP", SPOOL_GROUP)
    monkeypatch.setattr(deploy, "TRUSTED_GROUPS", ())
    # The spool's owner is root in production and a function argument
    # everywhere, like `unowned_by`'s `uid=`: a test cannot create a
    # root-owned directory, so the spool writers are driven against the
    # test user's own uid. The logic under test -- no-follow opens, the
    # fstat, the group and mode -- is the same.
    for name in ("create_spool", "repair_spool", "spool_mode_repairs"):
        monkeypatch.setattr(deploy, name,
                            functools.partial(getattr(deploy, name), uid=os.getuid()))


@pytest.fixture
def traversable_root():
    """A tree an ordinary user really can traverse, unlike `tmp_path`.

    pytest hands out `tmp_path` under a 0700 per-user directory, so the real
    `untraversable_for_users()` flags its ancestors and is right to. This
    needs `o+x` on EVERY ancestor -- a directory under `/tmp` has that.
    `dir="/tmp"` is PINNED rather than left to `tempfile`'s default: with no
    `dir=` the parent comes from TMPDIR, which is the exact defect
    `STAGING_PARENT` exists to avoid. The precondition is still asserted,
    because pinning the parent does not prove the parent is traversable.
    """
    root = tempfile.mkdtemp(prefix="walk-blocker-caller-", dir="/tmp")
    try:
        os.chmod(root, 0o755)
        assert not deploy.untraversable_for_users(root), (
            "this fixture's whole purpose is a traversable chain; %s is not one"
            % root)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _args(tmp_path, **overrides):
    """An args namespace as main() would build it -- from the constants.
    Reading deploy.default_paths() rather than restating the paths is what
    stops the fixture above and this helper from drifting apart: a test that
    overrides one value is visibly disagreeing with the installer, which is
    exactly what not_a_default() is there to catch."""
    ns = argparse.Namespace(dry_run=False, **deploy.default_paths())
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _move_constants(monkeypatch, root, spool=None):
    """Point every compiled location under `root` (a `traversable_root`),
    the way the autouse fixture does for tmp_path."""
    for attr, value in (("PREFIX", "prefix"), ("UNIT_DIR", "unit-dir"),
                        ("BASHRC_FILE", "bashrc"), ("ZSHENV_FILE", "zshenv"),
                        ("FISH_CONF_FILE", "fish.conf")):
        monkeypatch.setattr(deploy, "DEFAULT_" + attr, os.path.join(root, value))
    monkeypatch.setattr(deploy, "DEFAULT_SPOOL_DIR",
                        spool or os.path.join(root, "var-log"))
    staging = os.path.join(root, "run")
    os.makedirs(staging, exist_ok=True)
    monkeypatch.setattr(deploy, "STAGING_PARENT", staging)


def _previewed_command(out):
    """The constructed root command from a dry run's output."""
    return next(l for l in out.splitlines()
                if "--system" in l
                and os.path.join(deploy.REPO, "deploy.py") in l)


# --------------------------------------------------------------------------
# the stamped module
# --------------------------------------------------------------------------

def test_the_stamped_constants_are_carried_into_the_module(monkeypatch):
    """Every marker line resolves to the site's value, in the type the
    build emits: a bool stays a bool and an int an int, so `Persistent=`
    and the timeout arithmetic read the value and not its spelling. The
    autouse fixture is undone first, or this asserts things about
    tmp_path."""
    monkeypatch.undo()
    for name, key in (("DEFAULT_PREFIX", "install.prefix"),
                      ("DEFAULT_UNIT_DIR", "install.unit_dir"),
                      ("DEFAULT_SPOOL_DIR", "install.spool_dir"),
                      ("DEFAULT_AUDIT_FILENAME", "install.audit_filename"),
                      ("STAGING_PARENT", "install.staging_parent"),
                      ("DEFAULT_BASHRC_FILE", "hooks.bash.file"),
                      ("DEFAULT_ZSHENV_FILE", "hooks.zsh.file"),
                      ("DEFAULT_FISH_CONF_FILE", "hooks.fish.file"),
                      ("TIMER_SLOT", "timer.on_calendar"),
                      ("TIMER_ACCURACY_SEC", "timer.accuracy_sec"),
                      ("TRUSTED_TIMEOUT", "trusted_binaries.timeout"),
                      ("TRUSTED_SH", "trusted_binaries.sh"),
                      ("TRUSTED_PYTHON3", "trusted_binaries.python3"),
                      ("DISPLAY_NAME", "site.display_name")):
        assert getattr(deploy, name) == VALUES["site.toml:" + key], name
        assert isinstance(getattr(deploy, name), str), name
    for name, key in (("TIMER_RANDOMIZED_DELAY_SEC", "timer.randomized_delay_sec"),
                      ("TIMEOUT_START_SEC", "timer.timeout_start_sec"),
                      ("RELINK_TIMEOUT_S", "timer.relink_timeout_s"),
                      ("RELINK_KILL_AFTER_S", "timer.relink_kill_after_s")):
        assert getattr(deploy, name) == VALUES["site.toml:" + key], name
        assert type(getattr(deploy, name)) is int, name
    for name, key in (("HOOK_ENABLED_BASH", "hooks.bash.enabled"),
                      ("HOOK_ENABLED_ZSH", "hooks.zsh.enabled"),
                      ("HOOK_ENABLED_FISH", "hooks.fish.enabled"),
                      ("TIMER_PERSISTENT", "timer.persistent")):
        assert getattr(deploy, name) is VALUES["site.toml:" + key], name
    assert deploy.__version__ == walk_blocker.__version__


def test_the_in_tree_file_carries_sentinels_not_a_sites_values():
    """The tree is generic. Every marker line in `node/deploy.py` holds a
    placeholder the build replaces, so no location or slot in the tree can
    be mistaken for a site's, and a payload that was never stamped is
    unmistakable."""
    for marker in stamp.find_markers(source_text()):
        assert marker.value.startswith("'@@") and marker.value.endswith("@@'"), (
            marker.name, marker.value)


def test_the_consumers_row_matches_the_markers_in_the_file():
    """Three spellings of one set: the `CONSUMERS` row the build enforces,
    the `REQUIRED_SOURCES` this suite pins, and the markers actually in the
    file. A source in one and not the others is a literal nobody stamps or
    nobody checks."""
    in_file = set(m.source for m in stamp.find_markers(source_text()))
    assert in_file == set(REQUIRED_SOURCES)
    assert set(stamp.CONSUMERS["deploy.py"]) == set(REQUIRED_SOURCES)
    assert "deploy.py" in build.EXECUTABLE


def test_deploy_and_install_sh_are_stamped_from_the_same_keys():
    """The install <-> install.sh pair. Both files carry the spool, the
    audit filename and every hook file as literals, stamped from the same
    keys by the same build -- so the relink cannot re-assert a mode on one
    directory while the install asserts it on another, which is the
    divergence a hand-maintained default in each file allowed."""
    shared = {"site.toml:install.prefix", "site.toml:install.spool_dir",
              "site.toml:install.audit_filename",
              "site.toml:hooks.bash.file", "site.toml:hooks.bash.enabled",
              "site.toml:hooks.zsh.file", "site.toml:hooks.zsh.enabled",
              "site.toml:hooks.fish.file", "site.toml:hooks.fish.enabled"}
    assert shared <= set(stamp.CONSUMERS["deploy.py"])
    assert shared <= set(stamp.CONSUMERS["shim/install.sh"])


def test_a_stale_marker_is_reported_by_check_text():
    """`--check` is the oracle (ADR-0013): a payload stamped for one prefix
    and checked against a site that now says another is stale, by name."""
    text = stamped_text(VALUES)
    moved = dict(VALUES)
    moved["site.toml:install.prefix"] = "/opt/elsewhere/walk-blocker"
    findings = stamp.check_text(text, moved, "py", REQUIRED_SOURCES)
    assert len(findings) == 1, findings
    assert findings[0].startswith("stale: ") and "DEFAULT_PREFIX" in findings[0]
    assert "site.toml:install.prefix" in findings[0]


def test_a_missing_marker_is_louder_than_a_stale_one():
    """A marker that is renamed or deleted stops being stamped and stops
    being checked at the same instant, which is the one failure the gate
    must never report as success."""
    lines = [l for l in stamped_text(VALUES).splitlines(True)
             if "GENERATED from site.toml:timer.on_calendar" not in l]
    findings = stamp.check_text("".join(lines), VALUES, "py", REQUIRED_SOURCES)
    assert findings == ["missing: no `# GENERATED from site.toml:timer.on_calendar` marker"]


def test_deploy_is_runnable_by_the_nodes_own_interpreter(tmp_path):
    """No PEP 723 header and no `uv run` shebang: neither exists on the
    node, and this script runs there. Byte-compiled under the node's own
    interpreter, so a construct the workstation's Python accepts and the
    node's does not is caught here (ADR-0015)."""
    header = source_text()[:400]
    assert header.startswith("#!/usr/bin/env python3\n")
    assert "uv run" not in header
    assert "/// script" not in header
    proc = subprocess.run([NODE_PYTHON, "-m", "py_compile",
                           os.path.join(deploy.REPO, "deploy.py")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    py_compile.compile(os.path.join(deploy.REPO, "deploy.py"), doraise=True,
                       cfile=str(tmp_path / "deploy.pyc"))


def test_version_answers_without_a_mode():
    """`--version` is neither a mode nor a path: it answers on its own, on
    the node's interpreter, and names the payload's one version."""
    proc = subprocess.run([NODE_PYTHON, os.path.join(deploy.REPO, "deploy.py"),
                           "--version"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "walk-blocker %s" % walk_blocker.__version__


@pytest.fixture(scope="module")
def built_payload(tmp_path_factory):
    out = tmp_path_factory.mktemp("build") / "payload"
    o, e = io.StringIO(), io.StringIO()
    code = build.build(EXAMPLE_SITE, str(out), out=o, err=e)
    assert code == 0, e.getvalue()
    return out


def test_the_built_payload_ships_deploy_py_executable_and_stamped(built_payload):
    """The build's own product: `deploy.py` at the payload root, 0755, byte-
    compiling under the node's interpreter, current for the site it was
    built from, and answering `--version` from the payload directory."""
    path = built_payload / "deploy.py"
    assert stat.S_IMODE(os.lstat(str(path)).st_mode) == 0o755
    text = path.read_text()
    assert "@@" not in text
    site = walk_blocker.config.load_site(EXAMPLE_SITE)
    values = stamp.SiteValues(site, walk_blocker.__version__)
    assert stamp.check_text(text, values, "py", stamp.CONSUMERS["deploy.py"]) == []
    proc = subprocess.run([NODE_PYTHON, str(path), "--version"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "walk-blocker %s" % walk_blocker.__version__
    compiled = subprocess.run([NODE_PYTHON, "-m", "py_compile", str(path)],
                              capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

def test_deploy_system_preview_does_not_execute(tmp_path, monkeypatch):
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    args = _args(tmp_path)
    rc = deploy.system_preview(args)
    assert rc == 0
    assert not os.path.exists(args.prefix)
    assert not os.path.exists(args.bashrc_file)
    assert not os.path.exists(args.unit_dir)
    assert not os.path.exists(args.spool_dir)


def test_preview_names_a_command_that_can_actually_run(
        tmp_path, capsys, monkeypatch):
    """The preview is the one instruction handed to a root operator on a
    node with no `uv`. `sh deploy.py` is not a command; the interpreter
    named is the compiled one the unit itself runs under."""
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    deploy.system_preview(_args(tmp_path))
    out = capsys.readouterr().out

    assert "%s %s --system" % (
        deploy.TRUSTED_PYTHON3, os.path.join(deploy.REPO, "deploy.py")) in out
    assert "sh %s" % os.path.join(deploy.REPO, "deploy.py") not in out


def test_the_preview_runs_the_payloads_own_installer_preview(tmp_path, monkeypatch):
    """From the payload directory, with no path flags, through the trusted
    shell: install.sh carries the same literals and needs to be told
    nothing."""
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_preview(_args(tmp_path)) == 0
    assert calls == [[deploy.TRUSTED_SH,
                      os.path.join(deploy.REPO, "shim", "install.sh"),
                      "--system", "--dry-run"]]


def test_deploy_system_refuses_without_root(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: False)
    args = _args(tmp_path)
    rc = deploy.system_execute(args)
    assert rc == 3
    assert not os.path.exists(args.prefix)
    assert not os.path.exists(args.unit_dir)


def test_deploy_system_uninstall_requires_root_only(tmp_path, monkeypatch):
    """Reversing a control is the safer direction. It needs proof of root,
    which since ADR-0021 is the whole of the install's gate too."""
    args = _args(tmp_path)

    monkeypatch.setattr(deploy, "_is_root", lambda: False)
    assert deploy.system_uninstall(args) == 3

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_uninstall_checks(monkeypatch, args.prefix)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_uninstall(args) == 0
    assert any(c[:3] == ["systemctl", "disable", "--now"] for c in calls)
    assert any("--uninstall" in c for c in calls)


def test_the_cli_refuses_the_path_flags_it_used_to_accept():
    """The guarantee is that the command cannot be pointed elsewhere, not
    that it validates being pointed elsewhere. argparse exits 2."""
    for flag in PATH_FLAGS:
        proc = subprocess.run(
            [sys.executable, os.path.join(deploy.REPO, "deploy.py"),
             "--system", flag, "/tmp/somewhere"],
            capture_output=True, text=True)
        assert proc.returncode == 2, flag
        assert "unrecognized arguments" in proc.stderr, flag
        assert flag in proc.stderr, flag


def test_the_preview_runs_unprivileged_for_real_and_writes_nothing(
        traversable_root):
    """The preview path, end to end, as this unprivileged user: a stamped
    deployer beside a stamped installer, both for a fictional site laid out
    under a traversable root. It prints the whole plan, prints the rendered
    units, leaves the tree exactly as it found it -- and REFUSES, because a
    tree this account owns is not a chain root can be asked to install into
    and the install would refuse it. No stub anywhere: this is the run an
    operator does first, and what it must never do is advertise a command
    that is going to fail after the timer has been stopped.

    A passing end-to-end preview cannot be built without privilege: every
    ancestor an unprivileged test can create is owned by the test user, and
    that is precisely what `untrusted_prefix_chain()` exists to refuse. The
    advertised command's own shape is pinned by
    `test_the_previewed_command_is_the_command_that_installs`.
    """
    payload = os.path.join(traversable_root, "payload")
    layout = Layout(traversable_root,
                    prefix=os.path.join(traversable_root, "prefix"),
                    bashrc=os.path.join(traversable_root, "bashrc"),
                    zshenv=os.path.join(traversable_root, "zshenv"),
                    fishconf=os.path.join(traversable_root, "fish-conf.fish"),
                    spool=os.path.join(traversable_root, "var-log"),
                    toolbin=os.path.join(traversable_root, "usrbin"),
                    mount_table=os.path.join(traversable_root, "mounts"))
    stamped_install(traversable_root, dest=os.path.join(payload, "shim"),
                    layout=layout)
    values = site_values(**{
        "install.prefix": str(layout.prefix),
        "install.spool_dir": str(layout.spool),
        "install.spool_group": SPOOL_GROUP,
        "install.unit_dir": os.path.join(traversable_root, "unit-dir"),
        "install.staging_parent": os.path.join(traversable_root, "run"),
        "hooks.bash.file": str(layout.bashrc),
        "hooks.zsh.file": str(layout.zshenv),
        "hooks.fish.file": str(layout.fishconf),
    })
    script = write_stamped_deploy(values, payload)

    def snapshot():
        seen = set()
        for base, dirs, files in os.walk(traversable_root):
            for name in dirs + files:
                seen.add(os.path.join(base, name))
        return seen

    before = snapshot()
    proc = subprocess.run([NODE_PYTHON, script, "--system", "--dry-run"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 6, proc.stdout + proc.stderr
    assert snapshot() == before, "a preview must leave the tree untouched"
    assert not os.path.exists(str(layout.prefix))
    assert not os.path.exists(str(layout.spool))

    out = proc.stdout
    # The whole plan is still printed: a refusal at the end is a refusal
    # with the reader told what was going to happen, not a bare exit.
    assert "System-wide install of walk-blocker Layer 1" in out
    assert not [l for l in out.splitlines()
                if l.lstrip("# ").strip().startswith("sh ")
                and "install.sh" in l and "--system" in l
                and "--relink" not in l and "--version" not in l]
    assert "OnCalendar=%s" % VALUES["site.toml:timer.on_calendar"] in out
    assert "ExecStart=%s %s/reaper.py --report --spool %s" % (
        deploy.TRUSTED_PYTHON3, layout.prefix, layout.spool) in out
    # ...and no command is advertised under it.
    assert _previewed_lines(out, script) == [], out
    assert "the path is not trusted end to end" in proc.stderr, proc.stderr
    assert "would refuse too" in proc.stderr, proc.stderr


def _previewed_lines(out, script):
    return [l for l in out.splitlines()
            if "--system" in l and script in l]


# --------------------------------------------------------------------------
# unowned_by
# --------------------------------------------------------------------------

def test_unowned_by_accepts_a_tree_owned_by_the_expected_uid(tmp_path):
    tree = tmp_path / "payload"
    (tree / "shim").mkdir(parents=True)
    (tree / "shim" / "guard.sh").write_text("#!/bin/sh\n")
    (tree / "shim" / "guard.sh").chmod(0o755)
    (tree / "reaper.py").write_text("x\n")
    (tree / "reaper.py").chmod(0o644)

    assert unowned_by_here(tree) == []


def test_unowned_by_fails_closed_on_a_subtree_it_cannot_inspect(tmp_path):
    """A tree it could not read must never come back clean: `os.walk`
    swallows an unlistable directory unless you pass onerror."""
    tree = tmp_path / "payload"
    (tree / "hidden").mkdir(parents=True)
    (tree / "hidden" / "install.sh").write_text("#!/bin/sh\n")
    (tree / "hidden").chmod(0o000)
    try:
        offenders = unowned_by_here(tree)
        assert offenders, "an uninspectable subtree reported no offenders"
        assert any("could not be inspected" in o.reason for o in offenders), \
            offenders
    finally:
        (tree / "hidden").chmod(0o755)


def test_unowned_by_rejects_a_symlink_pointing_out_of_the_prefix(tmp_path):
    """`cp -a` copies a symlink as a symlink, `chown -R` reassigns only the
    link, and `chmod -R` skips links -- so a root-owned link to a
    user-writable file passed, and systemd executes the target."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "target.sh").write_text("#!/bin/sh\necho surprise\n")

    tree = tmp_path / "payload"
    (tree / "shim").mkdir(parents=True)
    escaping = tree / "shim" / "install.sh"
    escaping.symlink_to(outside / "target.sh")

    offenders = unowned_by_here(tree)
    assert [o for o in offenders
            if o.path == str(escaping)
            and o.code == deploy.UNOWNED_ESCAPING_SYMLINK], offenders


def test_unowned_by_allows_a_symlink_that_stays_inside_the_prefix(tmp_path):
    """install.sh's own symlink farm points at $prefix/shim/guard.sh. An
    in-tree link is fine: the walk reaches its target separately."""
    tree = tmp_path / "payload"
    (tree / "shim").mkdir(parents=True)
    guard = tree / "shim" / "guard.sh"
    guard.write_text("#!/bin/sh\n")
    guard.chmod(0o755)
    (tree / "bin").mkdir()
    (tree / "bin" / "find").symlink_to(guard)

    assert unowned_by_here(tree) == []


def test_unowned_by_flags_a_foreign_owner_and_a_writable_mode(tmp_path):
    tree = tmp_path / "payload"
    (tree / "shim").mkdir(parents=True)
    guard = tree / "shim" / "guard.sh"
    guard.write_text("#!/bin/sh\n")

    foreign = deploy.unowned_by(str(tree), uid=os.getuid() + 1)
    assert [o.path for o in foreign if o.path == str(guard)], foreign
    assert all(o.code == deploy.UNOWNED_FOREIGN_UID for o in foreign), foreign

    guard.chmod(0o775)
    writable = unowned_by_here(tree)
    assert [o for o in writable
            if o.path == str(guard)
            and o.code == deploy.UNOWNED_PERMISSIVE_MODE], writable


def test_unowned_by_flags_setuid_in_a_tree_root_executes_from(tmp_path):
    tree = tmp_path / "payload"
    tree.mkdir()
    odd = tree / "helper"
    odd.write_text("#!/bin/sh\n")
    odd.chmod(0o4755)
    assert [o for o in unowned_by_here(tree)
            if o.code == deploy.UNOWNED_SETUID], unowned_by_here(tree)


def test_unowned_by_checks_an_installed_file_not_only_a_directory(tmp_path):
    """`os.walk` yields nothing for a file, so the entries that go in as
    files would have passed unexamined."""
    lone = tmp_path / "reaper.py"
    lone.write_text("x\n")
    lone.chmod(0o644)
    assert unowned_by_here(lone) == []

    lone.chmod(0o666)
    assert [o for o in unowned_by_here(lone)
            if o.code == deploy.UNOWNED_PERMISSIVE_MODE], "a loose mode passed"

    assert deploy.unowned_by(str(lone), uid=os.getuid() + 1), \
        "a foreign owner passed"


# --------------------------------------------------------------------------
# ownership, staging and the install order
# --------------------------------------------------------------------------

def test_preview_prints_exactly_the_ownership_commands_execution_runs(
        tmp_path, monkeypatch, capsys):
    """These drifted once in the predecessor: the preview advertised a
    recursive chown of the prefix after execution had been scoped to the
    installed entries, so it documented the foot-gun the code avoids."""
    args = _args(tmp_path)
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))

    deploy.system_preview(args)
    previewed = capsys.readouterr().out

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_execute(args) == 0

    executed = [c for c in calls
                if c[:2] in (["chown", "-R"], ["chmod", "-R"])]
    assert executed == deploy.ownership_commands(args.prefix), executed
    for cmd in executed:
        assert " ".join(cmd) in previewed, "not in the preview: %s" % cmd
    assert "chown -R root:root %s\n" % args.prefix not in previewed


def test_recursive_mutations_are_aimed_at_the_installed_entries_not_the_prefix(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    recursive = [c for c in calls if c[:2] in (["chown", "-R"], ["chmod", "-R"])]
    assert recursive, calls
    for cmd in recursive:
        target = cmd[-1]
        assert target != args.prefix, "aimed at the prefix itself: %s" % cmd
        assert os.path.basename(target) in deploy.INSTALLED_ENTRIES, cmd
    for entry in deploy.INSTALLED_ENTRIES:
        assert any(c[-1] == os.path.join(args.prefix, entry)
                   for c in recursive), entry


def _installs_of(calls, args, name):
    target = os.path.join(args.prefix, name)
    return [c for c in calls if c[0] == "install" and c[-1] == target]


def test_the_readme_is_installed_where_the_hook_blocks_point(tmp_path,
                                                             monkeypatch):
    """The hook blocks tell every user to read $PREFIX/README.md. 0644,
    since it is read and never executed; in INSTALLED_ENTRIES so the
    ownership pass covers it and a rejected install removes it."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    installs = _installs_of(calls, args, "README.md")
    assert len(installs) == 1, calls
    assert "0644" in installs[0], installs[0]
    assert installs[0][-2].endswith("README.md")
    assert "README.md" in deploy.INSTALLED_ENTRIES


def test_walk_job_is_installed_executable(tmp_path, monkeypatch):
    """The command every Layer 1 refusal advertises has to be ON the node,
    and executable by the users install.sh links it for -- not only by the
    root that installed it."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    installs = _installs_of(calls, args, "walk-job")
    assert len(installs) == 1, calls
    assert "0755" in installs[0], installs[0]
    assert installs[0][-2].endswith("walk-job")
    assert "walk-job" in deploy.INSTALLED_ENTRIES


def test_the_site_record_and_the_survey_are_installed_readable(tmp_path,
                                                               monkeypatch):
    """site.toml and site.lock.json are the record of what was built --
    `cat` them to learn what is deployed (ADR-0013) -- and survey.py is run
    by an admin through the interpreter, so none of the three is
    executable. All in INSTALLED_ENTRIES, so the ownership pass covers them
    and a rejected install removes them."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    for name in ("site.toml", "site.lock.json", "survey.py", "search_rules.py"):
        installs = _installs_of(calls, args, name)
        assert len(installs) == 1, (name, calls)
        assert installs[0][:3] == ["install", "-m", "0644"], installs[0]
        assert installs[0][-2].endswith(name)
        assert name in deploy.INSTALLED_ENTRIES
    assert _installs_of(calls, args, "reaper.py")[0][:3] == ["install", "-m", "0755"]


def test_every_payload_source_is_an_installed_entry_and_vice_versa():
    """What is copied is what is owned, chmodded and removed on refusal.
    The marker is the one installed entry that is not a payload source: it
    is generated into the snapshot."""
    sources = set(rel for rel, _d, _m in deploy.PAYLOAD_SOURCES)
    assert sources | {deploy.PAYLOAD_MARKER} == set(deploy.INSTALLED_ENTRIES)
    for rel, is_dir, _mode in deploy.PAYLOAD_SOURCES:
        real = os.path.join(ROOT, "examples", "payload", rel)
        assert os.path.isdir(real) == is_dir, rel


def test_the_copy_does_not_preserve_the_payloads_ownership(tmp_path, monkeypatch):
    """`cp -a` as root chowns each new file to the SOURCE's owner, and an
    already-open descriptor keeps its access across a later chown."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    assert deploy.system_execute(_args(tmp_path)) == 0
    copies = [c for c in calls if c[0] == "cp"]
    assert copies, calls
    for cmd in copies:
        assert "--no-preserve=ownership" in cmd, cmd


def test_the_payload_is_staged_root_only_and_widened_only_when_clean(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    staged = next(c for c in calls if c[:2] == ["install", "-d"])
    assert "0700" in staged, staged
    widen = next(i for i, c in enumerate(calls)
                 if c[:2] == ["chmod", "0755"] and c[-1] == args.prefix)
    installer = next(i for i, c in enumerate(calls)
                     if any("install.sh" in a for a in c))
    assert widen < installer, "widened before the guard is wired up, not after"

    strips = [c for c in calls if c[:2] == ["chmod", "-R"] and "a-s" in c]
    assert len(strips) == len(deploy.INSTALLED_ENTRIES), strips


def test_the_payload_is_made_readable_not_merely_unwritable(tmp_path,
                                                             monkeypatch):
    """`chmod -R go-w` only REMOVES bits. A payload unpacked under `umask
    077` arrives at 0700, passes unowned_by, passes a root-run verify --
    and then every user has a shim on PATH that none of them can execute."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    modes = [c for c in calls if c[:2] == ["chmod", "-R"]]
    assert modes, calls
    for entry in deploy.INSTALLED_ENTRIES:
        target = os.path.join(args.prefix, entry)
        adds = [c for c in modes
                if c[-1] == target and any("a+rX" in a for a in c)]
        assert adds, "no mode is SET for %s, only removed: %s" % (entry, modes)


def test_a_symlinked_installed_entry_is_rejected_before_any_chmod(
        tmp_path, monkeypatch):
    """GNU `chmod -R` DEREFERENCES a symlink named as its operand, so a
    payload whose `shim` or `docs` is a link would have these commands
    rewrite modes throughout the target instead."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    os.makedirs(args.prefix, exist_ok=True)
    open(os.path.join(args.prefix, deploy.PAYLOAD_MARKER), "w").close()
    os.symlink(str(victim), os.path.join(args.prefix, "shim"))

    assert deploy.system_execute(args) == 5
    assert not any(c[:2] == ["chmod", "-R"] for c in calls), \
        "no recursive chmod may run once an entry is a symlink"
    assert not any(c[:2] == ["chown", "-R"] for c in calls), calls
    assert [c for c in calls if c[:2] == ["rm", "-rf"]], calls


def test_a_rejected_payload_is_removed_not_left_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: [deploy.Unowned("%s/shim/guard.sh" % root,
                                            deploy.UNOWNED_FOREIGN_UID,
                                            "owned by uid 1000")])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 5

    removed = [c[-1] for c in calls if c[:2] == ["rm", "-rf"]]
    for entry in deploy.INSTALLED_ENTRIES:
        assert os.path.join(args.prefix, entry) in removed, entry
    assert not any(c[:2] == ["chmod", "0755"] for c in calls), \
        "a rejected prefix must stay root-only"


def test_deploy_refuses_to_wire_up_a_payload_it_could_not_make_root_owned(
        tmp_path, monkeypatch):
    """The whole of ADR-0004 rests on this, so a failure stops the install
    rather than being reported after the timer is already enabled."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: [deploy.Unowned("%s/shim/guard.sh" % root,
                                            deploy.UNOWNED_FOREIGN_UID,
                                            "owned by uid 1000")])

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 5

    assert not any("install.sh" in arg for c in calls for arg in c), calls
    assert not any(c[:2] == ["systemctl", "enable"] for c in calls), calls
    assert not any(c[:2] == ["systemctl", "daemon-reload"] for c in calls), calls
    assert [c for c in calls if c[:2] == ["systemctl", "stop"]], \
        "the previous install's timer must be disarmed before the payload moves"
    assert not os.path.exists(os.path.join(args.unit_dir, deploy.TIMER_UNIT))


def test_deploy_reasserts_root_ownership_before_anything_runs_it(
        tmp_path, monkeypatch):
    """The unit runs install.sh as root on every poll, so the chown has to
    land before install.sh is invoked, not merely somewhere in the
    sequence."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    pass_prefix_checks(monkeypatch)

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    def index_of(predicate):
        return next(i for i, c in enumerate(calls) if predicate(c))

    chown = index_of(lambda c: c[:2] == ["chown", "-R"] and "root:root" in c)
    chmod = index_of(lambda c: c[:2] == ["chmod", "-R"] and any("go-w" in a for a in c))
    installer = index_of(lambda c: any("install.sh" in arg for arg in c))
    # Copies INTO THE PREFIX: the spool's `install -d` deliberately lands
    # after the chown, and holds no payload.
    last_copy = max(i for i, c in enumerate(calls)
                    if c[0] in ("cp", "install")
                    and any(a.startswith(args.prefix) for a in c))

    assert last_copy < chown < installer, calls
    assert chmod < installer, calls


def test_the_ownership_check_covers_the_prefix_directory_itself(tmp_path,
                                                                monkeypatch):
    """Scoping the check to the installed entries would stop it examining
    the prefix DIRECTORY, whose owner can replace shim/ after the check and
    have the timer run it as root. The mutations stay scoped; the check
    does not."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    checked = []
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: checked.append(root) or [])
    monkeypatch.setattr(deploy, "run", recording_run([]))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0
    assert args.prefix in checked, checked


def test_the_ownership_check_runs_again_after_the_installer(tmp_path,
                                                            monkeypatch):
    """install.sh creates $prefix/bin -- the directory that holds the shims
    -- after the first assertion."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    checked = []
    calls = []
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: checked.append(len(calls)) or [])
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    assert deploy.system_execute(_args(tmp_path)) == 0
    installer = next(i for i, c in enumerate(calls)
                     if any("install.sh" in a for a in c))
    assert len(checked) >= 2, "the check runs once, before install.sh"
    assert any(at > installer for at in checked), \
        "no ownership check after install.sh created $prefix/bin"


def test_deploy_system_executes_when_root_and_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    pass_prefix_checks(monkeypatch)

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    assert any(c[0] == "install" and args.prefix in c for c in calls), calls
    assert any(
        any("install.sh" in arg for arg in c)
        and "--system" in c and "--dry-run" not in c
        for c in calls), calls
    assert any(c[:2] == ["systemctl", "daemon-reload"] for c in calls)
    assert any(c[:3] == ["systemctl", "enable", "--now"] for c in calls)

    service_path = os.path.join(args.unit_dir, deploy.SERVICE_UNIT)
    timer_path = os.path.join(args.unit_dir, deploy.TIMER_UNIT)
    assert os.path.exists(service_path)
    assert os.path.exists(timer_path)

    service_text = open(service_path).read()
    exec_start = next(
        line for line in service_text.splitlines()
        if line.startswith("ExecStart="))
    assert "--report" in exec_start
    assert "--kill" not in exec_start
    assert args.prefix in service_text
    assert "$HOME" not in service_text and "~" not in service_text

    timer_text = open(timer_path).read()
    assert "OnCalendar=%s" % deploy.TIMER_SLOT in timer_text
    assert "RandomizedDelaySec=%d" % deploy.TIMER_RANDOMIZED_DELAY_SEC in timer_text


def test_execute_runs_the_deployed_installer_without_path_flags(
        tmp_path, monkeypatch):
    """The install <-> install.sh pair: install.sh carries the same literals
    from the same build, so deploy.py hands it a mode and nothing else --
    from the DEPLOYED copy, through the trusted shell, on both arms."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0
    installer = next(c for c in calls if any("install.sh" in a for a in c))
    assert installer == [deploy.TRUSTED_SH,
                         os.path.join(args.prefix, "shim", "install.sh"),
                         "--system"], installer

    pass_uninstall_checks(monkeypatch, args.prefix)
    calls.clear()
    assert deploy.system_uninstall(args) == 0
    teardown = next(c for c in calls if any("install.sh" in a for a in c))
    assert teardown == [deploy.TRUSTED_SH,
                        os.path.join(args.prefix, "shim", "install.sh"),
                        "--uninstall"], teardown


def test_the_post_install_message_names_both_trails_and_the_record(
        tmp_path, monkeypatch, capsys):
    """Two audit trails, and the evidence for --kill needs both; plus the
    two files that say what is installed without executing anything."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0
    out = capsys.readouterr().out
    assert "tail %s" % os.path.join(args.spool_dir, "reaper-audit.jsonl") in out
    assert "journalctl -t walk-blocker -o json" in out
    assert "cat %s" % os.path.join(args.prefix, deploy.PAYLOAD_MARKER) in out
    assert "cat %s" % os.path.join(args.prefix, "site.lock.json") in out
    assert "report-only" in out


# --------------------------------------------------------------------------
# the trust chain and traversability
# --------------------------------------------------------------------------

def test_untraversable_prefix_is_refused_even_though_it_is_trusted(tmp_path):
    """Trusted is not the same as reachable: a root-owned prefix under a
    0700 ancestor passes every validator, and every user's shell then
    carries an unreachable directory on PATH."""
    closed = tmp_path / "rootlike"
    (closed / "walk-blocker").mkdir(parents=True)
    closed.chmod(0o700)
    try:
        blocked = deploy.untraversable_for_users(str(closed / "walk-blocker"))
        assert [(p, why) for p, why in blocked
                if p == str(closed) and "no o+x" in why], blocked
    finally:
        closed.chmod(0o755)

    closed.chmod(0o711)
    try:
        blocked = deploy.untraversable_for_users(str(closed / "walk-blocker"))
        assert str(closed) not in [p for p, _ in blocked], blocked
    finally:
        closed.chmod(0o755)


def test_untraversable_check_covers_the_prefix_and_the_spool(
        tmp_path, monkeypatch):
    """Both, and the unit directory neither: the trail must be readable by
    the account that decides --kill (ADR-0012), and the unit directory is
    the distribution's."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    checked = []
    monkeypatch.setattr(
        deploy, "untraversable_for_users",
        lambda prefix: checked.append(prefix) or [])
    monkeypatch.setattr(deploy, "run", recording_run([]))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0
    assert checked == [args.spool_dir, args.prefix], checked


def test_untrusted_prefix_chain_accepts_a_chain_owned_all_the_way_down(tmp_path):
    prefix = tmp_path / "a" / "b" / "walk-blocker"
    prefix.mkdir(parents=True)
    for d in (tmp_path / "a", tmp_path / "a" / "b", prefix):
        d.chmod(0o755)
    assert deploy.untrusted_prefix_chain(str(prefix), trusted_uids=(0, os.getuid())) == []


def test_untrusted_prefix_chain_flags_a_writable_ancestor(tmp_path):
    loose = tmp_path / "loose"
    prefix = loose / "walk-blocker"
    prefix.mkdir(parents=True)
    loose.chmod(0o777)
    try:
        chain = deploy.untrusted_prefix_chain(str(prefix), trusted_uids=(0, os.getuid()))
        assert [(p, why) for p, why in chain
                if p == str(loose) and "writable by group or other" in why], chain
    finally:
        loose.chmod(0o755)


def test_untrusted_prefix_chain_exempts_a_sticky_shared_directory(tmp_path):
    shared = tmp_path / "shared"
    prefix = shared / "walk-blocker"
    prefix.mkdir(parents=True)
    shared.chmod(0o1777)
    try:
        chain = deploy.untrusted_prefix_chain(str(prefix), trusted_uids=(0, os.getuid()))
        assert [p for p, _ in chain] == [], chain
    finally:
        shared.chmod(0o755)


def test_untrusted_prefix_chain_flags_a_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    chain = deploy.untrusted_prefix_chain(
        str(link / "walk-blocker"), trusted_uids=(0, os.getuid()))
    assert [(p, why) for p, why in chain
            if p == str(link) and "symlink" in why], chain


def test_untrusted_prefix_chain_ignores_components_not_yet_created(tmp_path):
    assert deploy.untrusted_prefix_chain(
        str(tmp_path / "not" / "yet" / "there"),
        trusted_uids=(0, os.getuid())) == []


def test_untrusted_prefix_chain_denies_the_sticky_exemption_to_the_prefix(
        tmp_path):
    """Sticky is enough for an ancestor but not for the prefix, where the
    risk is others CREATING entries in it -- planting a marker to authorize
    a root `rm -rf` of a sibling."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o1777)
    try:
        as_prefix = deploy.untrusted_prefix_chain(
            str(shared), trusted_uids=(0, os.getuid()))
        assert [(p, why) for p, why in as_prefix
                if p == str(shared) and "the prefix itself" in why], as_prefix

        (shared / "walk-blocker").mkdir()
        as_ancestor = deploy.untrusted_prefix_chain(
            str(shared / "walk-blocker"), trusted_uids=(0, os.getuid()))
        assert as_ancestor == [], as_ancestor
    finally:
        shared.chmod(0o755)


def test_untrusted_prefix_chain_rejects_a_dangling_symlink_component(tmp_path):
    """`os.path.exists()` FOLLOWS symlinks, so a dangling link read as
    "missing" and never reached the lstat-based rejection."""
    link = tmp_path / "prefix"
    link.symlink_to(tmp_path / "target-does-not-exist-yet")
    assert not os.path.exists(str(link))
    assert os.path.lexists(str(link))

    chain = deploy.untrusted_prefix_chain(str(link),
                                          trusted_uids=(0, os.getuid()))
    assert [(p, why) for p, why in chain
            if p == str(link) and "symlink" in why], chain


def test_untrusted_prefix_chain_refuses_a_missing_prefix_under_a_shared_parent(
        tmp_path):
    """The sticky bit protects existing entries; it does not reserve a
    missing NAME."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o1777)
    try:
        chain = deploy.untrusted_prefix_chain(
            str(shared / "not-yet"), trusted_uids=(0, os.getuid()))
        assert [(p, why) for p, why in chain
                if p == str(shared) and "does not exist yet" in why], chain

        (shared / "not-yet").mkdir()
        assert deploy.untrusted_prefix_chain(
            str(shared / "not-yet"), trusted_uids=(0, os.getuid())) == []
    finally:
        shared.chmod(0o755)


def test_a_doubled_leading_slash_cannot_dodge_the_prefix_checks(tmp_path):
    """`os.path.normpath` preserves EXACTLY two leading slashes."""
    assert deploy.canonical_prefix("//var/tmp") == "/var/tmp"
    assert deploy.canonical_prefix("///var/tmp") == "/var/tmp"
    assert deploy.canonical_prefix("/usr//local//lib/x") == "/usr/local/lib/x"

    plain = deploy.untrusted_prefix_chain("/var/tmp")
    doubled = deploy.untrusted_prefix_chain("//var/tmp")
    assert plain, "the single-slash spelling should already be refused"
    assert [why for _, why in doubled], \
        "the doubled spelling must be refused identically"
    assert [p for p, _ in doubled] == [p for p, _ in plain]


def test_the_canonical_prefix_is_what_every_command_acts_on(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    doubled = "/" + str(tmp_path / "prefix")
    args = _args(tmp_path, prefix=doubled)
    assert deploy.system_execute(args) == 0

    assert args.prefix == str(tmp_path / "prefix"), args.prefix
    for cmd in calls:
        for arg in cmd:
            assert "//" not in arg, cmd

    service = open(os.path.join(args.unit_dir, deploy.SERVICE_UNIT)).read()
    assert "//" not in service.replace("file://", ""), service


def test_deploy_refuses_an_untrusted_path_before_touching_anything(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(
        deploy, "untrusted_prefix_chain",
        lambda prefix, trusted_uids=(0,), trusted_gids=(): [("/tmp", "mode 0777 is writable by group or "
                                        "other without the sticky bit")])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 6
    assert calls == [], "nothing may run before the path is judged"
    assert not os.path.exists(os.path.join(args.unit_dir, deploy.TIMER_UNIT))


def test_the_spool_and_unit_dir_get_the_prefix_treatment(tmp_path, monkeypatch):
    """Both are root-write sinks beside the prefix: the spool is where the
    root-run reaper writes, and the unit directory is where root writes a
    unit it then executes. Moved through the CONSTANTS, so the real chain
    check is what refuses rather than not_a_default()."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    try:
        real = deploy.untrusted_prefix_chain
        monkeypatch.setattr(
            deploy, "untrusted_prefix_chain",
            lambda path, trusted_uids=(0,), trusted_gids=(): real(
                path, trusted_uids=(0, os.getuid()), trusted_gids=trusted_gids))

        monkeypatch.setattr(deploy, "DEFAULT_SPOOL_DIR", str(loose / "spool"))
        assert deploy.system_execute(_args(tmp_path)) == 6
        monkeypatch.setattr(deploy, "DEFAULT_SPOOL_DIR", str(tmp_path / "var-log"))
        monkeypatch.setattr(deploy, "DEFAULT_UNIT_DIR", str(loose / "units"))
        assert deploy.system_execute(_args(tmp_path)) == 6
        assert calls == [], "nothing may run before the paths are judged"
    finally:
        loose.chmod(0o755)


# --------------------------------------------------------------------------
# the marker
# --------------------------------------------------------------------------

def test_deploy_refuses_a_populated_directory_that_is_not_its_own(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    shared = tmp_path / "usr-local-lookalike"
    (shared / "docs").mkdir(parents=True)          # somebody else's docs
    # The CONSTANT moves, not the namespace: an override would be refused
    # by not_a_default() and this test would pass for the wrong reason.
    monkeypatch.setattr(deploy, "DEFAULT_PREFIX", str(shared))
    args = _args(tmp_path)

    assert deploy.system_execute(args) == 6
    assert not any(c[0] == "rm" for c in calls), calls

    (shared / deploy.PAYLOAD_MARKER).write_text("")
    calls.clear()
    assert deploy.system_execute(args) == 0
    assert any(c[0] == "rm" for c in calls)


def test_deploy_marks_the_directory_as_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    marker = os.path.join(args.prefix, deploy.PAYLOAD_MARKER)
    installs = [i for i, c in enumerate(calls) if c[0] == "install"]
    marker_at = next(i for i, c in enumerate(calls) if marker in c)
    removals = [i for i, c in enumerate(calls) if c[0] == "rm"]
    assert marker_at < min(removals), \
        "the marker must be written before anything is deleted"
    assert min(installs) < marker_at


def test_the_payload_marker_carries_the_version(tmp_path, monkeypatch):
    """`cat $PREFIX/.walk-blocker-payload` answers "what is installed
    here?" with no privilege and no execution. Generated into the snapshot,
    so every filesystem effect of an install stays inside run()."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    staged = next(c[3] for c in calls
                  if c[:2] == ["install", "-m"]
                  and c[-1].endswith(deploy.PAYLOAD_MARKER))
    with open(staged) as fh:
        assert fh.read().strip() == deploy.__version__ == walk_blocker.__version__


def test_the_marker_is_installed_before_the_code_it_describes(
        tmp_path, monkeypatch):
    """The marker is the answer that survives a payload interrupted
    mid-install, which is only true while it is installed FIRST. The
    DESTINATION is matched exactly: the staging copy installs the same
    basenames first."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    def index_of(name):
        target = os.path.join(args.prefix, name)
        return next(i for i, c in enumerate(calls)
                    if c[0] == "install" and c[-1] == target)

    assert index_of(deploy.PAYLOAD_MARKER) < index_of("reaper.py")
    assert index_of("reaper.py") < index_of("search_rules.py")


def test_the_dry_run_names_the_version_and_writes_nothing(
        tmp_path, monkeypatch):
    """The marker is the ONE install line that does not go through run() on
    the dry-run path, so both halves are pinned: the line is printed with
    the version, and no marker install reaches run()."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    args = _args(tmp_path, dry_run=True)

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = deploy.system_execute(args)
    monkeypatch.undo()
    assert rc == 0, out.getvalue()

    marker = os.path.join(args.prefix, deploy.PAYLOAD_MARKER)
    assert "would write: %s (walk-blocker %s)" % (
        marker, deploy.__version__) in out.getvalue(), out.getvalue()
    assert not [c for c in calls
                if c[0] == "install" and c[-1].endswith(deploy.PAYLOAD_MARKER)]


# --------------------------------------------------------------------------
# the snapshot
# --------------------------------------------------------------------------

def test_the_install_copies_from_a_snapshot_not_the_payload_directory(
        tmp_path, monkeypatch):
    """The destination is root-owned from creation and verified, but that
    authenticates the destination's METADATA, not the bytes that arrived."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    args = _args(tmp_path)

    assert deploy.system_execute(args) == 0

    into_prefix = [c for c in calls if c[0] in ("install", "cp")
                   and c[-1].startswith(args.prefix)]
    assert into_prefix, calls
    from_repo = [c for c in into_prefix
                 if any(a.startswith(deploy.REPO) for a in c[:-1])]
    assert from_repo == [], from_repo

    staged = next(i for i, c in enumerate(calls)
                  if c[0] == "cp" and "walk-blocker-stage." in " ".join(c))
    created = next(i for i, c in enumerate(calls)
                   if c[0] == "install" and c[-1] == args.prefix)
    assert staged < created, calls[:6]


def test_the_snapshot_is_taken_after_the_checks_and_before_the_first_systemctl(
        tmp_path, monkeypatch):
    """ADR-0006's timing half: after everything that can refuse (pure
    Python, none of it blocking) and before anything that can block."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_execute(_args(tmp_path)) == 0

    first_systemctl = next(i for i, c in enumerate(calls) if c[0] == "systemctl")
    # Commands whose TARGET is under the staging parent: the snapshot being
    # taken. The snapshot path also appears later as the SOURCE of every
    # copy into the prefix, which is the other half of the same design.
    staging = [i for i, c in enumerate(calls)
               if c[-1].startswith(deploy.STAGING_PARENT + os.sep)]
    assert staging, calls
    assert max(staging) < first_systemctl, calls[:first_systemctl + 1]
    assert all(c[0] in ("install", "cp") for c in calls[:first_systemctl]), \
        "nothing but the snapshot runs before systemd is touched"


def test_the_snapshot_covers_every_payload_source(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    staged = {arg for c in calls for arg in c if "walk-blocker-stage." in arg}
    for relative, _is_dir, _mode in deploy.PAYLOAD_SOURCES:
        assert any(p.endswith("/" + relative) for p in staged), relative


def test_a_dry_run_creates_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    before = set(os.listdir("/tmp"))
    args = _args(tmp_path, dry_run=True)
    assert deploy.system_execute(args) == 0
    new = {n for n in set(os.listdir("/tmp")) - before
           if n.startswith("walk-blocker-stage.")}
    assert new == set(), new
    assert os.listdir(deploy.STAGING_PARENT) == []

    # The planned path, not the payload directory and not a real snapshot:
    # under the pinned parent, shaped like the one the install creates, and
    # still nothing on disk after asking for it.
    planned = deploy.stage_payload(dry_run=True)
    assert planned.startswith(deploy.STAGING_PARENT + os.sep), planned
    assert os.path.basename(planned).startswith(deploy.STAGING_PREFIX), planned
    assert planned != deploy.REPO
    assert not os.path.exists(planned), planned
    assert os.listdir(deploy.STAGING_PARENT) == []


def _payload_copies(calls, prefix):
    """The commands that put payload sources INTO THE PREFIX, with the
    snapshot's random leaf normalised away.

    Keyed on the destination, because the real install also copies REPO into
    the snapshot and a dry run has no counterpart for that -- those are the
    staging half, and `test_the_snapshot_covers_every_payload_source` is what
    holds them. What belongs here is the half both legs perform.

    TWO EXCLUSIONS, both deliberate, because an exclusion nobody can see is
    where the next divergence hides:

    - `install -d`, which creates the prefix and names no source;
    - the payload MARKER, which is generated rather than copied and is the
      one install line that deliberately does not reach `run()` on the dry
      path. `test_the_dry_run_names_the_version_and_writes_nothing` pins both
      halves of that exception; it is not this test's to re-litigate.

    `mkdtemp` picks the leaf, so the two legs cannot be compared literally:
    the install has a real one, a dry run has none to have. Normalising it is
    what leaves everything else -- which file, from where, to where, with
    which mode -- compared exactly."""
    out = []
    for cmd in calls:
        if cmd[0] not in ("install", "cp") or cmd[:2] == ["install", "-d"]:
            continue
        if not cmd[-1].startswith(prefix):
            continue
        if cmd[-1].endswith(deploy.PAYLOAD_MARKER):
            continue
        out.append([re.sub(r"walk-blocker-stage\.[^/]*",
                           "walk-blocker-stage.<leaf>", arg) for arg in cmd])
    return out


def test_the_dry_run_copies_from_where_the_install_copies_from(tmp_path,
                                                               monkeypatch):
    """The preview is the install's own plan, not a second account of it.

    A dry run creates no snapshot, so it has no leaf to name -- but naming
    the payload directory instead made the previewed command sequence differ
    from the executed one in every copy, which is the one thing an operator
    reads it to check (#54). The leaf is normalised on both sides; nothing
    else is.

    This fails on the commit before the fix, with REPO on the left and the
    snapshot on the right."""
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    prefix = _args(tmp_path).prefix

    dry_calls = []
    monkeypatch.setattr(deploy, "run", recording_run(dry_calls))
    assert deploy.system_execute(_args(tmp_path, dry_run=True)) == 0

    real_calls = []
    monkeypatch.setattr(deploy, "run", recording_run(real_calls))
    assert deploy.system_execute(_args(tmp_path)) == 0

    previewed = _payload_copies(dry_calls, prefix)
    executed = _payload_copies(real_calls, prefix)
    assert executed, "the real install copied nothing -- the test proves nothing"
    assert previewed == executed


def test_the_source_tree_is_not_required_to_be_root_owned(tmp_path,
                                                          monkeypatch):
    """ADR-0006: a sysadmin unpacks the payload into a folder xe owns and
    runs this as root. Refusing a non-root-owned source was proposed three
    times and rejected each time; this pins the decision. REPO here really
    is user-owned, so a check on the source would fire."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert os.stat(deploy.REPO).st_uid != 0 or os.getuid() == 0, \
        "this test means nothing if the payload directory is already root's"
    assert deploy.system_execute(_args(tmp_path)) == 0


def test_the_snapshot_parent_is_pinned_not_taken_from_tmpdir(monkeypatch):
    """tempfile's default parent comes from TMPDIR, so an environment
    variable would decide where the root-owned 0700 snapshot lived."""
    monkeypatch.undo()
    assert deploy.STAGING_PARENT == VALUES["site.toml:install.staging_parent"]
    source = source_text()
    body = source[source.index("def stage_payload("):]
    body = body[:body.index("\ndef ")]
    assert "dir=STAGING_PARENT" in body, "mkdtemp must be given the parent"
    assert "untrusted_prefix_chain(STAGING_PARENT)" in body, \
        "the pinned parent is checked, not assumed"


def test_an_untrusted_snapshot_parent_is_refused_not_worked_around(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        deploy, "untrusted_prefix_chain",
        lambda p, trusted_uids=(0,), trusted_gids=(): [(p, "owned by uid 1000")])
    monkeypatch.setattr(deploy, "run", recording_run([]))
    try:
        deploy.stage_payload()
    except SystemExit as exit_code:
        assert exit_code.code == 6
    else:
        raise AssertionError("an untrusted staging parent must be refused")


# --------------------------------------------------------------------------
# systemd state
# --------------------------------------------------------------------------

def test_the_previous_timer_is_disarmed_before_the_payload_moves(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    stops = [i for i, c in enumerate(calls)
             if c[:2] == ["systemctl", "stop"]
             or c[:3] == ["systemctl", "disable", "--now"]]
    # Scoped to the PREFIX: the staging snapshot also uses cp/install, but
    # writes to a private root-only directory, not to anything the running
    # timer executes.
    first_mutation = min(i for i, c in enumerate(calls)
                         if c[0] in ("rm", "cp", "install")
                         and c[-1].startswith(args.prefix))
    enables = [i for i, c in enumerate(calls)
               if c[:2] == ["systemctl", "enable"]]
    assert stops, calls
    assert max(stops) < first_mutation, "disarm has to precede the rm -rf"
    assert any(c[:3] == ["systemctl", "disable", "--now"] for c in calls), \
        "stop alone leaves it enabled, so it re-arms at the next reboot"
    assert min(enables) > max(stops), "re-armed only after validation"


def test_deploy_aborts_when_a_unit_refuses_to_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run",
                        recording_run(calls, active_units=(deploy.TIMER_UNIT,)))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 7
    assert not any(c[0] in ("rm", "cp") and c[-1].startswith(args.prefix)
                   for c in calls), \
        "the payload must not move while a unit is still active"
    assert not os.path.exists(os.path.join(args.unit_dir, deploy.TIMER_UNIT))


def test_deploy_refuses_a_timer_that_stopped_but_stayed_enabled(tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(
        deploy, "run",
        recording_run(calls, enabled_units=(deploy.TIMER_UNIT,)))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 7
    assert not any(c[0] in ("rm", "cp") and c[-1].startswith(args.prefix)
                   for c in calls), \
        "the payload must not move while the timer is still armed"


def test_deploy_proceeds_when_the_timer_was_never_installed(tmp_path,
                                                             monkeypatch):
    """A from-scratch node has no timer at all -- the ordinary first-install
    case -- and `show --value` answers "" for it, exit 0."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []

    def never_installed(cmd, check=True, capture=True, dry_run=False,
                        env=None):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        if cmd[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(deploy, "run", never_installed)

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0
    assert any(c[0] == "cp" and c[-1].startswith(args.prefix) for c in calls), \
        "the payload should have been installed, not refused"


def _query_fails(calls):
    def query_fails(cmd, check=True, capture=True, dry_run=False, env=None):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        if cmd[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(
                cmd, 1, "", "Failed to connect to bus: No such file "
                          "or directory\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return query_fails


def test_deploy_refuses_when_the_enablement_query_itself_fails(tmp_path,
                                                                monkeypatch):
    """Empty stdout is the SAME shape whether the timer has no unit file
    (safe) or the query itself failed. The returncode tells them apart."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", _query_fails(calls))

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 7
    assert not any(c[0] in ("rm", "cp") and c[-1].startswith(args.prefix)
                   for c in calls), \
        "the payload must not move when enablement could not be determined"


def test_uninstall_refuses_when_the_enablement_query_itself_fails(tmp_path,
                                                                   monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    args = _args(tmp_path)
    pass_uninstall_checks(monkeypatch, args.prefix)
    calls = []
    monkeypatch.setattr(deploy, "run", _query_fails(calls))
    assert deploy.system_uninstall(args) == 7
    assert not any("install.sh" in a for c in calls for a in c), \
        "nothing may be torn down while enablement could not be determined"


def test_uninstall_stops_the_service_not_just_the_timer(tmp_path, monkeypatch):
    """A running service instance's ExecStartPre is `install.sh --relink`,
    which would recreate the shims this uninstall just removed."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    args = _args(tmp_path)
    pass_uninstall_checks(monkeypatch, args.prefix)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    assert deploy.system_uninstall(args) == 0
    stops = [c for c in calls if c[:2] == ["systemctl", "stop"]]
    assert any(deploy.SERVICE_UNIT in c for c in stops), calls
    probe = next(i for i, c in enumerate(calls)
                 if c[:2] == ["systemctl", "is-active"])
    removal = next(i for i, c in enumerate(calls)
                   if any("install.sh" in a for a in c))
    assert probe < removal, "confirm it is stopped before removing the shims"


def test_uninstall_verifies_the_timer_not_only_the_service(tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    args = _args(tmp_path)
    pass_uninstall_checks(monkeypatch, args.prefix)

    calls = []
    monkeypatch.setattr(
        deploy, "run",
        recording_run(calls, active_units=(deploy.TIMER_UNIT,)))
    assert deploy.system_uninstall(args) == 7
    assert not any("install.sh" in a for c in calls for a in c), \
        "nothing may be removed while the timer is live"

    calls.clear()
    monkeypatch.setattr(
        deploy, "run",
        recording_run(calls, enabled_units=(deploy.TIMER_UNIT,)))
    assert deploy.system_uninstall(args) == 7, \
        "stopped-but-enabled still leaves a loadable unit"

    calls.clear()
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_uninstall(args) == 0
    assert any("install.sh" in a for c in calls for a in c)


# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------

def test_uninstall_runs_the_deployed_helper_not_the_payload_directory(
        tmp_path, monkeypatch):
    """deploy.py's bytes are fixed once loaded, but install.sh is opened
    later and sources wrapped_names.sh later still, so the directory's owner
    can replace either after the root operator started."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    args = _args(tmp_path)
    pass_uninstall_checks(monkeypatch, args.prefix)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    assert deploy.system_uninstall(args) == 0

    installer = next(arg for c in calls for arg in c if "install.sh" in arg)
    assert installer.startswith(args.prefix), installer
    assert not installer.startswith(deploy.REPO), installer


def test_uninstall_refuses_rather_than_falling_back_to_the_payload_directory(
        tmp_path, monkeypatch, capsys):
    """A fallback would reopen exactly the path being closed, so a missing
    deployed helper is a refusal -- with the manual steps, naming the
    enabled hook files."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    args = _args(tmp_path)
    pass_uninstall_checks(monkeypatch, args.prefix)
    os.unlink(os.path.join(args.prefix, "shim", "install.sh"))
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    assert deploy.system_uninstall(args) == 5
    assert not [arg for c in calls for arg in c if "install.sh" in arg]
    err = capsys.readouterr().err
    for path in deploy.enabled_hook_files(args):
        assert path in err, (path, err)
    assert os.path.join(args.prefix, "bin") in err


def test_the_uninstall_helper_must_be_root_owned_and_not_a_symlink(tmp_path,
                                                                   monkeypatch):
    """Both files, because install.sh SOURCES wrapped_names.sh into its own
    shell -- so a swap of either is root execution."""
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    staged = tmp_path / "prefix" / "shim"
    staged.mkdir(parents=True)
    for name in ("install.sh", "wrapped_names.sh"):
        (staged / name).write_text("")
    prefix = str(tmp_path / "prefix")

    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: [deploy.Unowned(root, deploy.UNOWNED_FOREIGN_UID,
                                            "owned by uid %d" % os.getuid())])
    assert deploy.uninstall_helper(prefix)[0] is None

    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    helper, why = deploy.uninstall_helper(prefix)
    assert why is None, why
    assert helper == str(staged / "install.sh")

    (staged / "install.sh").unlink()
    os.symlink(str(tmp_path / "elsewhere.sh"), str(staged / "install.sh"))
    assert deploy.uninstall_helper(prefix)[0] is None

    (staged / "install.sh").unlink()
    (staged / "install.sh").write_text("")
    (staged / "wrapped_names.sh").unlink()
    os.symlink(str(tmp_path / "other.sh"), str(staged / "wrapped_names.sh"))
    assert deploy.uninstall_helper(prefix)[0] is None


def _uninstall_with_a_planted_link(tmp_path, monkeypatch, attr):
    """Plant a symlink to a 0600 secret where the hook file named by `attr`
    would be, under a directory others can write, and run the uninstall.
    Returns (rc, calls, secret, link)."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    shared = tmp_path / "shared"
    shared.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("SECRET\n")
    secret.chmod(0o600)
    link = shared / "hookfile"
    link.symlink_to(secret)

    prefix = tmp_path / "p"
    prefix.mkdir()
    (prefix / deploy.PAYLOAD_MARKER).write_text("")

    real = deploy.untrusted_prefix_chain
    monkeypatch.setattr(
        deploy, "untrusted_prefix_chain",
        lambda path, trusted_uids=(0,), trusted_gids=(): real(
            path, trusted_uids=(0, os.getuid()), trusted_gids=trusted_gids))
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    shared.chmod(0o777)          # the precondition: others can plant the link
    try:
        args = _args(tmp_path, prefix=str(prefix), **{attr: str(link)})
        rc = deploy.system_uninstall(args)
    finally:
        shared.chmod(0o755)
    return rc, calls, secret, link


@pytest.mark.parametrize("attr", ["bashrc_file", "zshenv_file", "fish_conf_file"])
def test_uninstall_validates_every_hook_file_it_writes_not_only_the_prefix(
        tmp_path, monkeypatch, attr):
    """`strip_block()` READS the hook file and replaces it with a
    root-created 0644 regular file, so a symlink planted in a writable
    directory had its target's contents copied out world-readable. Every
    hook file, fish's drop-in included: it is `rm -f`'d on uninstall
    regardless of its gate."""
    rc, calls, secret, link = _uninstall_with_a_planted_link(tmp_path, monkeypatch, attr)
    assert rc == 6
    assert not any("install.sh" in a for c in calls for a in c), calls
    assert secret.stat().st_mode & 0o777 == 0o600
    assert os.path.islink(str(link)), "the link must be untouched"


def test_a_disabled_hooks_file_is_still_validated_as_a_path(tmp_path, monkeypatch):
    """Disabled means never written or stripped; it does not mean unchecked.
    The cost is a stat, and a hand-edited copy that re-enables the hook
    would otherwise reach strip_block() through an unvalidated path."""
    monkeypatch.setattr(deploy, "HOOK_ENABLED_FISH", False)
    assert [shell for _a, shell, enabled in deploy.hook_table() if enabled] == \
        ["bash", "zsh"]
    rc, calls, secret, link = _uninstall_with_a_planted_link(
        tmp_path, monkeypatch, "fish_conf_file")
    assert rc == 6
    assert calls == [], calls
    assert os.path.islink(str(link))

    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    fifo = tmp_path / "fifo"
    os.mkfifo(str(fifo))
    assert deploy.validate_root_write_paths(
        _args(tmp_path, fish_conf_file=str(fifo)), attrs=("fish_conf_file",)) == 6


def test_a_disabled_hook_is_not_named_where_the_operator_is_told_to_look(
        tmp_path, monkeypatch, capsys):
    """The manual-steps and the "check the hook files" messages list the
    files install.sh actually writes on this site; naming a disabled hook's
    file would send an operator to strip a block that was never there."""
    monkeypatch.setattr(deploy, "HOOK_ENABLED_ZSH", False)
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    args = _args(tmp_path)
    assert deploy.enabled_hook_files(args) == [args.bashrc_file, args.fish_conf_file]
    pass_uninstall_checks(monkeypatch, args.prefix)
    os.unlink(os.path.join(args.prefix, "shim", "install.sh"))
    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert deploy.system_uninstall(args) == 5
    err = capsys.readouterr().err
    assert args.bashrc_file in err and args.fish_conf_file in err
    assert args.zshenv_file not in err


def test_uninstall_reports_a_teardown_it_did_not_achieve(tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    args = _args(tmp_path)
    pass_uninstall_checks(monkeypatch, args.prefix)

    calls = []

    def rm_fails(cmd, check=True, capture=True, dry_run=False, env=None):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        if cmd[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(cmd, 0, "disabled\n", "")
        if cmd[0] == "rm":
            return subprocess.CompletedProcess(cmd, 1, "", "read-only fs")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(deploy, "run", rm_fails)
    assert deploy.system_uninstall(args) == 8

    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert deploy.system_uninstall(args) == 0, "a clean teardown still reports 0"


def test_uninstall_refuses_an_unmarked_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    shared = tmp_path / "usr-local-lookalike"
    (shared / "bin").mkdir(parents=True)
    monkeypatch.setattr(deploy, "DEFAULT_PREFIX", str(shared))
    assert deploy.system_uninstall(_args(tmp_path)) == 6
    assert not any("install.sh" in arg for c in calls for arg in c), calls


def test_uninstall_refuses_a_symlinked_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    link = tmp_path / "x"
    link.symlink_to(tmp_path / "real")
    (tmp_path / "real").mkdir()
    open(str(link / deploy.PAYLOAD_MARKER), "w").close()

    chain = deploy.untrusted_prefix_chain(str(link),
                                          trusted_uids=(0, os.getuid()))
    assert [why for _, why in chain if "symlink" in why], chain

    assert deploy.system_uninstall(_args(tmp_path, prefix=str(link))) == 6
    assert not any("install.sh" in arg for c in calls for arg in c), calls


def test_uninstall_does_not_claim_removal_it_did_not_achieve(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    args = _args(tmp_path)
    pass_uninstall_checks(monkeypatch, args.prefix)

    calls = []

    def failing_run(cmd, check=True, capture=True, dry_run=False, env=None):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        if cmd[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(cmd, 0, "disabled\n", "")
        rc = 1 if any("install.sh" in a for a in cmd) else 0
        return subprocess.CompletedProcess(cmd, rc, "", "")

    monkeypatch.setattr(deploy, "run", failing_run)
    assert deploy.system_uninstall(args) == 8
    assert any("install.sh" in a for c in calls for a in c), calls


def test_uninstall_refuses_a_prefix_that_would_delete_system_binaries(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    assert deploy.system_uninstall(_args(tmp_path, prefix="/")) == 6
    assert calls == [], "nothing may run before the prefix is judged"


# --------------------------------------------------------------------------
# unit files
# --------------------------------------------------------------------------

def exec_lines(prefix):
    """The rendered service's directives starting with `prefix`, as
    (directive, value) pairs."""
    service, _timer = deploy.render_units("/PFX", "/SPOOL")
    return [tuple(line.split("=", 1)) for line in service.splitlines()
            if line.startswith(prefix)]


def timer_lines():
    _service, timer = deploy.render_units("/PFX", "/SPOOL")
    return dict(tuple(line.split("=", 1)) for line in timer.splitlines()
                if "=" in line and not line.startswith(("#", "[")))


def test_unit_files_are_0644_regardless_of_the_root_shells_umask(tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))

    old = os.umask(0)
    try:
        args = _args(tmp_path)
        assert deploy.system_execute(args) == 0
        for name in (deploy.SERVICE_UNIT, deploy.TIMER_UNIT):
            mode = os.stat(os.path.join(args.unit_dir, name)).st_mode & 0o777
            assert mode == 0o644, "%s is %o under umask 000" % (name, mode)
    finally:
        os.umask(old)


def test_created_unit_dir_intermediates_are_not_world_writable(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))

    old = os.umask(0)
    try:
        monkeypatch.setattr(deploy, "DEFAULT_UNIT_DIR",
                            str(tmp_path / "new" / "units"))
        args = _args(tmp_path)
        assert deploy.system_execute(args) == 0
        for path in (tmp_path / "new", tmp_path / "new" / "units"):
            mode = path.stat().st_mode & 0o777
            assert not mode & 0o022, "%s is %o" % (path, mode)
    finally:
        os.umask(old)


def test_write_unit_reasserts_ownership_not_only_mode(tmp_path, monkeypatch):
    unit = tmp_path / deploy.SERVICE_UNIT
    unit.write_text("stale\n")

    chowned = []
    real_fchown = os.fchown
    monkeypatch.setattr(
        os, "fchown",
        lambda fd, uid, gid: chowned.append((uid, gid)))
    deploy.write_unit(str(unit), "[Unit]\n")
    assert chowned == [(0, 0)], chowned
    assert unit.read_text() == "[Unit]\n"
    assert unit.stat().st_mode & 0o777 == 0o644

    monkeypatch.setattr(os, "fchown", real_fchown)
    deploy.write_unit(str(unit), "[Unit]\n")          # no raise as ourselves

    def refuse(fd, uid, gid):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fchown", refuse)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    try:
        deploy.write_unit(str(unit), "[Unit]\n")
        raise AssertionError("root must not swallow a failed fchown")
    except PermissionError:
        pass


def test_a_unit_path_that_is_a_symlink_is_not_followed(tmp_path):
    target = tmp_path / "precious"
    target.write_text("do not truncate me\n")
    link = tmp_path / deploy.SERVICE_UNIT
    link.symlink_to(target)

    with pytest.raises(OSError):
        deploy.write_unit(str(link), "[Unit]\n")
    assert target.read_text() == "do not truncate me\n"


def test_the_reaper_ships_report_only():
    """Promoting to --kill is a decision someone makes after reading real
    findings. Checked against ExecStart specifically: `--kill-after` on the
    unrelated ExecStartPre legitimately puts `--kill` elsewhere in the unit."""
    _directive, command = exec_lines("ExecStart=")[0]
    assert "--report" in command
    assert "--kill" not in command


def test_layer_2_does_not_depend_on_layer_1_housekeeping():
    """The availability inversion: without the `-`, any refusal reachable
    from install.sh's relink arm stopped the reaper -- the layer that exists
    BECAUSE Layer 1 is bypassable made to depend on Layer 1's bookkeeping."""
    pre = exec_lines("ExecStartPre=")
    assert len(pre) == 1, pre
    _directive, command = pre[0]
    assert command.startswith("-"), (
        "a Layer 1 refusal must not suppress ExecStart: %r" % command)


def test_a_hung_relink_cannot_eat_the_units_start_timeout():
    """The `-` ignores a non-zero EXIT, not a hang: so the bound is
    explicit -- the compiled `timeout` binary, `--kill-after` from
    `[timer].relink_kill_after_s`, the duration from
    `[timer].relink_timeout_s`, the compiled shell, and the deployed
    installer with no path flags. systemd does not search PATH, so every
    command token is absolute."""
    _directive, command = exec_lines("ExecStartPre=")[0]
    words = command.lstrip("-").split()
    assert words[0] == deploy.TRUSTED_TIMEOUT and words[0].startswith("/"), command
    assert words[1] == "--kill-after=%d" % deploy.RELINK_KILL_AFTER_S, command
    assert words[2] == str(deploy.RELINK_TIMEOUT_S) and int(words[2]) > 0, command
    assert words[3] == deploy.TRUSTED_SH and words[3].startswith("/"), command
    assert words[4] == "/PFX/shim/install.sh", command
    assert words[5:] == ["--relink"], command


def test_the_kill_after_grace_period_is_positive_and_bounded():
    """`--kill-after` only helps if it is neither 0 nor large enough to
    itself threaten the service's start budget alongside the initial wait."""
    _directive, command = exec_lines("ExecStartPre=")[0]
    words = command.lstrip("-").split()
    grace = int(words[1].split("=", 1)[1])
    duration = int(words[2])
    assert 0 < grace, words[1]
    timeout_start = int(dict(exec_lines("TimeoutStartSec="))["TimeoutStartSec"])
    assert timeout_start == deploy.TIMEOUT_START_SEC
    assert duration + grace < timeout_start, (
        "timeout plus kill-after must stay clear of TimeoutStartSec: %r" % command)


def test_the_reapers_own_exit_status_still_reaches_the_unit():
    """The asymmetry is the point: exactly one Exec* directive tolerates
    failure, and it is the housekeeping one."""
    start = exec_lines("ExecStart=")
    assert len(start) == 1, start
    _directive, command = start[0]
    assert not command.startswith("-"), (
        "the finding alarm dies if the reaper's exit status is ignored")
    words = command.split()
    assert words[0] == deploy.TRUSTED_PYTHON3
    assert words[1] == "/PFX/reaper.py"
    assert words[2:] == ["--report", "--spool", "/SPOOL"], command
    assert ("SuccessExitStatus", "0") in exec_lines("SuccessExitStatus=")

    tolerant = [d for d, c in exec_lines("Exec") if c.startswith("-")]
    assert tolerant == ["ExecStartPre"], tolerant


def test_the_timer_carries_the_stamped_slot_and_settings(monkeypatch):
    """The slot is site config chosen against the live schedule, and the
    jitter and coalescing settings are what would undo it; all four come
    from the compiled constants, and a change to any of them is carried."""
    lines = timer_lines()
    assert lines["OnCalendar"] == deploy.TIMER_SLOT == VALUES["site.toml:timer.on_calendar"]
    assert lines["RandomizedDelaySec"] == str(deploy.TIMER_RANDOMIZED_DELAY_SEC)
    assert lines["AccuracySec"] == deploy.TIMER_ACCURACY_SEC
    assert lines["Persistent"] == "false"
    assert lines["WantedBy"] == "timers.target"

    monkeypatch.setattr(deploy, "TIMER_SLOT", "*:03,33:15")
    monkeypatch.setattr(deploy, "TIMER_RANDOMIZED_DELAY_SEC", 5)
    monkeypatch.setattr(deploy, "TIMER_ACCURACY_SEC", "500ms")
    monkeypatch.setattr(deploy, "TIMER_PERSISTENT", True)
    lines = timer_lines()
    assert lines["OnCalendar"] == "*:03,33:15"
    assert lines["RandomizedDelaySec"] == "5"
    assert lines["AccuracySec"] == "500ms"
    assert lines["Persistent"] == "true"


def test_the_unit_descriptions_carry_the_sites_display_name():
    service, timer = deploy.render_units("/PFX", "/SPOOL")
    for text in (service, timer):
        description = next(l for l in text.splitlines() if l.startswith("Description="))
        assert deploy.DISPLAY_NAME in description, description
        assert VALUES["site.toml:site.display_name"] in description


def test_the_payload_does_not_live_on_the_filesystem_it_watches():
    """A home directory may be on the filesystem under investigation."""
    service, timer = deploy.render_units("/PFX", "/SPOOL")
    for text in (service, timer):
        assert "$HOME" not in text
        assert "~" not in text
    assert "never under a home directory" in deploy.__doc__


def test_the_preview_prints_the_rendered_units_the_install_writes(
        tmp_path, monkeypatch, capsys):
    """Preview <-> execute parity on the units: the preview prints the two
    unit files rendered from the same constants and paths the install
    writes, line for line, with the site's values in them."""
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    args = _args(tmp_path)
    assert deploy.system_preview(args) == 0
    previewed = capsys.readouterr().out

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    assert deploy.system_execute(args) == 0
    for name in (deploy.SERVICE_UNIT, deploy.TIMER_UNIT):
        path = os.path.join(args.unit_dir, name)
        written = open(path).read()
        assert "# --- %s ---" % path in previewed
        for line in written.rstrip("\n").splitlines():
            assert line in previewed.splitlines(), line
    assert "OnCalendar=%s" % VALUES["site.toml:timer.on_calendar"] in previewed
    assert "TimeoutStartSec=%d" % VALUES["site.toml:timer.timeout_start_sec"] in previewed
    assert "ExecStart=%s %s/reaper.py --report --spool %s" % (
        VALUES["site.toml:trusted_binaries.python3"], args.prefix, args.spool_dir) in previewed


# --------------------------------------------------------------------------
# hook files as root-write sinks
# --------------------------------------------------------------------------

def test_a_symlinked_bashrc_file_is_refused_before_it_is_read(tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    secret = tmp_path / "secret"
    secret.write_text("PRIVATE\n")
    secret.chmod(0o600)
    link = tmp_path / "bashrc-link"
    os.symlink(str(secret), str(link))

    args = _args(tmp_path, bashrc_file=str(link))
    assert deploy.validate_root_write_paths(
        args, attrs=("bashrc_file",)) == 6
    assert secret.read_text() == "PRIVATE\n"
    assert stat.S_IMODE(os.stat(str(secret)).st_mode) == 0o600


def test_a_dangling_bashrc_symlink_is_refused_too(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    link = tmp_path / "dangling"
    os.symlink(str(tmp_path / "nope"), str(link))
    args = _args(tmp_path, bashrc_file=str(link))
    assert deploy.validate_root_write_paths(args, attrs=("bashrc_file",)) == 6


def test_a_bashrc_that_is_not_a_regular_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    fifo = tmp_path / "fifo"
    os.mkfifo(str(fifo))
    args = _args(tmp_path, bashrc_file=str(fifo))
    assert deploy.validate_root_write_paths(args, attrs=("bashrc_file",)) == 6


def test_a_regular_or_absent_bashrc_file_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    plain = tmp_path / "bashrc"
    plain.write_text("# existing\n")
    assert deploy.validate_root_write_paths(
        _args(tmp_path, bashrc_file=str(plain)), attrs=("bashrc_file",)) == 0
    assert deploy.validate_root_write_paths(
        _args(tmp_path, bashrc_file=str(tmp_path / "not-yet")),
        attrs=("bashrc_file",)) == 0


def test_a_bashrc_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    """`prepend_block` PRESERVES the existing contents, and install.sh's
    verify then has the shell source the whole file AS ROOT -- so a
    user-owned or group-writable hook file lets its owner choose what runs
    during the deploy."""
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    bashrc = tmp_path / "bashrc"
    bashrc.write_text("# somebody else's\n")

    args = _args(tmp_path, bashrc_file=str(bashrc))
    assert deploy.validate_root_write_paths(
        args, attrs=("bashrc_file",)) == 6

    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    assert deploy.validate_root_write_paths(
        _args(tmp_path, bashrc_file=str(bashrc)),
        attrs=("bashrc_file",)) == 0


def test_an_absent_bashrc_file_needs_no_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    calls = []
    monkeypatch.setattr(deploy, "unowned_by",
                        lambda root, uid=0: calls.append(root) or [])
    args = _args(tmp_path, bashrc_file=str(tmp_path / "not-yet"))
    assert deploy.validate_root_write_paths(args, attrs=("bashrc_file",)) == 0
    assert calls == [], "an absent file should not be ownership-checked"


def test_irregular_target_ignores_a_directory_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda p, trusted_uids=(0,), trusted_gids=(): [])
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    assert deploy.validate_root_write_paths(
        _args(tmp_path, unit_dir=str(unit_dir)), attrs=("unit_dir",)) == 0


def test_validate_covers_every_compiled_location():
    """PATH_KINDS and default_paths() name the same six locations, so a
    location added to one and not the other -- the install <-> uninstall
    divergence shape -- fails here."""
    assert sorted(attr for attr, _kind in deploy.PATH_KINDS) == \
        sorted(deploy.default_paths())
    assert dict(deploy.PATH_KINDS) == {
        "prefix": "dir", "spool_dir": "dir", "unit_dir": "dir",
        "bashrc_file": "file", "zshenv_file": "file", "fish_conf_file": "file"}


# --------------------------------------------------------------------------
# the literals
# --------------------------------------------------------------------------

def test_a_non_default_unit_dir_is_refused_by_execute(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path, unit_dir=str(tmp_path / "elsewhere"))
    assert deploy.system_execute(args) == 6
    assert calls == [], "refused before anything ran"
    assert not os.path.exists(args.prefix)


def test_a_non_default_unit_dir_is_refused_by_uninstall(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    prefix = str(tmp_path / "prefix")
    pass_uninstall_checks(monkeypatch, prefix)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path, prefix=prefix, unit_dir=str(tmp_path / "elsewhere"))
    assert deploy.system_uninstall(args) == 6
    assert calls == []


UNIT_UNSAFE = ' \t\n"\'\\%$'          # systemd word-splits, expands % and ${}
SHELL_UNSAFE = ';&|<>()[]{}`*?!#~ $"\'\\'  # sourced by every login shell


def test_the_installer_paths_are_shaped_for_every_sink_they_reach(monkeypatch):
    """One assertion per property the deleted validators used to check,
    over the COMPILED literals -- the schema asserts the same at build, and
    this is the half that would catch the schema and the generator
    drifting. The autouse fixture is undone first."""
    monkeypatch.undo()
    paths = deploy.default_paths()
    assert sorted(paths) == ["bashrc_file", "fish_conf_file", "prefix",
                             "spool_dir", "unit_dir", "zshenv_file"]

    for attr, value in sorted(paths.items()):
        assert os.path.isabs(value), attr
        assert value == deploy.canonical_prefix(value), \
            "%s is not in canonical form" % attr
        assert len([p for p in value.split(os.sep) if p]) >= 2, attr

    for attr in ("prefix", "spool_dir", "unit_dir"):
        assert not (set(paths[attr]) & set(UNIT_UNSAFE)), \
            "%s cannot go in a systemd unit line" % attr
    for attr in ("prefix", "spool_dir"):
        assert not (set(paths[attr]) & set(SHELL_UNSAFE)), \
            "%s is not safe to interpolate into a sourced shell block" % attr
    assert not (set(deploy.DEFAULT_AUDIT_FILENAME) & set(SHELL_UNSAFE + "/"))
    assert paths["spool_dir"].count(os.sep) >= 2
    for name in ("TRUSTED_TIMEOUT", "TRUSTED_SH", "TRUSTED_PYTHON3"):
        value = getattr(deploy, name)
        assert os.path.isabs(value) and not (set(value) & set(UNIT_UNSAFE)), name


def test_not_a_default_refuses_each_of_the_six_paths(tmp_path):
    for attr in deploy.default_paths():
        args = _args(tmp_path, **{attr: str(tmp_path / "elsewhere")})
        why = deploy.not_a_default(args)
        assert why is not None, attr
        assert attr in why, why
    assert deploy.not_a_default(_args(tmp_path)) is None


def test_a_non_default_path_is_refused_before_anything_runs(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    prefix = str(tmp_path / "prefix")
    pass_prefix_checks(monkeypatch)
    pass_uninstall_checks(monkeypatch, prefix)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    for func in (deploy.system_execute, deploy.system_uninstall):
        calls.clear()
        args = _args(tmp_path, unit_dir=str(tmp_path / "elsewhere"))
        assert func(args) == 6, func.__name__
        assert calls == [], "%s ran commands before refusing" % func.__name__


def test_the_previewed_command_is_the_command_that_installs(tmp_path, capsys,
                                                            monkeypatch):
    """Every advertised install command is deploy.py's and carries no path
    flags, and none advertises install.sh."""
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    deploy.system_preview(_args(tmp_path))
    out = capsys.readouterr().out

    advertised = [ln for ln in out.splitlines()
                  if ln.startswith("#   ") and "deploy.py" in ln
                  and "--system" in ln]
    assert advertised, out
    for line in advertised:
        assert line.strip().endswith("deploy.py --system"), line
        for flag in PATH_FLAGS:
            assert flag not in line, (flag, line)
    assert not [ln for ln in out.splitlines()
                if ln.lstrip("# ").strip().startswith("sh ")
                and "install.sh" in ln and "--system" in ln
                and "--relink" not in ln and "--version" not in ln], out


def test_the_preview_does_not_relay_the_standalone_installer_command(
        tmp_path, capsys, monkeypatch):
    """The child must actually EMIT the command for this to test anything.

    `recording_run` answers every call with empty stdout, so this assertion
    used to hold over zero relayed lines whatever the filter did -- vacuous
    before this flag was removed and still vacuous after it, for a second
    reason. The fixture below is the shape the filter exists to catch, plus
    a `--relink` line that must SURVIVE, so the test fails both when the
    filter stops stripping and when it strips too much.
    """
    pass_prefix_checks(monkeypatch)
    child = (
        "# System-wide install of walk-blocker Layer 1.\n"
        "#   sh /payload/shim/install.sh --system\n"
        "#   sh /payload/shim/install.sh --relink\n"
        "# trailer\n")
    monkeypatch.setattr(
        deploy, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, child, ""))
    deploy.system_preview(_args(tmp_path))
    out = capsys.readouterr().out

    offered = [l for l in out.splitlines()
               if l.lstrip("# ").strip().startswith("sh ")
               and "install.sh" in l and "--system" in l
               and "--relink" not in l and "--version" not in l]
    assert offered == [], offered
    # The reconcile command is not the standalone install and must not be
    # collateral: a filter that took it too would hide the one install.sh
    # command an operator may legitimately run by hand.
    assert "sh /payload/shim/install.sh --relink" in out, out
    assert "standalone command is omitted" in out
    assert _previewed_command(out)


def test_the_relay_filter_strips_a_command_but_not_prose(tmp_path, capsys,
                                                         monkeypatch):
    """The filter matches a command-shaped line only; the looser version
    once replaced a sentence in the middle of install.sh's own explanation."""
    pass_prefix_checks(monkeypatch)
    child = (
        "# preamble\n"
        "#   sh /somewhere/install.sh --system\n"
        "# NOT install.sh run with --system directly, because ...\n"
        "# trailer\n")
    monkeypatch.setattr(
        deploy, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, child, ""))
    deploy.system_preview(_args(tmp_path))
    out = capsys.readouterr().out

    assert "sh /somewhere/install.sh" not in out
    assert "standalone command is omitted" in out
    assert ("# NOT install.sh run with --system directly, because ..."
            in out)
    assert "# trailer" in out


def test_a_refusing_child_preview_is_relayed_not_swallowed(tmp_path, capsys,
                                                           monkeypatch):
    """install.sh checks filesystem state a literal cannot make true, so its
    refusal means the install would refuse too; no command is advertised."""
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(
        deploy, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 3, "", "install.sh: refusing hooks.bash.file: it is a symlink.\n"))
    assert deploy.system_preview(_args(tmp_path)) == 6
    captured = capsys.readouterr()
    assert "it is a symlink" in captured.err
    assert "would refuse too" in captured.err
    assert not [l for l in captured.out.splitlines()
                if l.startswith("#   ") and "deploy.py" in l
                and "--system" in l]


# --------------------------------------------------------------------------
# the audit directory: one direction of refusal (ADR-0012)
# --------------------------------------------------------------------------

def _record_spool_creation(monkeypatch, calls):
    """Log `create_spool()` into the same list `recording_run` fills, so a
    test can see where in the command sequence the spool is made. It is not
    a command -- it is fds -- which is why it needs its own marker."""
    real = deploy.create_spool

    def create(spool, gid, **kw):
        calls.append(["<create_spool>", spool, str(gid)])
        return real(spool, gid, **kw)
    monkeypatch.setattr(deploy, "create_spool", create)


def test_the_audit_directory_is_created_02750_with_its_group_before_the_installer_runs(
        tmp_path, monkeypatch):
    """ADR-0025: root:<spool_group> 02750, made before install.sh runs, and
    through `create_spool()` -- never `install -d`, which follows a symlinked
    leaf. Mutation: drop the fchmod, or go back to `install -d -m 0755`, and
    the mode or the call list says so."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    _record_spool_creation(monkeypatch, calls)
    pass_prefix_checks(monkeypatch)

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    created = [i for i, c in enumerate(calls)
               if c[0] == "<create_spool>" and c[1] == args.spool_dir]
    assert len(created) == 1, calls
    assert not [c for c in calls if c[:2] == ["install", "-d"]
                and c[-1] == args.spool_dir], calls
    installer = next(i for i, c in enumerate(calls)
                     if any("install.sh" in a for a in c))
    assert created[0] < installer, calls

    info = os.lstat(args.spool_dir)
    assert stat.S_IMODE(info.st_mode) == 0o2750, oct(info.st_mode)
    assert info.st_gid == os.getgid()


def test_the_spool_mode_still_refuses_the_write_adr_0004_forbids(
        tmp_path, monkeypatch):
    """The mode changed; the invariant did not. An append needs `w`, and
    neither group nor other has it. What changed is who READS: the group
    has r-x, other has nothing (ADR-0025)."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    pass_prefix_checks(monkeypatch)

    args = _args(tmp_path)
    assert deploy.system_execute(args) == 0

    bits = stat.S_IMODE(os.lstat(args.spool_dir).st_mode)
    assert not bits & 0o022, "group/other must never gain w: %04o" % bits
    assert bits & 0o050 == 0o050, "the reader group keeps r-x: %04o" % bits
    assert not bits & 0o007, "other reads nothing: %04o" % bits
    assert bits & stat.S_ISGID, "files take the group from setgid: %04o" % bits


def test_an_audit_directory_owned_by_someone_else_refuses_before_any_command(
        tmp_path, monkeypatch):
    """The mode is ours to assert; the OWNER is not ours to take."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    os.makedirs(args.spool_dir, exist_ok=True)
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: [deploy.Unowned(root, deploy.UNOWNED_FOREIGN_UID,
                                            "owned by uid 1000")]
        if root == args.spool_dir else [])

    assert deploy.system_execute(args) == 6
    assert calls == [], calls


def test_an_untraversable_audit_ancestor_refuses_before_any_command(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    ancestor = os.path.dirname(args.spool_dir)
    monkeypatch.setattr(
        deploy, "untraversable_for_users",
        lambda prefix: [(ancestor, "mode 0700 has no o+x")] if prefix == args.spool_dir
        else [])

    assert deploy.system_execute(args) == 6
    assert calls == [], calls


def test_the_preview_names_the_audit_directory_it_will_create(
        tmp_path, capsys, monkeypatch):
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    args = _args(tmp_path)
    deploy.system_preview(args)
    out = capsys.readouterr().out
    assert "mkdir %s" % args.spool_dir in out, out
    assert "fchmod 02750" in out, out
    assert "fchown 0:%d" % os.getgid() in out, out
    assert "Not `install -d`" in out, out
    assert "install -d -m 0755 %s" % args.spool_dir not in out, out


def _spool_with(tmp_path, monkeypatch, name, mode):
    """A spool holding one file at `mode`, with the real ownership check
    driven at this user's uid rather than root's, scoped to the SPOOL."""
    real = deploy.unowned_by
    args = _args(tmp_path)
    spool = args.spool_dir
    os.makedirs(spool, exist_ok=True)
    victim = os.path.join(spool, name)
    with open(victim, "w") as fh:
        fh.write("{}\n")
    os.chmod(victim, mode)
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=None: real(root, uid=os.getuid())
        if os.path.abspath(root) == spool else [])
    return args, victim


def test_a_group_writable_state_file_is_repaired_not_refused(
        tmp_path, monkeypatch):
    """A spool carrying a default ACL suppresses the umask at file creation,
    so the reaper's state file can land group-writable and the install
    would stop over a mode it is about to assert."""
    args, victim = _spool_with(tmp_path, monkeypatch, "reaper-state.json", 0o664)

    assert [r[0] for r in deploy.spool_mode_repairs(args.spool_dir)
            if r[0] != args.spool_dir] == [victim]
    permissive = [b for b in deploy.audit_dir_blockers(args.spool_dir)
                  if b[0] == "permissive"]
    assert permissive == [], permissive


def test_the_exemption_does_not_cover_a_file_this_install_never_wrote(
        tmp_path, monkeypatch):
    """The narrowness IS the property."""
    args, victim = _spool_with(tmp_path, monkeypatch, "someone-elses.json", 0o664)

    assert [r for r in deploy.spool_mode_repairs(args.spool_dir)
            if r[0] != args.spool_dir] == []
    permissive = [b for b in deploy.audit_dir_blockers(args.spool_dir)
                  if b[0] == "permissive"]
    assert [b[1] for b in permissive] == [victim]


def test_the_install_actually_tightens_what_it_declined_to_refuse(
        tmp_path, monkeypatch):
    args, victim = _spool_with(tmp_path, monkeypatch, "reaper-state.json", 0o664)
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    assert deploy.system_execute(args) == 0
    # Through an fd, so there is no command to find in `calls`: the proof
    # is the file itself.
    info = os.lstat(victim)
    assert stat.S_IMODE(info.st_mode) == 0o640, oct(info.st_mode)
    assert info.st_gid == os.getgid()
    assert not [c for c in calls if c[:1] == ["chmod"] and victim in c], calls


def test_the_preview_advertises_the_chmod_the_install_will_run(
        tmp_path, monkeypatch):
    args, victim = _spool_with(tmp_path, monkeypatch, "reaper-state.json", 0o664)
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    # The preview validates the root-write paths now, exactly as the install
    # does, and a tmp tree's ancestors are this user's -- the same stub the
    # install-side sibling of this test has carried all along.
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "run", recording_run([]))

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = deploy.system_preview(args)
    monkeypatch.undo()
    assert rc == 0, out.getvalue()
    assert "%s: gid %d mode 0664 -> gid %d mode 0640" % (
        victim, os.getgid(), os.getgid()) in out.getvalue(), out.getvalue()


def test_an_orphaned_state_temp_file_is_repaired_not_refused(
        tmp_path, monkeypatch):
    args, victim = _spool_with(
        tmp_path, monkeypatch, "reaper-state.json.tmp", 0o664)

    assert [r[0] for r in deploy.spool_mode_repairs(args.spool_dir)
            if r[0] != args.spool_dir] == [victim]
    permissive = [b for b in deploy.audit_dir_blockers(args.spool_dir)
                  if b[0] == "permissive"]
    assert permissive == [], permissive


def test_layer_1s_trail_is_installer_owned_under_its_compiled_name(
        tmp_path, monkeypatch):
    """The audit filename is a literal from `[install].audit_filename`, and
    the installer-owned list uses that literal rather than a spelling of
    its own -- so a site that renames the trail still gets it tightened."""
    args, victim = _spool_with(
        tmp_path, monkeypatch, deploy.DEFAULT_AUDIT_FILENAME, 0o664)
    assert [r[0] for r in deploy.spool_mode_repairs(args.spool_dir)
            if r[0] != args.spool_dir] == [victim]
    assert deploy.audit_path(args.spool_dir) == victim


def test_the_preview_refuses_where_the_install_would(tmp_path, monkeypatch):
    """Both callers go through `audit_dir_blockers()`, so the preview
    refuses on exactly the condition the install refuses on."""
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users",
                        lambda prefix: [])
    args = _args(tmp_path)
    os.makedirs(args.spool_dir, exist_ok=True)
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: [deploy.Unowned(root, deploy.UNOWNED_FOREIGN_UID,
                                            "owned by uid 1000")]
        if root == args.spool_dir else [])

    assert deploy.audit_dir_blockers(args.spool_dir), "fixture must be blocked"

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_execute(args) == 6
    assert calls == [], calls

    assert deploy.system_preview(args) == 6


def test_the_preview_and_the_install_agree_on_a_degenerate_spool(
        tmp_path, monkeypatch):
    """A spool of `/` is not a usable spool, and both callers say so. Moved
    through the CONSTANT: setting `args.spool_dir` directly would make
    not_a_default() refuse first and prove nothing."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    pass_prefix_checks(monkeypatch)

    monkeypatch.setattr(deploy, "DEFAULT_SPOOL_DIR", "/")
    args = _args(tmp_path)
    assert args.spool_dir == "/"

    assert [cls for cls, _p, _r in deploy.audit_dir_blockers("/")] == ["degenerate"]
    assert deploy.system_execute(args) == 6
    assert calls == [], calls
    assert deploy.system_preview(args) == 6


def test_a_relative_spool_is_a_degenerate_blocker(tmp_path):
    bad = deploy.audit_dir_blockers("")
    assert [cls for cls, _p, _r in bad] == ["degenerate"], bad


def test_an_ancestor_blocker_does_not_advertise_a_chmod_of_the_spool(tmp_path):
    """For an ancestor blocker there is no single correct command, so none
    is printed: a plausible one runs clean and fixes nothing."""
    spool = str(tmp_path / "var-log")
    buf = io.StringIO()
    deploy.write_audit_dir_refusal(
        spool,
        [("traversal", str(tmp_path), "mode 0700 has no o+x, so no ordinary "
                                      "user can traverse it")],
        out=buf)
    text = buf.getvalue()
    assert "%s: mode 0700" % tmp_path in text, text
    assert "fix with:" not in text, text
    assert "chmod 0755 %s" % spool not in text, text


def test_an_ownership_blocker_names_the_offending_path_not_the_directory(
        tmp_path):
    spool = str(tmp_path / "var-log")
    child = os.path.join(spool, "reaper-audit.jsonl")
    buf = io.StringIO()
    deploy.write_audit_dir_refusal(
        spool, [("ownership", child, "owned by uid 1000")], out=buf)
    text = buf.getvalue()
    assert "chown root:root %s" % child in text, text


# One exemplar per class: the CODE `unowned_by()` would emit, and the prose
# it would carry. Exemplars rather than real filesystem state: the suite runs
# NON-ROOT, so every path under tmp_path reports a foreign uid first and the
# other branches are unreachable from a real directory. The last row is a
# code from nowhere -- what a future edit that forgets the class map looks
# like from here.
_UNOWNED_EXEMPLARS = (
    ("ownership", deploy.UNOWNED_FOREIGN_UID, "owned by uid 1000",
     "chown root:root"),
    ("permissive", deploy.UNOWNED_PERMISSIVE_MODE,
     "mode 0777 is writable beyond its owner", "chmod go-w"),
    ("setuid", deploy.UNOWNED_SETUID, "mode 2755 is setuid or setgid",
     "chmod a-s"),
    ("symlink", deploy.UNOWNED_ESCAPING_SYMLINK,
     "symlink escapes the prefix -> /elsewhere", None),
    ("unreadable", deploy.UNOWNED_UNINSPECTABLE,
     "could not be inspected: Permission denied", None),
    ("unclassified", "a_code_from_a_later_edit",
     "a reason no future edit told this message about", None),
)


def test_every_code_unowned_by_can_emit_has_a_blocker_class():
    """The codes are the contract between `unowned_by()` and the refusal.
    One with no class would print the `unclassified` head -- true, but
    uninformative -- for a condition this file knows the name of."""
    for code in deploy.UNOWNED_CODES:
        cls = deploy._classify_unowned(code)
        assert cls != "unclassified", code
        assert cls in deploy._REFUSAL_HEADS, (code, cls)
    assert set(deploy.UNOWNED_CODES) == set(deploy._UNOWNED_CLASSES), \
        "UNOWNED_CODES and _UNOWNED_CLASSES have drifted apart"


def test_unowned_refuses_to_construct_an_offender_with_no_class():
    """Construction-time, not call-site: a code added to `unowned_by()` and
    forgotten in the class map cannot reach a caller silently."""
    with pytest.raises(AssertionError):
        deploy._unowned("/p", "a_code_from_a_later_edit", "why")


@pytest.mark.parametrize("cls,code,reason,_remedy", _UNOWNED_EXEMPLARS)
def test_a_reworded_reason_cannot_change_the_blocker_class(
        cls, code, reason, _remedy, tmp_path, monkeypatch):
    """The point of the code. `audit_dir_blockers()` must reach the same
    class whatever the prose says -- including prose that reads like some
    OTHER class's, which is exactly what a careless reword produces."""
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    args = _args(tmp_path)
    os.makedirs(args.spool_dir, exist_ok=True)
    offender = os.path.join(args.spool_dir, "offending-entry")
    misleading = "owned by uid 1000, is setuid or setgid, could not be "\
                 "inspected, symlink escapes the prefix"
    for prose in (reason, misleading):
        monkeypatch.setattr(
            deploy, "unowned_by",
            lambda root, uid=0, _p=prose: [deploy.Unowned(offender, code, _p)]
            if root == args.spool_dir else [])
        assert deploy.audit_dir_blockers(args.spool_dir) == [
            (cls, offender, prose)], (code, prose)


@pytest.mark.parametrize("cls,code,reason,remedy", _UNOWNED_EXEMPLARS)
def test_every_ownership_reason_gets_a_true_head_and_a_remedy_that_works(
        cls, code, reason, remedy, tmp_path):
    assert deploy._classify_unowned(code) == cls, code

    spool = str(tmp_path / "var-log")
    offender = os.path.join(spool, "offending-entry")
    buf = io.StringIO()
    deploy.write_audit_dir_refusal(spool, [(cls, offender, reason)], out=buf)
    text = buf.getvalue()

    assert reason in text, text
    assert offender in text, text
    if remedy is None:
        assert "fix with:" not in text, (
            "%s has no single correct command; printing one anyway is the "
            "defect: %s" % (cls, text))
    else:
        assert "fix with: %s %s" % (remedy, offender) in text, text
        assert "fix with: %s %s\n" % (remedy, spool) not in text, text


@pytest.mark.parametrize("cls,code,reason,_remedy", _UNOWNED_EXEMPLARS)
def test_both_callers_refuse_on_every_ownership_reason(
        cls, code, reason, _remedy, tmp_path, monkeypatch):
    """The class x caller matrix: every class against both callers."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    os.makedirs(args.spool_dir, exist_ok=True)
    offender = os.path.join(args.spool_dir, "offending-entry")
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: [deploy.Unowned(offender, code, reason)]
        if root == args.spool_dir else [])

    assert deploy.audit_dir_blockers(args.spool_dir) == [(cls, offender, reason)]
    assert deploy.system_execute(args) == 6, cls
    assert calls == [], calls
    assert deploy.system_preview(args) == 6, cls


def test_the_traversal_class_refuses_in_the_preview_too(
        tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    args = _args(tmp_path)
    monkeypatch.setattr(
        deploy, "untraversable_for_users",
        lambda prefix: [(os.path.dirname(args.spool_dir), "mode 0700 has no o+x")]
        if prefix == args.spool_dir else [])

    assert deploy.system_execute(args) == 6
    assert calls == [], calls
    assert deploy.system_preview(args) == 6


def test_the_refusal_head_says_which_problem_it_is(tmp_path):
    spool = str(tmp_path / "var-log")
    heads = {}
    for cls in ("traversal", "ownership", "permissive", "setuid",
                "symlink", "unreadable", "unclassified"):
        buf = io.StringIO()
        deploy.write_audit_dir_refusal(spool, [(cls, "/p", "r")], out=buf)
        heads[cls] = buf.getvalue()

    assert "cannot be traversed" in heads["traversal"]
    assert "not root's alone" in heads["ownership"]
    assert "writable beyond root" in heads["permissive"]
    assert "setuid or setgid" in heads["setuid"]
    assert "through a symlink" in heads["symlink"]
    assert "could not be inspected" in heads["unreadable"]
    assert "does not recognise" in heads["unclassified"]
    assert len(set(heads.values())) == len(heads), heads


def test_a_0750_spool_is_not_a_blocker_because_it_is_what_gets_fixed(tmp_path):
    """The REAL `untraversable_for_users`: the spool's own mode is what
    `create_spool()` asserts (02750, ADR-0025), so refusing on it is refusing
    to run the fix."""
    spool = tmp_path / "var-log"
    spool.mkdir()
    spool.chmod(0o750)
    tmp_path.chmod(0o755)

    offenders = [path for cls, path, _r in deploy.audit_dir_blockers(str(spool))
                 if cls == "traversal"]
    assert str(spool) not in offenders, offenders


def test_an_untraversable_ancestor_is_still_a_blocker(tmp_path):
    spool = tmp_path / "var-log"
    spool.mkdir()
    spool.chmod(0o755)
    tmp_path.chmod(0o750)          # the ANCESTOR, not the leaf
    try:
        classes = [cls for cls, _p, _r in deploy.audit_dir_blockers(str(spool))]
        assert "traversal" in classes, deploy.audit_dir_blockers(str(spool))
    finally:
        tmp_path.chmod(0o755)


_SPOOL_SPELLINGS = (
    ("canonical", lambda d: d),
    ("doubled leading slash", lambda d: "/" + d),
    ("dot component", lambda d: d.replace("/var-log", "/./var-log")),
    ("dotdot round trip", lambda d: d + "/../" + os.path.basename(d)),
)


@pytest.mark.parametrize("label,spell", _SPOOL_SPELLINGS)
def test_the_leaf_filter_survives_every_spelling_of_the_spool(
        label, spell, tmp_path):
    """The filter compares a canonical path against the caller's spelling;
    a raw comparison would let the leaf survive and bring the regression
    back silently. None of these spellings is reachable while ADR-0005
    holds, which is exactly why they are pinned."""
    spool = tmp_path / "var-log"
    spool.mkdir()
    spool.chmod(0o750)
    offenders = [path for cls, path, _r in deploy.audit_dir_blockers(spell(str(spool)))
                 if cls == "traversal"]
    for form in (str(spool), deploy.canonical_prefix(str(spool))):
        assert form not in offenders, (label, offenders)


def test_the_leaf_filter_is_path_identity_not_a_substring_test(tmp_path):
    outer = tmp_path / "var-log"
    outer.mkdir()
    spool = outer / "inner" / "var-log"
    spool.mkdir(parents=True)
    spool.chmod(0o755)
    outer.chmod(0o750)                      # the ANCESTOR, sharing a basename
    try:
        offenders = [path for cls, path, _r in deploy.audit_dir_blockers(str(spool))
                     if cls == "traversal"]
        assert str(outer) in offenders, offenders
        assert str(spool) not in offenders, offenders
    finally:
        outer.chmod(0o755)


def test_the_install_reaches_the_real_traversability_check(
        traversable_root, monkeypatch):
    """`system_execute()` end to end with the REAL check, against a tree
    whose ancestors really are traversable, with the spool at 0750. It must
    proceed: that state is the one the install exists to correct."""
    pass_ownership_checks(monkeypatch)
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    spool = os.path.join(traversable_root, "var-log")
    os.makedirs(spool)
    os.chmod(spool, 0o750)
    _move_constants(monkeypatch, traversable_root, spool=spool)

    assert deploy.audit_dir_blockers(deploy.DEFAULT_SPOOL_DIR) == []
    assert deploy.system_execute(_args(traversable_root)) == 0
    assert calls, "a proceeding install runs commands"


def test_the_install_still_refuses_a_really_untraversable_ancestor(
        traversable_root, monkeypatch):
    pass_ownership_checks(monkeypatch)
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    middle = os.path.join(traversable_root, "closed")
    spool = os.path.join(middle, "var-log")
    os.makedirs(spool)
    os.chmod(spool, 0o755)
    os.chmod(middle, 0o750)         # the ANCESTOR, not the leaf
    try:
        _move_constants(monkeypatch, traversable_root, spool=spool)
        assert deploy.system_execute(_args(traversable_root)) == 6
        assert calls == [], calls
    finally:
        os.chmod(middle, 0o755)


def test_the_preview_reaches_the_real_traversability_check(
        traversable_root, monkeypatch):
    pass_ownership_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    spool = os.path.join(traversable_root, "var-log")
    os.makedirs(spool)
    os.chmod(spool, 0o750)
    _move_constants(monkeypatch, traversable_root, spool=spool)
    assert deploy.system_preview(_args(traversable_root)) == 0


def test_the_preview_still_refuses_a_really_untraversable_ancestor(
        traversable_root, monkeypatch):
    pass_ownership_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    middle = os.path.join(traversable_root, "closed")
    spool = os.path.join(middle, "var-log")
    os.makedirs(spool)
    os.chmod(spool, 0o755)
    os.chmod(middle, 0o750)         # the ANCESTOR, not the leaf
    try:
        _move_constants(monkeypatch, traversable_root, spool=spool)
        assert deploy.system_preview(_args(traversable_root)) == 6
    finally:
        os.chmod(middle, 0o755)


# --------------------------------------------------------------------------
# the preflight both callers run
#
# Three defects of one shape: a check that lived only in `system_execute()`,
# so a node in that state previewed clean and the install refused -- after
# the timer had already been disabled and stopped. The fix is one function
# both callers use, so the tests below are all of the form "and the OTHER
# caller says the same thing".
# --------------------------------------------------------------------------

def _spool_as_regular_file(path):
    with open(path, "w") as fh:
        fh.write("not a directory\n")


def _spool_as_symlink_to_file(path):
    target = path + ".target"
    with open(target, "w") as fh:
        fh.write("not a directory\n")
    os.symlink(target, path)


def _spool_as_dangling_symlink(path):
    os.symlink(path + ".never-created", path)


def _spool_as_fifo(path):
    os.mkfifo(path)


# label, planter, the fragment the refusal has to name. A dangling symlink is
# on this list deliberately, though not for the reason first written here:
# measured, GNU coreutils 8.32, `install -d` on a dangling link does NOT
# create the target -- it exits 1 with "cannot change permissions of ..." and
# creates nothing. That is worse, not better: unblocked, the install would
# fail there, after the timer had already been disabled and stopped.
_NON_DIRECTORY_SPOOLS = (
    ("a regular file", _spool_as_regular_file, "is a regular file"),
    ("a symlink to a file", _spool_as_symlink_to_file, "which is not a directory"),
    ("a dangling symlink", _spool_as_dangling_symlink, "dangling symlink"),
    ("a fifo", _spool_as_fifo, "is a fifo"),
)


@pytest.mark.parametrize("label,plant,fragment", _NON_DIRECTORY_SPOOLS)
def test_a_spool_that_exists_and_is_not_a_directory_is_a_blocker(
        label, plant, fragment, tmp_path):
    """Both arms of `audit_dir_blockers()` looked straight past this: the
    traversal arm drops the leaf on purpose and the ownership arm is gated
    on `isdir`. So the spool's own TYPE is checked in its own right."""
    spool = str(tmp_path / "var-log")
    plant(spool)

    bad = deploy.audit_dir_blockers(spool)
    named = [(cls, path) for cls, path, _r in bad if cls == "not-a-directory"]
    assert named == [("not-a-directory", deploy.canonical_prefix(spool))], (label, bad)
    reason = [r for cls, _p, r in bad if cls == "not-a-directory"][0]
    assert fragment in reason, (label, reason)


@pytest.mark.parametrize("label,plant,_fragment", _NON_DIRECTORY_SPOOLS)
def test_both_callers_refuse_a_spool_that_is_not_a_directory(
        label, plant, _fragment, tmp_path, monkeypatch):
    """The preview used to print the approved command over this, and
    `install.sh`'s `assert_audit_dir` then failed on `install -d` -- after
    the timer had been stopped."""
    pass_prefix_checks(monkeypatch)
    args = _args(tmp_path)
    plant(args.spool_dir)

    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert deploy.system_preview(args) == 6, label

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_execute(args) == 6, label
    assert calls == [], calls


def test_a_non_directory_spool_is_offered_no_fabricated_remedy(tmp_path):
    """The only command that would "fix" this is an `rm` aimed by the
    installer at a path somebody put a file at on purpose."""
    spool = str(tmp_path / "var-log")
    buf = io.StringIO()
    deploy.write_audit_dir_refusal(
        spool, [("not-a-directory", spool, "is a regular file, not a directory")],
        out=buf)
    text = buf.getvalue()
    assert "is a regular file, not a directory" in text, text
    assert "fix with:" not in text, text
    assert "rm " not in text, text


def test_a_spool_that_is_a_symlink_to_a_directory_stays_the_symlink_class(
        tmp_path, monkeypatch):
    """Not double-reported: `unowned_by()` already names this one, with its
    own head and the reason that chown and chmod do not reach through it."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    spool = str(tmp_path / "var-log")
    os.symlink(str(real), spool)
    original = deploy.unowned_by
    monkeypatch.setattr(deploy, "unowned_by",
                        lambda root, uid=0: original(root, uid=os.getuid()))

    classes = [cls for cls, _p, _r in deploy.audit_dir_blockers(spool)]
    assert "symlink" in classes, classes
    assert "not-a-directory" not in classes, classes


def test_the_preview_refuses_a_prefix_the_users_cannot_reach(
        traversable_root, monkeypatch):
    """`untraversable_for_users(prefix)` ran only in the install, so a
    prefix under an ancestor with no `o+x` previewed clean and every
    monitored account would have got an unreachable directory on PATH."""
    pass_ownership_checks(monkeypatch)
    closed = os.path.join(traversable_root, "closed")
    prefix = os.path.join(closed, "prefix")
    os.makedirs(prefix)
    os.chmod(closed, 0o750)                 # the ANCESTOR, not the prefix
    try:
        _move_constants(monkeypatch, traversable_root)
        monkeypatch.setattr(deploy, "DEFAULT_PREFIX", prefix)
        monkeypatch.setattr(deploy, "run", recording_run([]))
        assert deploy.system_preview(_args(traversable_root)) == 6

        monkeypatch.setattr(deploy, "_is_root", lambda: True)
        calls = []
        monkeypatch.setattr(deploy, "run", recording_run(calls))
        assert deploy.system_execute(_args(traversable_root)) == 6
        assert calls == [], calls
    finally:
        os.chmod(closed, 0o755)


def test_the_preview_refuses_a_symlinked_hook_file(tmp_path, monkeypatch):
    """`validate_root_write_paths()` ran only in the install and the
    uninstall. A system rc under a dotfile manager is a symlink in the
    ORDINARY case, so this previewed clean on a perfectly normal node."""
    pass_prefix_checks(monkeypatch)
    args = _args(tmp_path)
    target = tmp_path / "real-bashrc"
    target.write_text("# a hook file under a dotfile manager\n")
    os.symlink(str(target), args.bashrc_file)

    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert deploy.system_preview(args) == 6

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_execute(args) == 6
    assert calls == [], calls


def test_the_preview_refuses_a_populated_prefix_that_is_not_its_own(
        tmp_path, monkeypatch):
    """The last of the install's pre-`systemctl` checks, reachable from the
    preview for the same reason as the other three."""
    pass_prefix_checks(monkeypatch)
    args = _args(tmp_path)
    os.makedirs(args.prefix)
    open(os.path.join(args.prefix, "somebody-elses-file"), "w").close()

    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert deploy.system_preview(args) == 6

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_execute(args) == 6
    assert calls == [], calls


def _stage_under(monkeypatch, parent):
    """Point STAGING_PARENT somewhere this test controls, past the autouse
    fixture. Returns the path, as a string, the way the compiled literal is."""
    monkeypatch.setattr(deploy, "STAGING_PARENT", str(parent))
    return str(parent)


def _only_the_staging_parent_is_untrusted(monkeypatch):
    """Make the chain check answer by SUBJECT rather than for everything.

    `pass_prefix_checks()` stubs `untrusted_prefix_chain` globally, which is
    what lets the six root-write locations pass under tmp. So this REPLACES
    that stub rather than adding a second one: same signature, answering for
    the staging parent alone and clean for every other path -- the shape the
    prefix-traversal parity case already uses.
    """
    def by_subject(prefix, trusted_uids=(0,), trusted_gids=()):
        if deploy.canonical_prefix(prefix) == deploy.canonical_prefix(
                deploy.STAGING_PARENT):
            return [(deploy.STAGING_PARENT,
                     "is group-writable, so the snapshot's parent is not "
                     "root's alone")]
        return []
    monkeypatch.setattr(deploy, "untrusted_prefix_chain", by_subject)


def test_the_preview_refuses_an_untrusted_staging_parent(tmp_path, monkeypatch):
    """The refusal `stage_payload()` makes between preflight() returning and
    the first `systemctl`. The preview never reached it, so a node whose
    staging parent anyone can write previewed clean and then refused with
    the install already under way."""
    pass_prefix_checks(monkeypatch)
    _only_the_staging_parent_is_untrusted(monkeypatch)
    args = _args(tmp_path)

    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert deploy.system_preview(args) == 6

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_execute(args) == 6
    assert calls == [], calls


def test_the_preview_refuses_a_staging_parent_that_is_not_there(
        tmp_path, monkeypatch):
    """`tempfile.mkdtemp(dir=...)` raises ENOENT uncaught, after every check
    has passed. ENOENT is an ANSWER here, so it is a blocker rather than an
    unknown -- and one the preview can give before anyone runs anything."""
    pass_prefix_checks(monkeypatch)
    missing = _stage_under(monkeypatch, tmp_path / "run-that-is-not-there")
    assert not os.path.exists(missing)
    args = _args(tmp_path)

    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert deploy.system_preview(args) == 6

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    assert deploy.system_execute(args) == 6
    assert calls == [], calls


def test_a_dry_run_still_plans_over_a_staging_parent_it_never_uses(
        tmp_path, monkeypatch, capsys):
    """The one check a dry run does not make, because `stage_payload()` does
    not make it either: it returns before the check, having created nothing.
    A dry run that refused here would refuse to PLAN an install over a
    condition it never touches."""
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    pass_prefix_checks(monkeypatch)
    _only_the_staging_parent_is_untrusted(monkeypatch)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))

    assert deploy.system_execute(_args(tmp_path, dry_run=True)) == 0
    assert calls, "a dry run prints a plan; it does not refuse"
    assert "refusing to stage" not in capsys.readouterr().err


_PARITY_CASES = ("a spool that is not a directory",
                 "a prefix the users cannot reach",
                 "a hook file that is a symlink",
                 "a staging parent that is not trusted")


@pytest.mark.parametrize("case", _PARITY_CASES)
def test_the_preview_and_the_install_refuse_with_the_same_head_line(
        case, tmp_path, monkeypatch, capsys):
    """Not "both return 6" -- the same SENTENCE, because both callers reach
    the same writer through the same function. Two writers that agree today
    are what this whole change exists to stop relying on."""
    pass_prefix_checks(monkeypatch)
    args = _args(tmp_path)
    if case == "a spool that is not a directory":
        _spool_as_regular_file(args.spool_dir)
    elif case == "a prefix the users cannot reach":
        monkeypatch.setattr(
            deploy, "untraversable_for_users",
            lambda prefix: [(os.path.dirname(prefix), "mode 0700 has no o+x, "
                             "so no ordinary user can traverse it")]
            if prefix == deploy.canonical_prefix(args.prefix) else [])
    elif case == "a staging parent that is not trusted":
        _only_the_staging_parent_is_untrusted(monkeypatch)
    else:
        target = tmp_path / "real-bashrc"
        target.write_text("# under a dotfile manager\n")
        os.symlink(str(target), args.bashrc_file)

    monkeypatch.setattr(deploy, "run", recording_run([]))
    assert deploy.system_preview(args) == 6, case
    previewed = capsys.readouterr().err.splitlines()

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    assert deploy.system_execute(args) == 6, case
    executed = capsys.readouterr().err.splitlines()

    assert previewed, case
    assert previewed[0] == executed[0], (case, previewed[0], executed[0])
    assert "deploy.py: refusing" in previewed[0], previewed[0]


# --------------------------------------------------------------------------
# the third state: what an unprivileged preview could not look at
# --------------------------------------------------------------------------

def _blinded(tmp_path, monkeypatch):
    """A prefix under an ancestor this user may not traverse OR stat into,
    which is what a preview run by someone other than root can meet on a
    real node. Returns the blind ancestor, for the caller to restore."""
    blind = tmp_path / "blind"
    (blind / "prefix").mkdir(parents=True)
    monkeypatch.setattr(deploy, "DEFAULT_PREFIX", str(blind / "prefix"))
    blind.chmod(0o000)
    return blind


def test_unstattable_as_me_separates_not_allowed_from_not_there(tmp_path):
    """ENOENT is an ANSWER -- every check models a path that does not exist
    yet -- and only EACCES/EPERM mean the answer is unavailable to this uid."""
    assert deploy.unstattable_as_me(str(tmp_path / "never-created")) is None
    assert deploy.unstattable_as_me(str(tmp_path)) is None

    if os.geteuid() == 0:
        pytest.skip("root is not subject to the bits this reads")
    blind = tmp_path / "blind"
    (blind / "prefix").mkdir(parents=True)
    blind.chmod(0o000)
    try:
        found = deploy.unstattable_as_me(str(blind / "prefix"))
        assert found is not None
        assert found[0] == str(blind / "prefix"), found
    finally:
        blind.chmod(0o755)


def test_the_preview_says_it_could_not_check_rather_than_clean(
        tmp_path, monkeypatch, capsys):
    """Neither of the two easy answers. Reporting it clean is the defect
    this whole change is about; reporting it blocked would be a refusal
    manufactured out of the reader's own uid."""
    if os.geteuid() == 0:
        pytest.skip("root can stat anything, which is the point")
    pass_prefix_checks(monkeypatch)
    blind = _blinded(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(deploy, "run", recording_run([]))
        args = _args(tmp_path)
        assert deploy.system_preview(args) == 0
        out = capsys.readouterr().out
    finally:
        blind.chmod(0o755)

    assert "NOT CHECKED" in out, out
    assert "could not be checked as this user" in out, out
    assert args.prefix in out, out
    # Still advertised: "I could not look" is not "I found something".
    assert _previewed_command(out)


def test_an_unprivileged_preview_invents_no_blocker_from_its_own_blindness(
        tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can stat anything, which is the point")
    pass_prefix_checks(monkeypatch)
    blind = _blinded(tmp_path, monkeypatch)
    try:
        rc, checks = deploy.preflight(_args(tmp_path), privileged=False)
    finally:
        blind.chmod(0o755)

    assert rc == 0
    states = dict((check.name, check.state) for check in checks)
    assert deploy.CHECK_BLOCKED not in states.values(), checks
    assert states["prefix_reach"] == deploy.CHECK_UNKNOWN, checks
    assert states["prefix_contents"] == deploy.CHECK_UNKNOWN, checks
    assert states["spool"] == deploy.CHECK_OK, checks


def test_a_check_root_could_not_make_is_a_refusal_not_a_pass(
        tmp_path, monkeypatch, capsys):
    """`unknown` is an answer about the READER's privileges, and root has
    none of that excuse. Unreachable as real root, which is why it is
    asserted rather than assumed away."""
    if os.geteuid() == 0:
        pytest.skip("root can stat anything, which is the point")
    pass_prefix_checks(monkeypatch)
    blind = _blinded(tmp_path, monkeypatch)
    try:
        rc, checks = deploy.preflight(_args(tmp_path), privileged=True)
    finally:
        blind.chmod(0o755)

    assert rc == 6
    assert any(check.state == deploy.CHECK_UNKNOWN for check in checks), checks
    assert "could not be made even as root" in capsys.readouterr().err


def test_the_offender_listing_is_capped_at_ten_distinct_lines(capsys):
    """Twelve distinct offenders and three duplicates: ten lines, sorted,
    duplicates collapsed first. The cap that keeps a refusal readable is now
    the helper's rather than each caller's, so it is pinned here once."""
    offenders = [("/p/%02d" % i, "code", "why %d" % i) for i in range(12)]
    deploy._write_offenders(offenders + offenders[:3])
    lines = capsys.readouterr().err.splitlines()
    assert lines == ["  /p/%02d: why %d" % (i, i) for i in range(10)], lines


# --------------------------------------------------------------------------
# --verify: the install against the record it was built from
# --------------------------------------------------------------------------

def _simulate_install(payload, prefix):
    """Copy exactly what an install copies, reading the table rather than
    restating it: a payload entry nobody added to PAYLOAD_SOURCES must show up
    as a test failure, not as a second list that quietly agrees with itself."""
    os.makedirs(prefix, exist_ok=True)
    for rel, is_dir, _mode in deploy.PAYLOAD_SOURCES:
        src, dst = os.path.join(payload, rel), os.path.join(prefix, rel)
        if is_dir:
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    with open(os.path.join(prefix, deploy.PAYLOAD_MARKER), "w") as handle:
        handle.write(walk_blocker.__version__ + "\n")
    return prefix


@pytest.fixture
def installed(built_payload, tmp_path):
    """A prefix holding what an install would have put there. `built_payload`
    is the fixture this module already defines for the build's own product."""
    return _simulate_install(str(built_payload), str(tmp_path / "installed"))


def _verify(prefix):
    class _Args(object):
        pass
    args = _Args()
    args.prefix = prefix
    out = io.StringIO()
    return deploy.system_verify(args, out=out), out.getvalue()


def test_a_clean_install_verifies(installed):
    code, out = _verify(installed)
    assert code == deploy.VERIFY_OK, out
    assert "0 finding(s)" in out


def test_an_edited_installed_file_differs(installed):
    """The oracle for the whole mode."""
    with open(os.path.join(installed, "shim", "guard.sh"), "a") as handle:
        handle.write("\n# edited in place\n")
    code, out = _verify(installed)
    assert code == deploy.VERIFY_DRIFT, out
    assert "differs: shim/guard.sh" in out


def test_a_removed_installed_file_is_missing(installed):
    os.remove(os.path.join(installed, "LICENSE"))
    code, out = _verify(installed)
    assert code == deploy.VERIFY_DRIFT, out
    assert "missing: LICENSE" in out


def test_an_added_file_under_an_installed_directory_is_extra(installed):
    with open(os.path.join(installed, "docs", "stray.md"), "w") as handle:
        handle.write("not from the payload\n")
    code, out = _verify(installed)
    assert code == deploy.VERIFY_DRIFT, out
    assert "extra: docs/stray.md" in out


def test_a_file_at_the_prefix_root_that_the_install_did_not_create_is_not_drift(installed):
    """The prefix may already hold files this install did not create, and the
    reconcile builds `bin/` there on every poll. Walking the root would report
    a neighbour's file, and the symlink farm, as drift."""
    os.makedirs(os.path.join(installed, "bin"), exist_ok=True)
    with open(os.path.join(installed, "bin", "find"), "w") as handle:
        handle.write("#!/bin/sh\n")
    with open(os.path.join(installed, "unrelated"), "w") as handle:
        handle.write("somebody else's file\n")
    code, out = _verify(installed)
    assert code == deploy.VERIFY_OK, out


def test_the_installer_is_built_but_not_installed_and_is_not_reported_missing(installed):
    """`deploy.py` is hashed into the lock and never installed -- it is this
    program, run from the staged copy. A verifier that walks the lock naively
    reports it missing on every run, forever."""
    assert not os.path.exists(os.path.join(installed, "deploy.py"))
    code, out = _verify(installed)
    assert code == deploy.VERIFY_OK, out
    assert "deploy.py" not in out


def test_the_built_but_not_installed_set_is_exactly_the_installer(built_payload):
    """Pinned so that a future payload file nobody adds to PAYLOAD_SOURCES is a
    test failure rather than a silent hole in verification."""
    with open(os.path.join(str(built_payload), "site.lock.json")) as handle:
        lock = json.load(handle)
    _expected, not_installed = deploy.expected_from_lock(lock)
    assert not_installed == {"deploy.py"}


def test_verify_checks_every_installed_file(installed, built_payload):
    with open(os.path.join(str(built_payload), "site.lock.json")) as handle:
        lock = json.load(handle)
    code, out = _verify(installed)
    assert code == deploy.VERIFY_OK
    assert "%d file(s) checked" % (len(lock["files"]) - 1) in out


@pytest.mark.parametrize("broken", ["missing", "truncated", "a-list", "no-files-key"])
def test_a_record_that_cannot_be_read_is_cannot_verify_not_clean(installed, broken):
    """Exit 1 means drift. A missing record is not drift and must never read as
    clean either: there is nothing to compare against."""
    lock = os.path.join(installed, "site.lock.json")
    if broken == "missing":
        os.remove(lock)
    else:
        with open(lock, "w") as handle:
            handle.write({"truncated": "{", "a-list": "[]",
                          "no-files-key": '{"version": "0.1.0"}'}[broken])
    code, out = _verify(installed)
    assert code == deploy.VERIFY_UNKNOWN, out
    assert "differs:" not in out and "missing:" not in out


def test_a_prefix_that_does_not_exist_is_cannot_verify(tmp_path):
    code, out = _verify(str(tmp_path / "nothing-installed-here"))
    assert code == deploy.VERIFY_UNKNOWN, out


def test_the_marker_disagreeing_with_the_record_is_drift(installed):
    """The marker is written before anything else, so it is the one check that
    survives an install that stopped half way."""
    with open(os.path.join(installed, deploy.PAYLOAD_MARKER), "w") as handle:
        handle.write("9.9.9\n")
    code, out = _verify(installed)
    assert code == deploy.VERIFY_DRIFT, out
    assert deploy.PAYLOAD_MARKER in out


def test_a_record_that_disagrees_with_itself_is_drift(installed):
    """site_sha256 and the site.toml entry are the lock's one internal
    relation. A hand edit that updated one and forgot the other is caught."""
    path = os.path.join(installed, "site.lock.json")
    with open(path) as handle:
        lock = json.load(handle)
    lock["site_sha256"] = "0" * 64
    with open(path, "w") as handle:
        json.dump(lock, handle)
    code, out = _verify(installed)
    assert code == deploy.VERIFY_DRIFT, out
    assert "site_sha256 disagrees" in out


def test_verify_writes_nothing(installed):
    """It is a reporter. Not a repair, not a chmod, not a re-copy."""
    def snapshot():
        seen = {}
        for base, _dirs, names in os.walk(installed):
            for name in names:
                full = os.path.join(base, name)
                st = os.lstat(full)
                seen[full] = (st.st_size, st.st_mtime_ns, st.st_mode)
        return seen
    before = snapshot()
    _verify(installed)
    assert snapshot() == before


def test_verify_needs_no_privilege(installed, monkeypatch):
    """Every installed file is world-readable by decision (ADR-0012), and on a
    shared node the people whose PATH this changed should be able to check the
    guard that refuses their command."""
    monkeypatch.setattr(deploy, "_is_root", lambda: False)
    code, _out = _verify(installed)
    assert code == deploy.VERIFY_OK


def test_verify_refuses_a_dry_run():
    """Accepting it silently on a read-only mode teaches an operator that
    `--verify` alone writes something. It does not, so there is no safer
    spelling of it to reach for."""
    with pytest.raises(SystemExit) as caught:
        deploy.main(["--verify", "--dry-run"])
    assert caught.value.code == 2


def test_verify_is_a_mode_and_excludes_the_others():
    for argv in (["--verify", "--system"], ["--verify", "--uninstall"]):
        with pytest.raises(SystemExit) as caught:
            deploy.main(argv)
        assert caught.value.code == 2, argv


def test_verify_takes_no_path_arguments():
    """ADR-0005: this installer cannot be pointed somewhere else, and the new
    mode gains no exception."""
    for flag in PATH_FLAGS:
        with pytest.raises(SystemExit) as caught:
            deploy.main(["--verify", flag, "/tmp"])
        assert caught.value.code == 2, flag


def test_the_docstring_names_every_option_the_parser_accepts():
    """The usage block reads as a complete reference, and its first line is
    what argparse prints as the description, so an option missing from it is
    one a reader is told does not exist. `--verify` was added without it once.

    The offered set is read from `--help` rather than restated here: a list
    that repeats the parser agrees with itself and catches nothing."""
    out = io.StringIO()
    with pytest.raises(SystemExit):
        with contextlib.redirect_stdout(out):
            deploy.main(["--help"])
    offered = set(re.findall(r"--[a-z][a-z-]+", out.getvalue()))
    assert "--verify" in offered, "the parser stopped accepting --verify"

    # `--help` is argparse's own. `--dry-run` used to be exempt here because
    # it was undocumented; it is the inspection path now, so it is not.
    exempt = {"--help"}
    for flag in sorted(offered - exempt):
        assert flag in deploy.__doc__, "%s is accepted but undocumented" % flag


@pytest.mark.parametrize("escape", [
    "shim/../../../../etc/hostname",
    "../outside",
    "/etc/hostname",
    "docs/../../..",
])
def test_a_record_naming_a_path_outside_the_payload_is_refused(installed, escape):
    """The mode exists to read a record that may have been tampered with, so
    the record cannot be assumed well formed. A path like this passes a plain
    prefix test, leaves the prefix when joined, and would have an unprivileged
    caller hash and report a file that is no part of the payload.

    Refusing the whole record rather than skipping the entry is deliberate: if
    it names something outside the payload, no per-file verdict taken from it
    is worth printing."""
    path = os.path.join(installed, "site.lock.json")
    with open(path) as handle:
        lock = json.load(handle)
    lock["files"][escape] = "0" * 64
    with open(path, "w") as handle:
        json.dump(lock, handle)
    code, out = _verify(installed)
    assert code == deploy.VERIFY_UNKNOWN, out
    assert "differs:" not in out and "missing:" not in out
    assert "no part of the payload" in out


def test_a_record_whose_keys_are_not_strings_is_refused(installed):
    path = os.path.join(installed, "site.lock.json")
    with open(path, "w") as handle:
        handle.write('{"version": "0.1.0", "files": {"1": 2}}')
    with open(path) as handle:
        raw = json.load(handle)
    raw["files"] = {"ok": 1}
    with open(path, "w") as handle:
        json.dump(raw, handle)
    # a non-string VALUE is only ever unequal, which is drift, not a refusal;
    # a non-string KEY cannot survive JSON, so the guard is about types we can
    # actually receive. This pins that the refusal does not overreach.
    code, _out = _verify(installed)
    assert code in (deploy.VERIFY_DRIFT, deploy.VERIFY_UNKNOWN)


# --------------------------------------------------------------------------
# ADR-0025: the spool chain's one loosening, and the groups it rests on
# --------------------------------------------------------------------------
#
# The chain tests drive the REAL `untrusted_prefix_chain()` with the test
# user trusted as an owner (`trusted_uids=(0, me)`), because every ancestor
# of a tmp tree is the test user's. What is under test is the GROUP rule, and
# every one of these has a case the seam must reject: a test that passed only
# because the CI user owns everything would pass with the rule deleted.

ME = (0, os.getuid())


def _group_writable(path, mode=0o775):
    os.makedirs(path, exist_ok=True)
    os.chmod(path, mode)
    assert os.lstat(path).st_gid == os.getgid()
    return path


def test_a_group_writable_spool_ancestor_with_a_trusted_gid_passes(tmp_path):
    """The loosening, and its converse in the same test: the identical
    directory refuses the moment its gid is not trusted."""
    ancestor = _group_writable(str(tmp_path / "log"))
    spool = os.path.join(ancestor, "walk-blocker")
    os.mkdir(spool, 0o750)
    assert deploy.untrusted_prefix_chain(
        spool, trusted_uids=ME, trusted_gids=(os.getgid(),)) == []
    refused = deploy.untrusted_prefix_chain(spool, trusted_uids=ME)
    assert [p for p, _why in refused] == [ancestor], refused


def test_a_group_writable_ancestor_with_an_untrusted_gid_refuses_with_todays_message(
        tmp_path):
    ancestor = _group_writable(str(tmp_path / "log"))
    spool = os.path.join(ancestor, "walk-blocker")
    os.mkdir(spool, 0o750)
    refused = deploy.untrusted_prefix_chain(
        spool, trusted_uids=ME, trusted_gids=(os.getgid() + 1,))
    assert refused == [(ancestor, "mode 0775 is writable by group or other, "
                                  "so its entries can be replaced")], refused


def test_an_other_writable_ancestor_refuses_whatever_gid_is_trusted(tmp_path):
    """Other-write is never accepted on a group's strength. Mutation: test
    `st_mode & 0o020` instead of `== 0o020` and this passes the 0777."""
    ancestor = _group_writable(str(tmp_path / "log"), mode=0o777)
    spool = os.path.join(ancestor, "walk-blocker")
    os.mkdir(spool, 0o750)
    refused = deploy.untrusted_prefix_chain(
        spool, trusted_uids=ME, trusted_gids=(os.getgid(),))
    assert [p for p, _why in refused] == [ancestor], refused


def test_the_spool_itself_is_never_loosened_only_its_strict_ancestors(tmp_path):
    """A group-writable SPOOL refuses even with its gid trusted: the listed
    group may hold an ancestor, never the directory the trail is in."""
    spool = _group_writable(str(tmp_path / "walk-blocker"))
    refused = deploy.untrusted_prefix_chain(
        spool, trusted_uids=ME, trusted_gids=(os.getgid(),))
    assert [p for p, _why in refused] == [spool], refused


def _trusting_me(monkeypatch):
    real = deploy.untrusted_prefix_chain
    monkeypatch.setattr(
        deploy, "untrusted_prefix_chain",
        lambda path, trusted_uids=(0,), trusted_gids=(): real(
            path, trusted_uids=ME, trusted_gids=trusted_gids))


@pytest.mark.parametrize("attr", ["prefix", "unit_dir", "bashrc_file"])
def test_the_same_directory_refuses_on_every_chain_but_the_spools(
        tmp_path, monkeypatch, attr):
    """One group-writable directory, trusted gid and all: under the spool it
    passes, under the prefix, the unit directory or a hook file it refuses.
    Mutation: pass `spool_trusted_gids` from every PATH_KINDS entry in
    validate_root_write_paths() and the non-spool cases pass."""
    _trusting_me(monkeypatch)
    shared = _group_writable(str(tmp_path / "shared"))
    spool_args = _args(tmp_path, spool_dir=os.path.join(shared, "spool"))
    assert deploy.validate_root_write_paths(
        spool_args, attrs=("spool_dir",),
        spool_trusted_gids=(os.getgid(),)) == 0
    # One level down, so `shared` is a STRICT ancestor on every chain -- a
    # hook file directly in it would make `shared` the judged leaf, which is
    # refused for the leaf rule's reason rather than this test's.
    sub = os.path.join(shared, "sub")
    os.mkdir(sub, 0o755)
    below = os.path.join(sub, "file" if attr.endswith("_file") else "dir")
    other_args = _args(tmp_path, **{attr: below})
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    rc = deploy.validate_root_write_paths(
        other_args, attrs=(attr,), spool_trusted_gids=(os.getgid(),))
    monkeypatch.undo()
    assert rc == 6, err.getvalue()
    assert shared in err.getvalue(), err.getvalue()


def test_the_staging_chain_is_not_loosened_either(tmp_path, monkeypatch):
    """The staging parent is judged in preflight() and again where the
    snapshot is made, both with root alone, whatever the site lists."""
    _trusting_me(monkeypatch)
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    shared = _group_writable(str(tmp_path / "shared"))
    staging = os.path.join(shared, "run")
    os.makedirs(staging)
    monkeypatch.setattr(deploy, "STAGING_PARENT", staging)
    _stub_groups(monkeypatch, tmp_path, {"svc-log": _Group(os.getgid(), [])},
                 trusted=("svc-log",))
    err = io.StringIO()
    rc, checks = deploy.preflight(_args(tmp_path), privileged=True, out=err)
    assert rc == 6, err.getvalue()
    assert "refusing to stage the payload" in err.getvalue(), err.getvalue()


# --- the human-member check ------------------------------------------------

class _Group(object):
    def __init__(self, gid, members, name=None):
        self.gr_gid = gid
        self.gr_mem = list(members)
        self.gr_name = name


class _Account(object):
    def __init__(self, name, uid, gid):
        self.pw_name = name
        self.pw_uid = uid
        self.pw_gid = gid


LOGIN_DEFS_TEXT = "# fictional\nUID_MIN 1000\nUID_MAX 60000\nGID_MIN 1000\n"

# Fictional accounts. `nobody` is the one real-shaped name, above UID_MAX as
# it is on the distributions this runs on.
ACCOUNTS = {
    "svc-d": _Account("svc-d", 105, 110),
    "nobody": _Account("nobody", 65534, 65534),
    "user-a": _Account("user-a", 1500, 1500),
    "user-b": _Account("user-b", 1501, 110),
}


def _login_defs(tmp_path, text=LOGIN_DEFS_TEXT):
    path = tmp_path / "login.defs"
    path.write_text(text)
    return str(path)


def _resolve(tmp_path, groups, names, accounts=None, text=LOGIN_DEFS_TEXT):
    accounts = ACCOUNTS if accounts is None else accounts

    def getgrnam(name):
        return groups[name]

    def getpwnam(name):
        return accounts[name]

    return deploy.resolve_trusted_groups(
        names, login_defs=_login_defs(tmp_path, text), getgrnam=getgrnam,
        getpwnam=getpwnam, getpwall=lambda: list(accounts.values()))


def test_a_service_group_with_no_person_is_accepted(tmp_path):
    gids, refusals = _resolve(tmp_path, {"svc-log": _Group(110, ["svc-d"])},
                              ["svc-log"],
                              accounts={"svc-d": ACCOUNTS["svc-d"]})
    assert (gids, refusals) == ((110,), [])


def test_a_supplementary_human_member_refuses(tmp_path):
    gids, refusals = _resolve(tmp_path, {"svc-log": _Group(110, ["user-a"])},
                              ["svc-log"],
                              accounts={"user-a": ACCOUNTS["user-a"]})
    assert gids == ()
    assert [n for n, _r in refusals] == ["svc-log"]
    assert "member 'user-a' has uid 1500" in refusals[0][1], refusals


def test_a_primary_gid_human_member_refuses(tmp_path):
    """`gr_mem` does not list accounts whose PRIMARY group it is. Mutation:
    drop the getpwall() pass and this group is accepted."""
    gids, refusals = _resolve(tmp_path, {"svc-log": _Group(110, [])},
                              ["svc-log"])
    assert gids == ()
    assert any("'user-b' has it as its primary group" in r for _n, r in refusals), refusals


def test_an_unresolvable_member_name_refuses(tmp_path):
    gids, refusals = _resolve(tmp_path, {"svc-log": _Group(110, ["ghost"])},
                              ["svc-log"],
                              accounts={"svc-d": ACCOUNTS["svc-d"]})
    assert gids == ()
    assert "member 'ghost' does not resolve" in refusals[0][1], refusals


def test_nobody_above_uid_max_is_not_a_person(tmp_path):
    """Mutation: drop the UID_MAX bound in `human()` and `nobody` refuses
    the group."""
    gids, refusals = _resolve(tmp_path, {"svc-log": _Group(110, ["nobody"])},
                              ["svc-log"],
                              accounts={"nobody": ACCOUNTS["nobody"]})
    assert (gids, refusals) == ((110,), [])


@pytest.mark.parametrize("missing", ["UID_MIN", "UID_MAX", "GID_MIN"])
def test_a_missing_login_defs_bound_fails_closed(tmp_path, missing):
    """No default: a guess about the node's allocation policy is what the
    check replaces. Mutation: default UID_MIN or UID_MAX to 1000/60000 and
    the service group is accepted."""
    text = "".join(l + "\n" for l in LOGIN_DEFS_TEXT.splitlines()
                   if not l.startswith(missing))
    gids, refusals = _resolve(tmp_path, {"svc-log": _Group(110, [])},
                              ["svc-log"],
                              accounts={"svc-d": ACCOUNTS["svc-d"]}, text=text)
    assert gids == ()
    assert missing in refusals[0][1], refusals


def test_an_unparsable_last_word_fails_closed_and_octal_reads_like_strtol(tmp_path):
    text = LOGIN_DEFS_TEXT + "UID_MAX sixty\n"
    gids, refusals = _resolve(tmp_path, {"svc-log": _Group(110, [])},
                              ["svc-log"], accounts={}, text=text)
    assert gids == () and "UID_MAX" in refusals[0][1], refusals
    defs = deploy.read_login_defs(_login_defs(tmp_path, "UID_MIN 01750\nUID_MAX 0xEA60\n"))
    assert defs == {"UID_MIN": 1000, "UID_MAX": 60000}


def test_a_gid_at_or_above_gid_min_refuses(tmp_path):
    gids, refusals = _resolve(tmp_path, {"svc-log": _Group(1000, [])},
                              ["svc-log"], accounts={})
    assert gids == ()
    assert "at or above GID_MIN 1000" in refusals[0][1], refusals


def test_a_listed_group_that_does_not_resolve_refuses(tmp_path):
    gids, refusals = _resolve(tmp_path, {}, ["svc-log"], accounts={})
    assert gids == () and "does not resolve" in refusals[0][1], refusals


def test_no_listed_group_reads_nothing_at_all(tmp_path):
    assert deploy.resolve_trusted_groups(
        (), login_defs=str(tmp_path / "absent")) == ((), [])


def test_the_refusal_states_what_the_member_check_cannot_see():
    out = io.StringIO()
    deploy.write_trusted_groups_refusal([("svc-log", "a reason")], out=out)
    text = out.getvalue()
    assert "refusing trusted_groups" in text
    assert "does not enumerate" in text and "not seen" in text, text
    assert "GID_MIN narrows that gap" in text, text
    out = io.StringIO()
    deploy.write_spool_group_refusal("wbread", "it does not resolve", out=out)
    assert "does not enumerate" in out.getvalue(), out.getvalue()


# --- the dry run makes the same check --------------------------------------

def _stub_groups(monkeypatch, tmp_path, groups, trusted, accounts=None):
    """Put fictional groups in front of the node's NSS for deploy.py alone,
    falling through to the real lookup for any other name -- the reader
    group is the test user's real primary group."""
    accounts = ACCOUNTS if accounts is None else accounts
    real_getgrnam = deploy.grp.getgrnam
    monkeypatch.setattr(deploy.grp, "getgrnam",
                        lambda name: groups[name] if name in groups
                        else real_getgrnam(name))
    monkeypatch.setattr(deploy.pwd, "getpwnam", lambda name: accounts[name])
    monkeypatch.setattr(deploy.pwd, "getpwall", lambda: list(accounts.values()))
    # GID_MIN above the test user's own gid, which is what a group-writable
    # tmp directory really carries -- whatever that gid is on the machine
    # running the suite.
    monkeypatch.setattr(deploy, "LOGIN_DEFS", _login_defs(
        tmp_path, "UID_MIN 1000\nUID_MAX 60000\nGID_MIN %d\n"
        % max(1000, os.getgid() + 1)))
    monkeypatch.setattr(deploy, "TRUSTED_GROUPS", tuple(trusted))


def test_the_dry_run_makes_the_member_check_and_refuses_as_the_install_does(
        tmp_path, monkeypatch):
    """As an ordinary user: NSS and login.defs are readable by anyone, so
    the dry run does not report the check as unknown -- it makes it, and
    refuses with the install's own words. Mutation: skip the groups step
    when not privileged and the dry run returns 0."""
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    _stub_groups(monkeypatch, tmp_path, {"svc-log": _Group(110, ["user-a"])},
                 trusted=("svc-log",))

    def refusal(dry_run, root):
        monkeypatch.setattr(deploy, "_is_root", lambda: root)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = deploy.system_execute(_args(tmp_path, dry_run=dry_run))
        return rc, [l for l in err.getvalue().splitlines()
                    if l.startswith("deploy.py: refusing trusted_groups")]

    dry = refusal(dry_run=True, root=False)
    real = refusal(dry_run=False, root=True)
    assert dry[0] == real[0] == 6, (dry, real)
    assert dry[1] == real[1] and dry[1], (dry, real)


def test_a_reader_group_that_does_not_resolve_refuses_both_callers(
        tmp_path, monkeypatch):
    pass_prefix_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    monkeypatch.setattr(deploy, "DEFAULT_SPOOL_GROUP", "no-such-group-wbtest")
    for dry_run, root in ((True, False), (False, True)):
        monkeypatch.setattr(deploy, "_is_root", lambda root=root: root)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = deploy.system_execute(_args(tmp_path, dry_run=dry_run))
        assert rc == 6, err.getvalue()
        assert "refusing spool_group=no-such-group-wbtest" in err.getvalue()


def test_an_accepted_trusted_gid_reaches_the_spool_chain_and_only_it(
        tmp_path, monkeypatch):
    """End to end through preflight(): a group-writable spool ancestor whose
    gid a clean listed group holds is accepted; take the group off the list
    and the same tree refuses."""
    _trusting_me(monkeypatch)
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    shared = _group_writable(str(tmp_path / "log"))
    monkeypatch.setattr(deploy, "DEFAULT_SPOOL_DIR", os.path.join(shared, "wb"))
    _stub_groups(monkeypatch, tmp_path,
                 {"svc-log": _Group(os.getgid(), ["svc-d"])}, trusted=("svc-log",),
                 accounts={"svc-d": ACCOUNTS["svc-d"]})
    rc, _checks = deploy.preflight(_args(tmp_path), privileged=True, out=io.StringIO())
    assert rc == 0
    monkeypatch.setattr(deploy, "TRUSTED_GROUPS", ())
    err = io.StringIO()
    # validate_root_write_paths() writes to sys.stderr, not to `out`.
    with contextlib.redirect_stderr(err):
        rc, _checks = deploy.preflight(_args(tmp_path), privileged=True, out=err)
    assert rc == 6 and shared in err.getvalue(), err.getvalue()


# --- the migration ---------------------------------------------------------

def _old_spool(tmp_path, mode=0o755, files=("reaper-audit.jsonl", "reaper-state.json"),
               file_mode=0o644):
    spool = str(tmp_path / "var-log")
    os.makedirs(spool, exist_ok=True)
    os.chmod(spool, mode)
    for name in files:
        path = os.path.join(spool, name)
        with open(path, "w") as fh:
            fh.write("{}\n")
        os.chmod(path, file_mode)
    return spool


def _real_spool_checks(monkeypatch):
    """The prefix-side checks stubbed as usual, the SPOOL's ownership check
    real and driven at this user's uid."""
    real = deploy.unowned_by
    monkeypatch.setattr(deploy, "untrusted_prefix_chain",
                        lambda prefix, trusted_uids=(0,), trusted_gids=(): [])
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    monkeypatch.setattr(
        deploy, "unowned_by",
        lambda root, uid=0: real(root, uid=os.getuid())
        if os.path.abspath(root) == os.path.abspath(deploy.DEFAULT_SPOOL_DIR) else [])


def test_an_old_0755_spool_with_0644_files_previews_as_repairs_and_installs_as_02750(
        tmp_path, monkeypatch):
    """The state every node that installed before ADR-0025 is in. The
    preview lists repairs and still advertises the install; the install
    leaves 02750, the reader group and 0640 files. Mutation: make
    spool_mode_repairs() return nothing, and the files stay 0644."""
    spool = _old_spool(tmp_path)
    _real_spool_checks(monkeypatch)
    monkeypatch.setattr(deploy, "run", recording_run([]))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert deploy.system_preview(_args(tmp_path)) == 0
    assert "will REPAIR rather than refuse" in out.getvalue(), out.getvalue()
    for name in (spool, os.path.join(spool, "reaper-audit.jsonl")):
        assert "%s: gid %d mode" % (name, os.getgid()) in out.getvalue()

    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    with contextlib.redirect_stdout(io.StringIO()):
        assert deploy.system_execute(_args(tmp_path)) == 0
    info = os.lstat(spool)
    assert stat.S_IMODE(info.st_mode) == 0o2750 and info.st_gid == os.getgid()
    for name in ("reaper-audit.jsonl", "reaper-state.json"):
        info = os.lstat(os.path.join(spool, name))
        assert stat.S_IMODE(info.st_mode) == 0o640, (name, oct(info.st_mode))
        assert info.st_gid == os.getgid()


def test_a_group_writable_spool_still_refuses(tmp_path, monkeypatch):
    _old_spool(tmp_path, mode=0o775)
    _real_spool_checks(monkeypatch)
    monkeypatch.setattr(deploy, "_is_root", lambda: True)
    calls = []
    monkeypatch.setattr(deploy, "run", recording_run(calls))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert deploy.system_execute(_args(tmp_path)) == 6
    assert "writable beyond root" in err.getvalue(), err.getvalue()
    assert calls == [], calls


def test_setgid_on_a_spool_file_still_refuses(tmp_path, monkeypatch):
    spool = _old_spool(tmp_path, files=("reaper-state.json",), file_mode=0o2644)
    assert os.lstat(os.path.join(spool, "reaper-state.json")).st_mode & stat.S_ISGID
    _real_spool_checks(monkeypatch)
    bad = deploy.audit_dir_blockers(spool)
    assert [(c, os.path.basename(p)) for c, p, _r in bad] == [
        ("setuid", "reaper-state.json")], bad
    assert [r for r in deploy.spool_mode_repairs(spool)
            if r[0] != spool] == [], "a setid file is not a repair"


def test_setgid_on_the_spool_directory_is_the_decision_not_a_finding(
        tmp_path, monkeypatch):
    """Mutation: drop `_is_spools_own_setgid()` from audit_dir_blockers()
    and every installed spool refuses its own reinstall."""
    spool = _old_spool(tmp_path, mode=0o2750, files=())
    _real_spool_checks(monkeypatch)
    assert deploy.audit_dir_blockers(spool) == []
    os.chmod(spool, 0o4750)
    assert [c for c, _p, _r in deploy.audit_dir_blockers(spool)] == ["setuid"]


# --- no root write into the spool follows a link --------------------------

def test_the_sweep_and_the_create_follow_no_link(tmp_path, monkeypatch):
    """The spool swapped for a directory of links named like the trail and
    the state, each at a sentinel: the sweep skips every one, and the
    blockers refuse them by name. Mutation: `os.chmod(path, 0o640)` on the
    listed paths instead of the no-follow fd, and the sentinel's mode moves."""
    spool = str(tmp_path / "var-log")
    os.makedirs(spool)
    os.chmod(spool, 0o755)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("not the walk-blocker's\n")
    sentinel.chmod(0o600)
    for name in ("reaper-audit.jsonl", "reaper-state.json",
                 "uncovered-mounts.state.new", deploy.DEFAULT_AUDIT_FILENAME):
        os.symlink(str(sentinel), os.path.join(spool, name))
    before = (sentinel.read_bytes(), stat.S_IMODE(sentinel.stat().st_mode))

    deploy.repair_spool(spool, os.getgid())
    deploy.create_spool(spool, os.getgid())
    assert (sentinel.read_bytes(), stat.S_IMODE(sentinel.stat().st_mode)) == before
    _real_spool_checks(monkeypatch)
    monkeypatch.setattr(deploy, "DEFAULT_SPOOL_DIR", spool)
    refused = [os.path.basename(p) for c, p, _r in deploy.audit_dir_blockers(spool)
               if c == "symlink"]
    assert sorted(refused) == sorted(["reaper-audit.jsonl", "reaper-state.json",
                                      "uncovered-mounts.state.new",
                                      deploy.DEFAULT_AUDIT_FILENAME]), refused


def test_a_spool_that_is_a_link_to_a_directory_is_not_created_through(tmp_path):
    """`create_spool()` refuses a link at the spool's name and leaves the
    target's mode and group as they were. Mutation: `install -d -m 2750`
    (or `os.chmod` on the path) and the target becomes 2750."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    target.chmod(0o755)
    spool = str(tmp_path / "var-log")
    os.symlink(str(target), spool)
    with pytest.raises(deploy.SpoolRefused):
        deploy.create_spool(spool, os.getgid())
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    deploy.repair_spool(spool, os.getgid())
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_an_unreadable_login_defs_is_unknown_to_the_dry_run_and_a_refusal_as_root(
        tmp_path, monkeypatch):
    """A check the reader may not make is reported as not made -- and the
    spool chain, which depends on its answer, is not judged with a guess.
    Root that cannot read it refuses. Mutation: judge the spool chain with
    root alone instead, and the dry run refuses a group-writable ancestor
    the install might accept."""
    _trusting_me(monkeypatch)
    monkeypatch.setattr(deploy, "untraversable_for_users", lambda prefix: [])
    monkeypatch.setattr(deploy, "unowned_by", lambda root, uid=0: [])
    shared = _group_writable(str(tmp_path / "log"))
    monkeypatch.setattr(deploy, "DEFAULT_SPOOL_DIR", os.path.join(shared, "wb"))
    monkeypatch.setattr(deploy, "TRUSTED_GROUPS", ("svc-log",))

    def unreadable(_names, **_kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(deploy, "resolve_trusted_groups", unreadable)
    with contextlib.redirect_stderr(io.StringIO()):
        rc, checks = deploy.preflight(_args(tmp_path, dry_run=True),
                                      privileged=False, out=io.StringIO())
    assert rc == 0
    unknown = [(c.name, c.subject) for c in checks if c.state == deploy.CHECK_UNKNOWN]
    assert ("paths", os.path.join(shared, "wb")) in unknown, unknown
    assert ("trusted_groups", deploy.LOGIN_DEFS) in unknown, unknown
    out = io.StringIO()
    rc, _checks = deploy.preflight(_args(tmp_path), privileged=True, out=out)
    assert rc == 6 and "could not be made even as root" in out.getvalue()
