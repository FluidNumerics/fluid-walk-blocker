"""The shell installer, driven as a STAMPED copy in a sandbox.

install.sh writes real files (hook blocks, an audit log directory, a symlink
farm) at locations that are literals stamped in by the build (ADR-0013), so
every case here stamps a copy whose locations sit under tmp_path -- through
`_install_helpers.stamped_install()`, the one sanctioned way -- and reads back
what was actually written. Root is faked via a stub `id` on PATH, never an
environment override (ADR-0004). No test may reach a real journal: the copy's
audit sink is neutralized in both of its forms.
"""

import json
import os
import shutil
import stat
import subprocess

import pytest

from conftest import ROOT
from walk_blocker import __version__, stamp
from walk_blocker import search_rules as R

import _install_helpers as H
from _install_helpers import (
    BASH_STUB_IGNORES_HOOK, BASH_STUB_SOURCES_HOOK, FIXTURE_MOUNTS,
    FIXTURE_POLICY, INSTALL_SH, Layout, NO_LOGGER, SH, STAT_ROOT_755,
    STOCK_BASHRC, TEST_LOGGER, TOOL_STUB, ZSH_STUB_IGNORES_HOOK,
    populate_bin, relink_with_a_recording_logger, run_install, site_values,
    stage_walk_job, stamped_install, sandbox_env, write_stub,
)

BEGIN = "# >>> walk-blocker >>>"
END = "# <<< walk-blocker <<<"
ORIG = ".walk-blocker.orig"


def stat_stub_uid_for(path_glob, uid_mode):
    """A `stat` stand-in reporting `uid_mode` for paths matching `path_glob`
    and root/0755 for everything else, since a test cannot create a
    root-owned tree."""
    return ('#!/bin/sh\ncase "$3" in\n'
            '  %s) printf "%s\\n" ;;\n'
            '  *) printf "0 755\\n" ;;\n'
            'esac\n' % (path_glob, uid_mode))


# --------------------------------------------------------------------------
# the markers: what the build must stamp
# --------------------------------------------------------------------------

def test_every_marker_is_a_sentinel_in_tree_and_is_stamped_by_the_fixture(tmp_path):
    """The in-tree file carries a sentinel on every marker line, so a copy
    that was never stamped cannot be mistaken for a built one; and the
    fixture's `site_values()` supplies exactly the sources the markers name,
    so a marker added to one and not the other fails here rather than in a
    build nobody ran. `check_text` with every source required is the same
    oracle `walk-blocker build --check` uses."""
    text = open(INSTALL_SH).read()
    markers = stamp.find_markers(text)
    assert markers, "install.sh carries no markers"
    for m in markers:
        assert m.value.startswith("'@@") and m.value.endswith("@@'"), (
            "line %d: %s is %s, not a sentinel" % (m.lineno, m.name, m.value))
    sources = sorted(m.source for m in markers)
    assert "VERSION" in sources
    assert "site.toml:derived.mount_overrides" in sources

    layout = Layout(tmp_path)
    values = site_values(layout)
    stamped = stamp.stamp_text(text, values, "sh")
    assert stamp.check_text(stamped, values, "sh", required=sources) == []
    assert "@@" not in stamped, "a sentinel survived stamping"
    assert set(m.key for m in markers) == set(values), (
        "the fixture supplies a source no marker reads, or misses one")


def test_the_stamped_copy_is_dash_clean(tmp_path):
    """`sh -n` under the node's shell over a stamped copy (ADR-0015)."""
    layout = stamped_install(tmp_path)
    result = subprocess.run([SH, "-n", str(layout.script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# the symlink farm
# --------------------------------------------------------------------------

def test_only_tools_that_exist_are_shimmed(tmp_path):
    """A shim named `ag` where ag is absent makes `command -v ag` succeed and
    silently changes how other people's scripts probe for tools."""
    result, layout = run_install(tmp_path, ["--relink"], tools=("find", "grep"))
    assert result.returncode == 0, result.stderr
    linked = layout.shims()
    assert linked == {"find", "grep"}
    for absent in ("fd", "rg", "bfs", "tree"):
        assert absent not in linked


def test_unwrapped_tools_are_never_shimmed_even_when_present(tmp_path):
    """`[shim].unwrapped_tools` (fzf and sk in the fixture site) genuinely
    resolve on the tool path here -- real stub binaries, not an absence --
    and still must not be linked: they are reached through keybindings and
    editor plugins, where a refusal is an invisible no-op."""
    result, layout = run_install(tmp_path, ["--relink"], tools=("find", "grep", "fzf", "sk"))
    assert result.returncode == 0, result.stderr
    assert layout.shims() == {"find", "grep"}


def test_a_tool_that_appears_later_gets_wrapped_on_the_next_reconcile(tmp_path):
    """The reaper's timer reruns --relink, so a tool installed today is
    wrapped today."""
    _r, layout = run_install(tmp_path, ["--relink"], tools=("find",))
    assert layout.shims() == {"find"}
    _r2, layout = run_install(tmp_path, ["--relink"], tools=("find", "rg"), layout=layout)
    assert layout.shims() == {"find", "rg"}


def test_link_farm_uses_the_stamped_tool_path_not_the_callers_PATH(tmp_path):
    """Which tools get wrapped must not depend on who ran the installer.
    `link_farm` walks `[install].tool_search_path`, never the caller's $PATH.

    Two assertions, not one: either half passing alone would still be
    consistent with this reading $PATH after all, just with the stamped path
    layered on top rather than replacing it."""
    tool_dir = tmp_path / "toolpath"
    tool_dir.mkdir()
    # `find`: only on the stamped tool path, absent from the caller's PATH.
    write_stub(tool_dir / "find", TOOL_STUB)
    # `grep`: only on the caller's PATH (the sandbox bin). Must NOT be wrapped.
    result, layout = run_install(
        tmp_path, ["--relink"], tools=("grep",),
        **{"install.tool_search_path": [str(tool_dir)]})
    assert result.returncode == 0, result.stdout + result.stderr
    linked = layout.shims()
    assert "find" in linked, "present on the stamped tool path; must be wrapped"
    assert "grep" not in linked, "present only on the caller's PATH; must not be wrapped"


def test_a_name_dropped_from_the_TABLE_stops_being_shadowed(tmp_path):
    """The link loop walks the table, so a name the TABLE stops carrying is
    never visited at all and its symlink would survive every future
    reconcile -- a shim on every user's PATH under a name the dispatcher no
    longer knows. Driven with a stale link planted by hand."""
    layout = Layout(tmp_path)
    layout.bin.mkdir(parents=True)
    os.symlink("/nonexistent/guard.sh", str(layout.bin / "updatedb"))
    result, layout = run_install(tmp_path, ["--relink"], tools=("find", "grep"), layout=layout)
    assert result.returncode == 0, result.stderr
    assert layout.shims() == {"find", "grep"}
    assert "updatedb" in result.stdout, (
        "removing a shim from every user's PATH is not a silent act")


def test_uninstall_removes_a_shim_for_a_name_no_longer_in_the_table(tmp_path):
    """Same defect on the teardown path, where it also ate the directory:
    `rmdir` fails on a non-empty directory and the uninstall arm sends that
    into `|| true`, so it reported success while leaving both behind."""
    layout = Layout(tmp_path)
    layout.bin.mkdir(parents=True)
    for name in ("find", "updatedb"):
        os.symlink("/nonexistent/guard.sh", str(layout.bin / name))
    result, layout = run_install(tmp_path, ["--uninstall"], tools=("find", "grep"),
                                 layout=layout, fake_uid=0)
    assert result.returncode == 0, result.stderr
    assert not layout.bin.exists(), "the shim directory outlived the uninstall"


def test_a_tool_that_disappears_stops_being_shadowed(tmp_path):
    _r, layout = run_install(tmp_path, ["--relink"], tools=("find", "rg"))
    _r, layout = run_install(tmp_path, ["--relink"], tools=("find",), layout=layout)
    assert "rg" not in os.listdir(str(layout.bin)), (
        "a dangling shim for a removed tool would answer `command -v` for a "
        "binary that is no longer there")


def test_the_alternative_every_refusal_names_is_actually_linked(tmp_path):
    """guard.sh's refusal ends with `walk-job -- <tool> ...`, and nothing on
    the node provides walk-job but this installer. The advertised name is
    read out of the rendered guard.sh rather than written here, so the two
    cannot drift apart: rename the tool in one place and this fails."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stderr
    guard = (layout.shim_dir / "guard.sh").read_text()
    advice = [line for line in guard.splitlines() if "-- %s ..." in line]
    assert len(advice) == 1, advice
    advertised = advice[0].split("'", 1)[1].split()[0]
    link = layout.bin / advertised
    assert os.path.islink(str(link)), (
        "%s is advertised by every refusal and is not on PATH after an install: %s"
        % (advertised, sorted(os.listdir(str(layout.bin)))))
    assert os.path.realpath(str(link)) == str(layout.prefix / advertised)


def test_the_walk_job_link_survives_every_relink(tmp_path):
    """The sweep removes any link the name list does not claim. walk-job is
    not in the wrapped-name table, so an unclaimed link would be created and
    then deleted on the same run -- or created at install and swept at the
    first poll."""
    layout = None
    for _ in range(2):
        result, layout = run_install(tmp_path, ["--relink"], tools=("find", "grep"), layout=layout)
        assert result.returncode == 0, result.stderr
        assert os.path.islink(str(layout.bin / "walk-job")), sorted(os.listdir(str(layout.bin)))
    swept = [line for line in result.stdout.splitlines()
             if "no longer wrapped" in line or "not linked" in line]
    assert not any("walk-job" in line for line in swept), swept


def test_the_uninstall_takes_the_walk_job_link_with_it(tmp_path):
    """An empty keep-list sweeps the directory, so walk-job goes the way the
    shims do -- and the `rmdir` that follows only succeeds if nothing is
    left. The payload itself is left for deploy.py."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stderr
    assert os.path.islink(str(layout.bin / "walk-job"))
    result, layout = run_install(tmp_path, ["--uninstall"], layout=layout, fake_uid=0)
    assert result.returncode == 0, result.stderr
    assert not layout.bin.exists()
    assert (layout.prefix / "walk-job").exists()


def test_an_install_refuses_a_prefix_with_no_walk_job(tmp_path):
    """Same treatment as a missing guard.sh: the installer is about to put
    this file on every user's PATH, and the answer to a prefix without it is
    to rerun deploy.py rather than to link a name at nothing."""
    result, layout = run_install(tmp_path, ["--system"],
                                 fake_uid=0, walk_job=False)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "walk-job" in result.stderr
    assert not layout.bin.exists(), "the farm was built before the payload was checked"


def test_a_user_owned_walk_job_is_refused(tmp_path):
    """The link goes on every user's PATH and every refusal tells them to run
    it, so its owner could choose what they all execute. Checked exactly as
    guard.sh is."""
    result, layout = run_install(
        tmp_path, ["--system"], fake_uid=0,
        stat_body=stat_stub_uid_for("*/walk-job", "1000 755"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "walk-job is owned by" in result.stderr
    assert not layout.bin.exists(), "the farm was built anyway"


# --------------------------------------------------------------------------
# the reconcile's reports
# --------------------------------------------------------------------------

def test_relink_reports_a_hook_a_package_update_removed(tmp_path):
    """A shell's system rc file is typically its package's protected
    configuration, so a routine upgrade of that package is the likeliest
    thing to remove Layer 1's hook (ADR-0008). Layer 1 would go on working in
    interactive shells and be absent for `ssh host 'cmd'`, the incident
    shape. The report names the STAMPED package and no package manager: which
    one a site runs is not this tree's to know."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)          # a stock file with no block
    result, layout = run_install(tmp_path, ["--relink"], tools=("find", "grep"), layout=layout)
    assert result.returncode == 0, (
        "a Layer 1 diagnosis must not fail ExecStartPre and take Layer 2 down with it:\n"
        + result.stderr)
    assert "block-missing" in result.stderr
    assert "bash package" in result.stderr, (
        "the message has to name the likely cause: the stamped package")
    assert "upgrade" in result.stderr
    for manager in ("dpkg", "apt", "rpm", "yum", "dnf", "conffile"):
        assert manager not in result.stderr.lower(), manager


def test_relink_reports_a_bashrc_that_is_not_there_at_all(tmp_path):
    """The other shape, and it must not be mistaken for a healthy one."""
    layout = Layout(tmp_path, bashrc=tmp_path / "no-such-bashrc")
    result, _l = run_install(tmp_path, ["--relink"], tools=("find", "grep"), layout=layout)
    assert result.returncode == 0, result.stderr
    assert "file-absent" in result.stderr


def test_relink_reports_a_zshenv_hook_a_package_update_removed(tmp_path):
    """Mirrors the bash case on the zsh file, naming the zsh package."""
    layout = Layout(tmp_path)
    layout.zshenv.write_text("# a stock zshenv, no block\n")
    result, _l = run_install(tmp_path, ["--relink"], tools=("find", "grep"), layout=layout)
    assert result.returncode == 0, result.stderr
    assert "block-missing" in result.stderr
    assert "zsh package" in result.stderr


def test_relink_reports_a_zshenv_that_is_not_there_at_all(tmp_path):
    layout = Layout(tmp_path, zshenv=tmp_path / "no-such-zshenv")
    result, _l = run_install(tmp_path, ["--relink"], tools=("find", "grep"), layout=layout)
    assert result.returncode == 0, result.stderr
    assert "file-absent" in result.stderr


def test_relink_does_not_hang_on_a_fifo_bashrc(tmp_path):
    """The `-f` test is load-bearing: reading a FIFO blocks forever, which
    here would hang ExecStartPre and stop the reaper on every poll. Reported
    as its own state, too -- "absent" and "something that is not a regular
    file is at that path" mean different things to whoever reads the journal.

    A healthy zshenv sits beside the fifo so the assertions stay about the
    fifo; fish is left absent so its report stays out of them."""
    layout = Layout(tmp_path)
    os.mkfifo(str(layout.bashrc))
    layout.zshenv.write_text("%s\nPATH=%s:$PATH\n%s\n" % (BEGIN, layout.bin, END))
    result, _l = run_install(tmp_path, ["--relink"], tools=("find", "grep"),
                             layout=layout, fish_absent=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "not-a-regular-file" in result.stderr
    assert "file-absent" not in result.stderr


def test_relink_is_silent_when_the_hook_is_intact(tmp_path):
    """Healthy is silent: a check that fires on a healthy node teaches
    everyone to ignore it. Install first, so the block and the farm are both
    real, then relink from the DEPLOYED copy, which is what the timer runs."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    installed, layout = run_install(tmp_path, ["--system"],
                                    fake_uid=0, layout=layout)
    assert installed.returncode == 0, installed.stderr
    assert "verified" in installed.stdout
    result, _l = run_install(tmp_path, ["--relink"], fake_uid=0, layout=layout,
                             script=layout.script)
    assert result.returncode == 0, result.stderr
    assert "hook" not in result.stderr.lower(), (
        "the healthy path must say nothing about the hook: %r" % result.stderr)


def test_a_present_block_that_does_not_fire_is_reported_as_not_firing(tmp_path):
    """The markers being there is necessary, not sufficient: the shell has to
    actually source the file. This is the `not-firing` state, and it must not
    be misdiagnosed as the block being missing."""
    layout = Layout(tmp_path)
    for f in (layout.bashrc, layout.zshenv):
        f.write_text("%s\nPATH=%s:$PATH\n%s\n" % (BEGIN, layout.bin, END))
    result, _l = run_install(tmp_path, ["--relink"], tools=("find", "grep"),
                             layout=layout, bash_ignores_bashrc=True)
    assert result.returncode == 0, result.stderr
    assert "bash PATH hook not-firing" in result.stderr
    assert "block-missing" not in result.stderr, (
        "the markers ARE present; saying otherwise misdiagnoses it")


def test_relink_reports_fish_hook_state_only_when_fish_is_present(tmp_path):
    """A best-effort hook is checked only when its shell resolves; otherwise
    a node without fish would report file-absent on every poll forever."""
    result, _l = run_install(tmp_path, ["--relink"], tools=("find", "grep"), fish_absent=True)
    assert result.returncode == 0, result.stderr
    # Not a blanket "fish" not in output: tmp_path is named after this test.
    assert "fish PATH hook" not in result.stderr


def test_the_relink_refusal_reaches_the_audit_sink(tmp_path):
    """The positive control for the sink isolation, and the reason it is
    needed: every other test asserts that NO record is emitted, and "no
    records" reads identically whether the sink is neutralized or the
    reporting is simply broken.

    A DETERMINISTIC refusal: an unprivileged relink skips the root-only
    checks, so the prefix is made unwritable and `install -d $PREFIX/bin`
    fails under `set -e`."""
    layout = Layout(tmp_path)
    layout.prefix.mkdir(mode=0o500)
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode != 0, "the refusal this test reports on did not happen:\n" + result.stdout
    refused = [r for r in records if r["action"] == "relink_refused"]
    assert refused, "a refusal the unit ignores must still reach the audit sink:\n" + result.stderr
    assert refused[0]["state"] == "exit-%d" % result.returncode
    assert refused[0]["layer"] == "shim"
    recorded = (tmp_path / "logger-calls.txt").read_text()
    assert "-t walk-blocker" in recorded, recorded


def test_a_tool_that_stops_being_wrapped_reaches_the_audit_trail(tmp_path):
    """A tool that stops being wrapped is a coverage change, invisible by
    definition -- one that STARTS being wrapped shows up as a wrapped tool,
    one that stops shows up as nothing at all, on an unattended poll."""
    layout = Layout(tmp_path)
    layout.bin.mkdir(parents=True)
    # A name the table no longer carries, and a name whose tool is gone: the
    # sweep path and the link-farm path, which count the same way.
    for name in ("updatedb", "du"):
        os.symlink("/nonexistent/guard.sh", str(layout.bin / name))
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 0, result.stderr
    changes = [r for r in records if r["action"] == "coverage_change"]
    assert changes, "an unlink reached nothing: %s" % records
    assert changes[0]["state"] == "unwrapped-2", changes
    # Counts, not names: a swept entry is a filename read off a directory,
    # and sg_report has no way to escape one for JSON.
    assert "updatedb" not in json.dumps(changes[0])


def test_the_unwrapped_count_is_not_glob_expanded(tmp_path):
    """install.sh does not `set -f` the way guard.sh does, so counting by
    re-splitting the swept list would glob-expand a swept entry named `*`
    against the working directory. Counted one entry at a time instead."""
    layout = Layout(tmp_path)
    layout.bin.mkdir(parents=True)
    for name in ("*", "updatedb"):
        os.symlink("/nonexistent/guard.sh", str(layout.bin / name))
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 0, result.stderr
    changes = [r for r in records if r["action"] == "coverage_change"]
    assert changes, records
    assert changes[0]["state"] == "unwrapped-2", "the count was glob-expanded: %s" % changes
    assert not (layout.bin / "*").exists(), "the odd name was not removed"


def test_a_relink_that_changes_nothing_reports_no_coverage_change(tmp_path):
    """Silent when healthy, like every other report here."""
    layout = Layout(tmp_path)
    first, _records = relink_with_a_recording_logger(tmp_path, layout)
    assert first.returncode == 0, first.stderr
    second, records = relink_with_a_recording_logger(tmp_path, layout)
    assert second.returncode == 0, second.stderr
    assert [r for r in records if r["action"] == "coverage_change"] == [], (
        "a steady state must be silent: %s" % records)


# --------------------------------------------------------------------------
# the mount table (ADR-0016, tier three)
# --------------------------------------------------------------------------

def uncovered(records):
    return sorted((r["mount"], r["fstype"], r["state"])
                  for r in records if r["action"] == "uncovered_mount")


def test_the_relink_reports_a_remote_mount_no_override_covers(tmp_path):
    """`/archive` is an NFS export with no `[[filesystems.mounts]]` entry, so
    it is guarded on its tier-one default and the reconcile says so, by
    name, with the class the shim will apply."""
    layout = Layout(tmp_path)
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 0, result.stderr
    assert ("/archive", "nfs4", "expensive") in uncovered(records), records


def test_an_overridden_mount_is_not_reported(tmp_path):
    """Both overridden mounts are quiet -- the expensive one with a depth and
    the cheap one -- and covering `/archive` too makes the report empty."""
    layout = Layout(tmp_path)
    _result, records = relink_with_a_recording_logger(tmp_path, layout)
    mounts = [m for m, _fs, _c in uncovered(records)]
    assert "/home" not in mounts, records
    assert "/opt/site-tools" not in mounts, records

    covered = H.make_policy(mounts=dict(FIXTURE_POLICY.mounts, **{"/archive": ("expensive", 3)}))
    layout2 = Layout(tmp_path / "covered")
    dest = layout2.tmp_path / "copy"
    stamped_install(tmp_path, dest=dest, layout=layout2, policy=covered,
                    **{"derived.mount_overrides": H.render_shim_module.mount_overrides(covered)})
    result, records = relink_with_a_recording_logger(tmp_path, layout2, script=layout2.script)
    assert result.returncode == 0, result.stderr
    assert uncovered(records) == [], records


def test_a_local_mount_on_its_cheap_default_is_not_reported(tmp_path):
    """A cheap-by-default local mount cannot produce a false refusal, and one
    line per pseudo-filesystem per poll would bury the line this record
    exists to surface."""
    layout = Layout(tmp_path)
    _result, records = relink_with_a_recording_logger(tmp_path, layout)
    mounts = [m for m, _fs, _c in uncovered(records)]
    assert "/" not in mounts and "/run" not in mounts, records


def test_the_mount_report_agrees_with_the_rule_table(tmp_path):
    """The installer carries its own `sh` copy of the tier-one default, so
    the set it reports is derived here from `classify_mount()` and compared:
    every uncovered row the table calls expensive, and nothing else. A table
    with every branch in it -- listed type, remote by source, remote by
    option, local unknown, an unknown remote type -- so a divergence in any
    one shows."""
    table = (
        "/dev/sda1 / ext4 rw 0 0\n"
        "fast /scratch wekafs rw 0 0\n"                  # listed type
        "srv:/exp /mnt/exp unknownfs rw 0 0\n"           # remote by source, unknown type
        "//srv/share /mnt/smb cifs rw,_netdev 0 0\n"     # remote by option
        "gw /mnt/gw fuse.gateway rw,addr=192.0.2.5 0 0\n"  # remote by option, fuse type
        "tmpfs /run tmpfs rw 0 0\n"                      # local unknown -> cheap
        "fast /home wekafs rw 0 0\n"                     # overridden
        "nas:/tools /opt/site-tools/ nfs4 rw 0 0\n"      # overridden, trailing slash
    )
    expected = []
    for line in table.splitlines():
        src, mnt, fs, opts = line.split()[:4]
        mnt = mnt[:-1] if mnt != "/" and mnt.endswith("/") else mnt
        if mnt in FIXTURE_POLICY.mounts:
            continue
        if R.classify_mount((src, mnt, fs, opts), FIXTURE_POLICY) == "expensive":
            expected.append((mnt, fs, "expensive"))
    assert expected, "the fixture table exercises nothing"

    layout = Layout(tmp_path)
    layout.mount_table.write_text(table)
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 0, result.stderr
    assert uncovered(records) == sorted(expected)


def test_an_escaped_mount_point_is_skipped_and_an_odd_one_is_unrepresentable(tmp_path):
    """The shim's reader skips an octal-escaped mount point, so the report
    does too; and a mount point sg_report cannot put in JSON safely is
    recorded as such rather than dropped or interpolated raw."""
    layout = Layout(tmp_path)
    layout.mount_table.write_text(
        FIXTURE_MOUNTS
        + "nas:/a /mnt/with\\040space nfs4 rw 0 0\n"
        + "nas:/b /mnt/q\"uote nfs4 rw 0 0\n")
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 0, result.stderr
    got = uncovered(records)
    assert not any("space" in m for m, _f, _c in got), got
    assert ("unrepresentable", "nfs4", "expensive") in got, got
    for r in records:
        json.dumps(r)   # every record parsed already; none was malformed


def test_the_mount_report_never_fails_the_relink(tmp_path):
    """An unreadable table is a judgement the shim also cannot make; the
    reconcile says nothing and finishes."""
    layout = stamped_install(tmp_path, dest=tmp_path / "copy")
    layout.mount_table.unlink()
    result, records = relink_with_a_recording_logger(tmp_path, layout, script=layout.script)
    assert result.returncode == 0, result.stderr
    assert uncovered(records) == []


# --------------------------------------------------------------------------
# the shells the node uses
# --------------------------------------------------------------------------

def test_the_install_tests_run_under_the_shells_the_node_uses():
    """Makes the shell under test visible rather than assumed. install.sh's
    shebang is `#!/bin/sh`, which is dash on the node and bash on many
    workstations; where dash is absent this SKIPS with the reason, because a
    green run on the wrong shell is the kind of silence that reads as
    coverage."""
    if not shutil.which("dash"):
        pytest.skip("dash is not installed, so these tests are exercising %r and NOT "
                    "the /bin/sh the node runs. Install it to close the gap." % SH)
    assert SH == shutil.which("dash"), "dash is installed but the harness is using %r" % SH


@pytest.mark.parametrize("shell", ["dash", "bash"])
def test_an_approved_install_runs_the_same_under_both_shells(tmp_path, shell):
    """CI drives every shell tool under both dash and bash invoked as sh
    (ADR-0015). A construct that passes on one and fails on the other is the
    failure this exists to catch, so the whole install runs under each."""
    if not shutil.which(shell):
        pytest.skip("%s is not installed" % shell)
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"],
                                 fake_uid=0, layout=layout, shell=shutil.which(shell))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("verified") == 3, result.stdout
    assert layout.shims() == {"find", "grep", "du"}


def test_the_exit_trap_preserves_the_status_under_dash(tmp_path):
    """`exit` inside an EXIT trap must not re-enter the trap and must not
    rewrite the status -- POSIX says so, bash and dash both implement it, and
    the repo's own history says to check rather than trust that."""
    layout = Layout(tmp_path, prefix=tmp_path / "locked")
    layout.prefix.mkdir(mode=0o500)
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 1, "the trap rewrote or swallowed the status under %s: %s" % (SH, result.stderr)
    assert [r for r in records if r["action"] == "relink_refused"], (
        "the trap did not report under %s: %s" % (SH, records))


def test_no_test_stamps_the_installer_without_neutralizing_the_sink():
    """A safety argument that call sites must remember is one the next call
    site forgets, so this makes the rule checkable: `stamped_install()` in
    the helper is the only place the installer's text is stamped, and the
    only place the logger fallback is renamed."""
    helper = open(os.path.join(ROOT, "tests", "_install_helpers.py")).read()
    this = open(os.path.join(ROOT, "tests", "test_install.py")).read()
    needle = "stamp.stamp_" + "text("
    assert helper.count(needle) == 1, "the helper stamps in more than one place"
    assert this.count(needle) == 1, (
        "a test stamps the installer itself; only the marker test may, and it never runs the result")
    assert "copy" + "tree(" not in this
    assert "SG_LOGGER=" in open(INSTALL_SH).read(), (
        "install.sh no longer has the constant the fixture stamps")
    assert "command -v logger" in open(INSTALL_SH).read(), (
        "install.sh no longer has the fallback the fixture renames")


# --------------------------------------------------------------------------
# --version
# --------------------------------------------------------------------------

def test_version_prints_the_stamped_version_and_needs_no_payload(tmp_path):
    """Answers on a node where the install is broken, which is when it is
    asked: no payload, no PATH, no root."""
    layout = stamped_install(tmp_path, dest=tmp_path / "alone")
    (layout.tmp_path / "alone" / "guard.sh").unlink()
    (layout.tmp_path / "alone" / "wrapped_names.sh").unlink()
    result = subprocess.run([SH, str(layout.script), "--version"],
                            capture_output=True, text=True, env={"PATH": ""})
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("\nwalk-blocker %s\n" % __version__), result.stdout


def test_version_reflects_a_different_stamped_value(tmp_path):
    layout = stamped_install(tmp_path, VERSION="9.8.7")
    result = subprocess.run([SH, str(layout.script), "--version"], capture_output=True, text=True)
    assert result.stdout.endswith("\nwalk-blocker 9.8.7\n"), result.stdout


def test_dry_run_is_refused_in_the_modes_that_cannot_honour_it(tmp_path):
    """Refused, not ignored, and this is the whole point of the pair.

    `--dry-run` is parsed before the mode is known, so it is syntactically
    accepted everywhere -- but only the `--system` arm reads it. `--relink`
    rebuilds the shim farm and `--uninstall` strips the hook blocks, both
    unconditionally and both as root. Accepting the flag there and writing
    anyway is worse than refusing, because the caller believes they asked for
    a dry run and got one.

    Before ADR-0021 this argv did not parse at all: `--dry-run` did not exist
    in this script, so it fell to the unknown-argument arm. Removing the
    approval flag introduced the flag -- and the silent ignore with it.
    """
    layout = stamped_install(tmp_path)
    for mode in ("--relink", "--uninstall"):
        result = subprocess.run(
            [SH, str(layout.script), mode, "--dry-run"],
            capture_output=True, text=True)
        assert result.returncode == 64, (mode, result.stdout, result.stderr)
        assert "--dry-run applies to --system only" in result.stderr, mode
        assert mode.lstrip("-") in result.stderr, mode


def test_dry_run_is_still_honoured_by_the_system_arm(tmp_path):
    """The other half: the refusal above must not have taken the real one."""
    result, layout = run_install(tmp_path, ["--system", "--dry-run"])
    assert result.returncode == 0, result.stderr
    assert not layout.bashrc.exists()
    assert not layout.bin.exists()


def test_an_unknown_argument_and_a_missing_mode_are_usage_errors(tmp_path):
    layout = stamped_install(tmp_path)
    for argv in (["--prefix", "/x"], []):
        result = subprocess.run([SH, str(layout.script)] + argv, capture_output=True, text=True)
        assert result.returncode == 64, argv
    assert "unknown argument --prefix" in subprocess.run(
        [SH, str(layout.script), "--prefix", "/x"], capture_output=True, text=True).stderr


# --------------------------------------------------------------------------
# the system arm
# --------------------------------------------------------------------------

def test_system_mode_prints_and_does_not_execute(tmp_path):
    result, layout = run_install(tmp_path, ["--system", "--dry-run"])
    assert result.returncode == 0
    assert "As root" in result.stdout
    assert str(layout.bashrc) in result.stdout
    assert str(layout.zshenv) in result.stdout
    assert str(layout.fishconf) in result.stdout
    assert not layout.bashrc.exists(), "a --system dry run must touch nothing"
    assert not layout.bin.exists()


def test_system_mode_refuses_without_root(tmp_path):
    result, layout = run_install(tmp_path, ["--system"], fake_uid=1000)
    assert result.returncode == 3
    assert "must run as root" in result.stderr
    assert not layout.bashrc.exists()


def test_system_mode_self_executes_when_root_and_approved(tmp_path):
    """`ssh host 'cmd'` runs a non-interactive shell; a stock system bashrc's
    guard returns before anything useful runs, so the block goes above it."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stderr
    assert "verified" in result.stdout
    assert layout.shims() == {"find", "grep", "du"}
    walk_link = layout.bin / "walk-job"
    assert os.path.islink(str(walk_link))
    assert os.path.realpath(str(walk_link)) == str(layout.prefix / "walk-job")

    text = layout.bashrc.read_text()
    assert "export EDITOR=vim" in text, "the original file's content must survive"
    assert "WALK_BLOCKER_AUDIT=" in text
    assert "WALK_BLOCKER_SHIM_DIR=" in text
    # Look for the guard AFTER the block: the block's own comment describes
    # the guard shape to explain itself.
    end_marker = text.index(END)
    guard_at = text.index("*) return;;", end_marker)
    block_at = text.index("WALK_BLOCKER_SHIM_DIR=")
    assert block_at < end_marker < guard_at, "below the guard, the block never fires over ssh"


def test_the_block_exports_the_stamped_shim_dir_and_audit_path(tmp_path):
    """Sourcing the block in a fresh shell yields the two exported values the
    shim reads, and puts the shim directory first on PATH exactly once."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stderr
    probe = tmp_path / "probe.sh"
    probe.write_text('. "%s"\n. "%s"\nprintf \'%%s\\n%%s\\n%%s\\n\' "$WALK_BLOCKER_SHIM_DIR" '
                     '"$WALK_BLOCKER_AUDIT" "$PATH"\n' % (layout.bashrc, layout.bashrc))
    run = subprocess.run([SH, str(probe)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert run.returncode == 0, run.stderr
    shim_dir, audit, path = run.stdout.splitlines()
    assert shim_dir == str(layout.bin)
    assert audit == str(layout.audit)
    assert path == "%s:/usr/bin:/bin" % layout.bin, "prepended once, idempotently"


def test_system_mode_installing_twice_does_not_stack_blocks(tmp_path):
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stderr
    assert layout.bashrc.read_text().count("WALK_BLOCKER_SHIM_DIR=") == 1
    assert layout.zshenv.read_text().count("WALK_BLOCKER_SHIM_DIR=") == 1


def test_system_uninstall_leaves_the_hook_files_as_it_found_them(tmp_path):
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    layout.zshenv.write_text("# a stock zshenv\n")
    run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stderr
    assert layout.bashrc.read_text() == STOCK_BASHRC
    assert layout.zshenv.read_text() == "# a stock zshenv\n"
    assert not layout.fishconf.exists()


def test_a_first_install_backs_up_the_pre_install_hook_files(tmp_path):
    """A `.walk-blocker.orig` beside each shared file, matching what was
    there before walk-blocker ever touched it, is what a human can fall back
    to. `--uninstall` restores by STRIPPING the block, not from this file."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    layout.zshenv.write_text("# a stock zshenv\n")
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (layout.bashrc.parent / (layout.bashrc.name + ORIG)).read_text() == STOCK_BASHRC
    assert (layout.zshenv.parent / (layout.zshenv.name + ORIG)).read_text() == "# a stock zshenv\n"
    # The drop-in is generated in full; there is nothing to back up.
    assert not (layout.fishconf.parent / (layout.fishconf.name + ORIG)).exists()


def test_a_second_install_does_not_clobber_the_original_backup(tmp_path):
    """The backup is the state before walk-blocker ever touched the file --
    not the state before its most recent install."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    layout.zshenv.write_text("# a stock zshenv\n")
    run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (layout.bashrc.parent / (layout.bashrc.name + ORIG)).read_text() == STOCK_BASHRC
    assert (layout.zshenv.parent / (layout.zshenv.name + ORIG)).read_text() == "# a stock zshenv\n"


def test_no_backup_is_left_when_the_hook_file_did_not_exist(tmp_path):
    """A `.orig` snapshot of a file that was never there would misreport
    that it existed and was empty."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (layout.bashrc.parent / (layout.bashrc.name + ORIG)).exists()
    assert not (layout.zshenv.parent / (layout.zshenv.name + ORIG)).exists()


def test_system_uninstall_refuses_without_root(tmp_path):
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=1000, layout=layout)
    assert result.returncode == 3
    assert "must run as root" in result.stderr
    assert layout.bashrc.read_text() != STOCK_BASHRC, "install must still be in place"


def test_system_mode_hook_verification_catches_a_hook_that_does_not_fire(tmp_path):
    """The block can land in the hook file and still do nothing, if the
    shell never reads that file for a non-interactive remote command. So the
    installer proves the hook fires rather than reporting on having written
    it -- and says plainly that the partial state is live, not rolled back."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, bash_ignores_bashrc=True)
    assert result.returncode == 4, (
        "installer reported success with a shell that ignores the hook file\n"
        + result.stdout + result.stderr)
    assert "FAILED to verify the bash hook" in result.stderr
    assert BEGIN in layout.bashrc.read_text()
    assert layout.shims(), "the farm must not be torn down on a probe failure"
    assert "NOT rolled back" in result.stderr
    assert "interactive shells are protected" in result.stderr
    assert "--uninstall" in result.stderr


def test_system_mode_reports_a_written_but_empty_block_plainly(tmp_path):
    """The OTHER verify failure shape: nothing resolved to wrap at all, so
    there is nothing to protect -- but the block is written by the time this
    is discovered. The message has to say the block is live and name
    --uninstall, not just that nothing was linked."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, tools=())
    assert result.returncode == 4, result.stdout + result.stderr
    assert "nothing was linked" in result.stderr
    assert "still written" in result.stderr
    assert "--uninstall" in result.stderr
    assert BEGIN in layout.bashrc.read_text()
    assert not layout.shims()


def test_system_mode_verification_cannot_pass_on_an_inherited_PATH(tmp_path):
    """If the caller already has the shim dir on PATH, `command -v find`
    answers from it whether or not the hook file was read. A check that
    passes for a reason other than the one it is testing is worse than no
    check."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    env = sandbox_env(layout, layout.toolbin, PATH="%s:%s" % (layout.bin, layout.toolbin))
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, bash_ignores_bashrc=True,
                                 zsh_ignores_zshenv=True, env=env)
    assert result.returncode == 4, (
        "verification passed on an inherited PATH, so it proves nothing\n"
        + result.stdout + result.stderr)


def test_zsh_verification_cannot_pass_on_an_inherited_PATH(tmp_path):
    """The zsh-only isolation of the test above: bash's own hook is left
    healthy so it verifies normally, which means an exit 4 here can only
    come from verify_zsh_hook not being fooled by the caller's PATH."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    env = sandbox_env(layout, layout.toolbin, PATH="%s:%s" % (layout.bin, layout.toolbin))
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, zsh_ignores_zshenv=True, env=env)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "a non-interactive bash resolves" in result.stdout, (
        "bash's own hook should have verified fine, isolating the failure to zsh\n"
        + result.stdout + result.stderr)


def test_every_path_the_hook_blocks_advertise_exists(tmp_path):
    """The blocks go into files every user's shell reads, and they send the
    reader to a file under the prefix. The reference is read out of the
    generated block rather than written here, so moving the pointer fails
    this instead of going quiet."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stderr
    for hook in (layout.bashrc, layout.zshenv, layout.fishconf):
        text = hook.read_text()
        referred = [w.rstrip(".") for line in text.splitlines() if line.startswith("#")
                    for w in line.split() if w.startswith(str(layout.prefix))]
        assert referred, "the block in %s no longer points anywhere" % hook
        for path in referred:
            assert os.path.isfile(path), (
                "the block tells every user to read %s, which the install does not create" % path)
            assert os.stat(path).st_mode & 0o044, oct(os.stat(path).st_mode)


# --------------------------------------------------------------------------
# the zsh hook, mirroring the bash cases on the zsh file
# --------------------------------------------------------------------------

def test_system_mode_zsh_hook_verification_catches_a_hook_that_does_not_fire(tmp_path):
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 zsh_ignores_zshenv=True)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "FAILED to verify the zsh hook" in result.stderr
    assert BEGIN in layout.zshenv.read_text()
    assert "NOT rolled back" in result.stderr


def test_verify_hooks_reports_both_failures_in_one_run(tmp_path):
    """verify_hooks() does not short-circuit: a single failed install reports
    every broken required hook in the same run rather than one-fix-one-
    discover."""
    result, _l = run_install(tmp_path, ["--system"], fake_uid=0,
                             bash_ignores_bashrc=True, zsh_ignores_zshenv=True)
    assert result.returncode == 4, result.stdout + result.stderr
    assert result.stderr.count("FAILED to verify the") == 2, result.stderr


def test_a_symlinked_zshenv_file_is_refused(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("PRIVATE\n")
    secret.chmod(0o600)
    link = tmp_path / "zshenv-link"
    os.symlink(str(secret), str(link))
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], fake_uid=0, layout=Layout(tmp_path, zshenv=link))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "symlink" in result.stderr
    assert secret.read_text() == "PRIVATE\n"
    assert stat.S_IMODE(os.stat(str(secret)).st_mode) == 0o600


def test_zshenv_fifo_is_refused_on_uninstall(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(str(fifo))
    result, _l = run_install(tmp_path, ["--uninstall"], fake_uid=0,
                             layout=Layout(tmp_path, zshenv=fifo), timeout=30)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "regular file" in result.stderr


def test_uninstall_still_works_on_a_plain_zshenv(tmp_path):
    layout = Layout(tmp_path)
    layout.zshenv.write_text("# a stock zshenv\n")
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert BEGIN in layout.zshenv.read_text()
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "walk-blocker" not in layout.zshenv.read_text()


def test_bashrc_and_zshenv_blocks_are_not_accidentally_shared_content(tmp_path):
    """bashrc_block() and zshenv_block() stay separate functions with
    separate content, so a prose change meant for one shell cannot silently
    land on the other's file: each block carries the text specific to its
    own shell's mechanism, not the other's."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stdout + result.stderr
    bashrc_text = layout.bashrc.read_text()
    zshenv_text = layout.zshenv.read_text()
    assert "SSH_SOURCE_BASHRC" in bashrc_text and "SHLVL" in bashrc_text
    assert "zshenv UNCONDITIONALLY" in zshenv_text and "--no-rcs" in zshenv_text
    assert "--no-rcs" not in bashrc_text
    assert "SHLVL < 2" not in zshenv_text


# --------------------------------------------------------------------------
# the fish hook -- a drop-in, and best-effort in the fixture site
# --------------------------------------------------------------------------

def test_fish_hook_is_written_and_verifies_when_fish_is_present(tmp_path):
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == "", "fish verifying successfully must not print to stderr"
    assert "%s written (best-effort)" % layout.fishconf in result.stdout
    assert "a non-interactive fish resolves" in result.stdout
    assert "set -gx WALK_BLOCKER_SHIM_DIR" in layout.fishconf.read_text()


def test_fish_hook_is_skipped_silently_when_fish_is_absent(tmp_path):
    """The ordinary case on a node without fish: nothing is written, nothing
    is reported as broken -- as link_farm skips a name that does not
    resolve."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 fish_absent=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not layout.fishconf.exists()
    combined = result.stdout + result.stderr
    assert "fish PATH hook" not in combined
    assert "(best-effort)" not in combined


def test_fish_hook_failure_is_not_fatal_to_the_install(tmp_path):
    """A best-effort hook that does not fire is a warning, never a failed
    install (ADR-0008) -- and the file is still written either way."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 fish_ignores_conf=True)
    assert result.returncode == 0, "a fish verify failure must not fail the install\n" + result.stderr
    assert result.stdout.count("verified") == 2, "bash and zsh must still have succeeded"
    assert "FAILED to verify the fish hook" in result.stderr
    assert "best-effort, not fatal" in result.stderr
    assert layout.fishconf.exists()


def test_a_symlinked_fish_conf_file_is_refused(tmp_path):
    """Weaker than the bash refusal, deliberately: the writer never reads the
    file. The refusal is "something already at this path is not what the
    installer put there"."""
    secret = tmp_path / "secret"
    secret.write_text("PRIVATE\n")
    secret.chmod(0o600)
    link = tmp_path / "fish-conf-link.fish"
    os.symlink(str(secret), str(link))
    result, _l = run_install(tmp_path, ["--system"], fake_uid=0,
                             layout=Layout(tmp_path, fishconf=link))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "symlink" in result.stderr
    assert secret.read_text() == "PRIVATE\n"


def test_the_preview_predicts_the_fish_dropin_refusal(tmp_path):
    """Preview and execute stay in step for the drop-in too: a preview must
    not advertise a command the install then refuses."""
    link = tmp_path / "fish-conf-link.fish"
    os.symlink(str(tmp_path / "nowhere"), str(link))
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], layout=Layout(tmp_path, fishconf=link))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "will refuse" in result.stdout and "hooks.fish.file" in result.stdout


def test_fish_conf_directory_is_created_when_it_does_not_exist(tmp_path):
    """Whether fish's conf.d exists depends on whether fish is installed;
    write_fish_conf() creates it, intermediates included, at 0755."""
    layout = Layout(tmp_path, fishconf=tmp_path / "nofish" / "conf.d" / "walk-blocker.fish")
    assert not layout.fishconf.parent.exists()
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert layout.fishconf.exists()
    assert oct(layout.fishconf.parent.stat().st_mode & 0o777) == oct(0o755)
    assert oct(layout.fishconf.parent.parent.stat().st_mode & 0o777) == oct(0o755)


def test_uninstall_removes_the_fish_conf_file(tmp_path):
    """Plain removal, unconditional: it must not error when fish is no
    longer present to have written one."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert layout.fishconf.exists()
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout, fish_absent=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not layout.fishconf.exists()


# --------------------------------------------------------------------------
# gate classes come from config (ADR-0008)
# --------------------------------------------------------------------------

def test_a_required_zsh_that_cannot_verify_fails_the_install(tmp_path):
    result, _l = run_install(tmp_path, ["--system"], fake_uid=0,
                             zsh_ignores_zshenv=True, **{"hooks.zsh.gate": "required"})
    assert result.returncode == 4, result.stdout + result.stderr


def test_a_best_effort_zsh_that_cannot_verify_does_not_fail_the_install(tmp_path):
    """The same build with zsh reclassified: the hook is written, its failure
    is a warning, and the install reports success."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 zsh_ignores_zshenv=True, **{"hooks.zsh.gate": "best-effort"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert BEGIN in layout.zshenv.read_text(), "still written"
    assert "%s block installed (best-effort)" % layout.zshenv in result.stdout
    assert "FAILED to verify the zsh hook" in result.stderr
    assert "best-effort, not fatal" in result.stderr


def test_a_best_effort_shell_that_is_absent_is_neither_written_nor_reported(tmp_path):
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 zsh_absent=True, **{"hooks.zsh.gate": "best-effort"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not layout.zshenv.exists()
    assert "zsh" not in result.stderr
    relink, layout = run_install(tmp_path, ["--relink"], fake_uid=0, layout=layout,
                                 script=layout.script, zsh_absent=True)
    assert relink.returncode == 0, relink.stderr
    assert "zsh PATH hook" not in relink.stderr


def test_a_required_shell_whose_binary_is_absent_is_a_hard_failure(tmp_path):
    """An automatic pass on "absent" would silently convert "unchecked" into
    "verified"."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 zsh_absent=True)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "no zsh on PATH" in result.stderr
    assert BEGIN in layout.zshenv.read_text(), "written, then found unverifiable"


def test_a_required_fish_gates_the_install(tmp_path):
    """The class is the site's call, not the shell's: fish reclassified as
    required is written with the required hooks and its failure fails the
    install, with the same not-rolled-back report bash and zsh give."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 fish_ignores_conf=True, **{"hooks.fish.gate": "required"})
    assert result.returncode == 4, result.stdout + result.stderr
    assert "FAILED to verify the fish hook" in result.stderr
    assert "best-effort" not in result.stderr
    assert "NOT rolled back" in result.stderr
    assert layout.fishconf.exists()

    absent, _l = run_install(tmp_path / "absent", ["--system"], fake_uid=0,
                             fish_absent=True, **{"hooks.fish.gate": "required"})
    assert absent.returncode == 4, absent.stdout + absent.stderr
    assert "no fish on PATH" in absent.stderr


def test_a_disabled_hook_is_never_written_verified_or_reported(tmp_path):
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 zsh_ignores_zshenv=True, **{"hooks.zsh.enabled": False})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not layout.zshenv.exists()
    assert str(layout.zshenv) not in result.stdout + result.stderr
    relink, layout = run_install(tmp_path, ["--relink"], fake_uid=0, layout=layout,
                                 script=layout.script, **{"hooks.zsh.enabled": False})
    assert relink.returncode == 0, relink.stderr
    assert "zsh PATH hook" not in relink.stderr
    preview, _l = run_install(tmp_path, ["--system", "--dry-run"], layout=layout, **{"hooks.zsh.enabled": False})
    assert str(layout.zshenv) not in preview.stdout


def test_a_disabled_hook_is_left_alone_by_uninstall(tmp_path):
    """An uninstall removes what this build could have written; a file a
    disabled hook names is not that, and its contents stay untouched."""
    layout = Layout(tmp_path)
    layout.zshenv.write_text("# not ours\n")
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout,
                                 **{"hooks.zsh.enabled": False})
    assert result.returncode == 0, result.stderr
    assert layout.zshenv.read_text() == "# not ours\n"


def test_the_preview_lists_each_class_by_file(tmp_path):
    result, layout = run_install(tmp_path, ["--system", "--dry-run"], **{"hooks.zsh.gate": "best-effort"})
    assert result.returncode == 0, result.stderr
    out = result.stdout
    required_at = out.index("REQUIRED hook file")
    best_at = out.index("BEST-EFFORT hook")
    assert required_at < out.index("bash: %s" % layout.bashrc) < best_at
    assert best_at < out.index("zsh: %s" % layout.zshenv)
    assert best_at < out.index("fish: %s" % layout.fishconf)


# --------------------------------------------------------------------------
# hook-file refusals, on the install and the uninstall path alike
# --------------------------------------------------------------------------

def test_a_symlinked_bashrc_file_is_refused(tmp_path):
    """prepend_block reads this file and rewrites it 0644, so a symlink here
    republishes its target to every user."""
    secret = tmp_path / "secret"
    secret.write_text("PRIVATE\n")
    secret.chmod(0o600)
    link = tmp_path / "bashrc-link"
    os.symlink(str(secret), str(link))
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], fake_uid=0, layout=Layout(tmp_path, bashrc=link))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "symlink" in result.stderr
    assert secret.read_text() == "PRIVATE\n"
    assert stat.S_IMODE(os.stat(str(secret)).st_mode) == 0o600


def test_a_dangling_bashrc_symlink_is_refused(tmp_path):
    """-L is tested before -e: a dangling link fails -e, and prepend_block
    would then create a regular file at the link's target."""
    link = tmp_path / "dangling"
    os.symlink(str(tmp_path / "nowhere"), str(link))
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], fake_uid=0, layout=Layout(tmp_path, bashrc=link))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "symlink" in result.stderr


def test_a_regular_bashrc_file_is_still_accepted(tmp_path):
    layout = Layout(tmp_path)
    layout.bashrc.write_text("# existing\n")
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert BEGIN in layout.bashrc.read_text()


SOURCE_THE_BLOCK = """\
WALK_BLOCKER_AUDIT=
. "%s"
printf '%%s' "$WALK_BLOCKER_AUDIT" > "%s"
"""


def rewrite_stamped_line(script, name, sh_literal):
    """Replace the value on `name`'s stamped line in a stamped COPY with a
    hand-written sh literal, for a value the build itself refuses. The
    marker comment is dropped from that line so the copy is honest about no
    longer being what the build would emit."""
    text = script.read_text()
    marker = "\n%s=" % name
    assert marker in text, "no %s= line to rewrite" % name
    start = text.index(marker) + 1
    end = text.index("\n", start)
    text = "%s%s=%s%s" % (text[:start], name, sh_literal, text[end:])
    script.write_text(text)


def test_the_generated_block_restores_an_awkward_audit_path_verbatim(tmp_path):
    """`shquote` is the boundary that stops an audit path from ending its own
    assignment. The build refuses a quote in a stamped value, so the value is
    written into the stamped copy by hand, carrying an apostrophe AND the
    metacharacters that once made this a command execution in every login
    shell -- and the assertion is on what a sourcing shell actually ends up
    with, not on the presence of an assignment line."""
    canary = tmp_path / "pwn"
    awkward = "it'sa;id>%s `whoami` $(id) \"q\" a|b&c.jsonl" % canary
    layout = stamped_install(tmp_path)
    rewrite_stamped_line(layout.script, "SG_AUDIT_FILENAME", "'%s'" % awkward.replace("'", "'\\''"))
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, script=layout.script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WALK_BLOCKER_AUDIT=" in layout.bashrc.read_text()

    got = tmp_path / "got"
    probe = tmp_path / "src.sh"
    probe.write_text(SOURCE_THE_BLOCK % (layout.bashrc, got))
    run = subprocess.run([SH, str(probe)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert run.returncode == 0, run.stderr
    assert got.read_text() == "%s/%s" % (layout.spool, awkward), "value did not survive sourcing intact"
    assert not canary.exists(), "the `id>...` half executed"


def test_an_audit_path_with_a_newline_is_refused_not_truncated(tmp_path):
    """`$()` strips trailing newlines, so a value ending in one would be
    silently changed and the shim would write its records somewhere other
    than configured. Refused at build by `sh_literal`, and -- for a
    hand-edited copy -- refused again by install.sh."""
    with pytest.raises(stamp.StampError):
        H.stamp_install_text(site_values(Layout(tmp_path), **{"install.audit_filename": "a\n"}))
    layout = stamped_install(tmp_path)
    rewrite_stamped_line(layout.script, "SG_AUDIT_FILENAME", "'a.jsonl\n'")
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], layout=layout, script=layout.script)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "newline" in result.stderr


def test_a_prefix_with_a_newline_is_refused(tmp_path):
    """The prefix reaches the block as $BIN, so it has the same problem."""
    layout = stamped_install(tmp_path)
    rewrite_stamped_line(layout.script, "DEFAULT_PREFIX", "'%s\n'" % layout.prefix)
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], layout=layout, script=layout.script)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "newline" in result.stderr


def test_ordinary_paths_are_unaffected_by_the_newline_check(tmp_path):
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (layout.bin / "find").is_symlink()


def test_an_approved_install_is_refused_from_a_checkout(tmp_path):
    """`link_farm` points every shim at "$HERE/guard.sh", so an approved
    install run out of an ordinary user's tree puts that user's file on
    every other user's PATH. Being root at the time is what makes it
    effective, not what prevents it."""
    layout = stamped_install(tmp_path, dest=tmp_path / "someones-checkout")
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, script=layout.script)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "deployed copy" in result.stderr
    assert "deploy.py" in result.stderr
    assert not layout.bin.exists()


def test_an_approved_install_is_allowed_from_the_deployed_copy(tmp_path):
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stdout + result.stderr
    shim = layout.bin / "find"
    assert shim.is_symlink()
    assert os.path.realpath(str(shim)).startswith(str(layout.shim_dir))


def test_the_preview_is_still_allowed_from_a_checkout(tmp_path):
    """Preview prints and installs nothing, so it has no location to get
    wrong -- and refusing it would make the guard undiscoverable."""
    layout = stamped_install(tmp_path, dest=tmp_path / "someones-checkout")
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], layout=layout, script=layout.script)
    assert result.returncode == 0, result.stdout + result.stderr


def test_uninstall_refuses_a_symlinked_bashrc_file(tmp_path):
    """`strip_block()` reads this file and writes it back as a 0644 regular
    file, exactly as the install path does."""
    secret = tmp_path / "secret"
    secret.write_text("PRIVATE\n")
    secret.chmod(0o600)
    link = tmp_path / "bashrc-link"
    os.symlink(str(secret), str(link))
    result, _l = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=Layout(tmp_path, bashrc=link))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "symlink" in result.stderr
    assert secret.read_text() == "PRIVATE\n"
    assert stat.S_IMODE(os.stat(str(secret)).st_mode) == 0o600


def test_uninstall_refuses_a_bashrc_that_is_not_a_regular_file(tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(str(fifo))
    result, _l = run_install(tmp_path, ["--uninstall"], fake_uid=0,
                             layout=Layout(tmp_path, bashrc=fifo), timeout=30)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "regular file" in result.stderr


def test_uninstall_still_works_on_a_plain_bashrc(tmp_path):
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert BEGIN in layout.bashrc.read_text()
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "walk-blocker" not in layout.bashrc.read_text()


# --------------------------------------------------------------------------
# the trust chain
# --------------------------------------------------------------------------

def test_an_approved_install_is_refused_when_the_payload_is_not_root_owned(tmp_path):
    """The location check ($HERE == $PREFIX/shim) does not establish that the
    payload came from deploy.py: copying shim/ into any user-writable
    directory laid out the same way makes the two paths match. With the real
    stat, the walk sees the tmp tree's true ownership."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 real_stat=True)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not root" in result.stderr
    assert "deploy.py" in result.stderr
    assert not layout.bin.exists()


def test_an_approved_install_is_refused_on_a_world_writable_ancestor(tmp_path):
    """Root-owned is not enough if someone else can replace what is under it."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 stat_body="#!/bin/sh\nprintf '0 777\\n'\n")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "mode 777" in result.stderr
    assert not layout.bin.exists()


def test_an_approved_install_is_refused_when_a_payload_file_is_not_root_owned(tmp_path):
    """A trusted directory chain does not make its CONTENTS trusted: a
    root-owned 0755 shim/ can hold a user-owned guard.sh, and every shim
    would then point at an inode its owner can still rewrite."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 stat_body=stat_stub_uid_for("*/guard.sh", "1000 755"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "guard.sh is owned by" in result.stderr
    assert "uid 1000" in result.stderr
    assert not layout.bin.exists()


def test_an_approved_install_is_refused_when_a_payload_file_is_a_symlink(tmp_path):
    """A symlink's target sits outside everything the ancestor walk just
    proved, so it is refused rather than followed."""
    layout = stamped_install(tmp_path)
    real_guard = tmp_path / "elsewhere-guard.sh"
    real_guard.write_text("#!/bin/sh\nexit 0\n")
    (layout.shim_dir / "guard.sh").unlink()
    os.symlink(str(real_guard), str(layout.shim_dir / "guard.sh"))
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, script=layout.script)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "is a symlink" in result.stderr
    assert not layout.bin.exists()


def test_an_approved_install_refuses_a_bashrc_in_a_writable_directory(tmp_path):
    """Validating only the final component leaves a race: in a writable
    directory another user could replace it as a symlink between the check
    and the write. The real stat sees the tmp tree's true ownership."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, real_stat=True)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not root" in result.stderr
    assert layout.bashrc.read_text() == STOCK_BASHRC
    assert not layout.bin.exists()


def test_the_preview_notes_a_writable_directory_but_does_not_refuse(tmp_path):
    """A preview writes nothing, so there is no race to lose -- but staying
    silent would advertise a command the install then refuses."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], layout=layout, real_stat=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "will refuse" in result.stdout, result.stdout
    assert "not root" in result.stdout


def test_a_trusted_hook_directory_passes_the_chain_check(tmp_path):
    """The inverse, and the reason the check costs nothing in production:
    the system directories the stamped paths sit under are root-owned 0755,
    which is exactly what the stand-in reports."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert BEGIN in layout.bashrc.read_text()


def test_an_approved_install_refuses_a_bashrc_owned_by_someone_else(tmp_path):
    """verify_bash_hook has bash source this file AS ROOT to prove the hook
    fires, and prepend_block preserves whatever is already in it -- so its
    owner chooses what runs during the deploy."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, stat_body=stat_stub_uid_for(str(layout.bashrc), "1000 644"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "uid 1000" in result.stderr
    assert "sourced as root to verify the hook fires" in result.stderr
    assert layout.bashrc.read_text() == STOCK_BASHRC
    assert not layout.bin.exists()


def test_an_approved_install_refuses_a_group_writable_bashrc(tmp_path):
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, stat_body=stat_stub_uid_for(str(layout.bashrc), "0 664"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "mode 664" in result.stderr
    assert not layout.bin.exists()


def test_uninstall_refuses_a_bashrc_owned_by_someone_else(tmp_path):
    """`--uninstall` never sets APPROVED; keying these checks off APPROVED
    once skipped the leaf ownership check on the teardown path, which then
    rewrote the file as root anyway. The flag answers "is this run about to
    write the file", which is the question that matters."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout,
                                 stat_body=stat_stub_uid_for(str(layout.bashrc), "1000 644"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "uid 1000" in result.stderr
    assert layout.bashrc.read_text() == STOCK_BASHRC


def test_uninstall_refuses_an_untrusted_bashrc_ancestor(tmp_path):
    """The ancestor check is fatal on the teardown too, not advisory: it
    rewrites the file, so the swap race applies."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout, real_stat=True)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not root" in result.stderr
    assert layout.bashrc.read_text() == STOCK_BASHRC


def test_a_symlink_in_the_trusted_chain_is_refused(tmp_path):
    """`stat` follows a directory symlink but `dirname` does not, so a walk
    that resolved would check a root-owned TARGET and then continue up the
    LEXICAL parent, never visiting the target's real parent. The stand-in
    reports root/0755 for everything, so only the symlink check can fail."""
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    link = tmp_path / "link"
    os.symlink(str(real_dir), str(link))
    layout = Layout(tmp_path, bashrc=link / "bashrc")
    layout.bashrc.write_text(STOCK_BASHRC)
    result, _l = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "is a symlink" in result.stderr
    assert "cannot be walked" in result.stderr


def test_wrapped_names_is_not_sourced_before_it_is_checked(tmp_path):
    """The systemd unit runs this script AS ROOT on every poll, so a swapped
    or user-owned wrapped_names.sh sourced ahead of the checks would get root
    code execution before anything could reject it. The staged copy is
    rigged to touch a canary; the assertion is that it does not appear."""
    layout = stamped_install(tmp_path)
    canary = tmp_path / "sourced-canary"
    (layout.shim_dir / "wrapped_names.sh").write_text(
        "SG_WRAPPED_NAMES='find grep du'\n: > '%s'\n" % canary)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, script=layout.script,
                                 stat_body=stat_stub_uid_for("*/wrapped_names.sh", "1000 644"))
    assert result.returncode == 3, result.stdout + result.stderr
    assert not canary.exists(), "wrapped_names.sh executed before it was rejected"


def test_wrapped_names_is_still_sourced_when_it_is_trusted(tmp_path):
    """The inverse: deferring the source must not stop it happening, or the
    shim farm is built from an empty name list."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (layout.bin / "find").is_symlink()


def test_a_root_relink_from_a_checkout_is_refused(tmp_path):
    """`--relink` is what the unit runs AS ROOT. Run as root out of a
    checkout, link_farm would repoint every shim at that checkout's guard.sh
    -- handing its owner the code every user's shell executes."""
    layout = stamped_install(tmp_path, dest=tmp_path / "someones-checkout")
    layout.bin.mkdir(parents=True)
    result, layout = run_install(tmp_path, ["--relink"], fake_uid=0, layout=layout, script=layout.script)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "--relink" in result.stderr
    assert list(layout.bin.iterdir()) == []


def test_a_root_relink_from_the_deployed_copy_is_allowed(tmp_path):
    """The inverse, and the case the timer actually runs."""
    layout = stamped_install(tmp_path)
    result, layout = run_install(tmp_path, ["--relink"], fake_uid=0, layout=layout, script=layout.script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (layout.bin / "find").is_symlink()
    assert os.path.realpath(str(layout.bin / "find")).startswith(str(layout.shim_dir))


def test_an_unprivileged_relink_from_a_checkout_still_works(tmp_path):
    """The test and debug path. A non-root run cannot write $PREFIX/bin on a
    real install anyway, so gating it would cost the workflow and buy
    nothing."""
    layout = stamped_install(tmp_path, dest=tmp_path / "someones-checkout")
    result, layout = run_install(tmp_path, ["--relink"], layout=layout, script=layout.script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (layout.bin / "find").is_symlink()


def test_the_preview_advertises_a_teardown_that_actually_runs(tmp_path):
    """A preview must not advertise what the tool refuses: the teardown it
    offers names the DEPLOYED copy, never $HERE."""
    result, layout = run_install(tmp_path, ["--system", "--dry-run"])
    assert result.returncode == 0, result.stdout + result.stderr
    offered = [ln for ln in result.stdout.splitlines()
               if ln.lstrip("# ").strip().startswith("sh ")
               and "install.sh" in ln and "--uninstall" in ln]
    assert offered, result.stdout
    for line in offered:
        assert str(layout.shim_dir) in line, line
        assert "$HERE" not in line, line


def test_the_advertised_teardown_runs_from_the_deployed_copy(tmp_path):
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout, script=layout.script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "walk-blocker" not in layout.bashrc.read_text()


def test_a_fifo_payload_entry_is_refused(tmp_path):
    """`stat -c '%u %a'` answers happily for a FIFO, so refusing symlinks and
    then reading uid and mode would let every other inode type through. The
    timeout is what makes the liveness half of this claim real."""
    layout = stamped_install(tmp_path)
    (layout.shim_dir / "guard.sh").unlink()
    os.mkfifo(str(layout.shim_dir / "guard.sh"))
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0,
                                 layout=layout, script=layout.script, timeout=30)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not a" in result.stderr and "regular file" in result.stderr
    assert not layout.bin.exists()


def test_a_fifo_wrapped_names_is_refused_without_hanging(tmp_path):
    """Sourcing a fifo as root blocks forever, so this is a liveness property
    as much as a security one: the refusal has to come before the `.`."""
    layout = stamped_install(tmp_path)
    (layout.shim_dir / "wrapped_names.sh").unlink()
    os.mkfifo(str(layout.shim_dir / "wrapped_names.sh"))
    result, _l = run_install(tmp_path, ["--relink"], fake_uid=0, layout=layout,
                             script=layout.script, timeout=30)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "regular file" in result.stderr


def test_a_root_uninstall_validates_the_directory_it_sources_from(tmp_path):
    """load_wrapped_names() once checked only the file. If $HERE is writable
    by someone else, its owner can replace that root-owned file between the
    stat and the `.` that follows. Standalone `--uninstall` does not pass
    through require_deployed_copy(), so the check belongs in the function."""
    layout = stamped_install(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, layout = run_install(tmp_path, ["--uninstall"], fake_uid=0, layout=layout,
                                 script=layout.script, real_stat=True)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not root" in result.stderr
    assert layout.bashrc.read_text() == STOCK_BASHRC


def test_a_relative_path_does_not_hang_the_trusted_chain_walk(tmp_path):
    """`dirname` on a relative path eventually returns `.`, and `dirname .`
    is `.` forever -- so an unanchored walk's only exit, reaching `/`, would
    never be taken. The schema admits only absolute paths, so the shape
    arrives from a hand-edited copy; the mechanism still has to terminate.
    The timeout is the assertion."""
    work = tmp_path / "work"
    layout = Layout(tmp_path, prefix=work, bashrc="bashrc")
    work.mkdir()
    (work / "bashrc").write_text(STOCK_BASHRC)
    result, _l = run_install(tmp_path, ["--system"], fake_uid=0,
                             layout=layout, cwd=str(work), timeout=30)
    assert result.returncode is not None


def test_a_relative_path_is_anchored_so_its_real_ancestors_are_checked(tmp_path):
    """Anchored rather than refused, because a relative path's real ancestors
    are exactly what needs checking: `bashrc` in a user-owned working
    directory must still be refused."""
    work = tmp_path / "work"
    layout = Layout(tmp_path, prefix=work, bashrc="bashrc")
    work.mkdir()
    (work / "bashrc").write_text(STOCK_BASHRC)
    result, _l = run_install(tmp_path, ["--system"], fake_uid=0,
                             layout=layout, cwd=str(work), timeout=30, real_stat=True)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not root" in result.stderr
    assert (work / "bashrc").read_text() == STOCK_BASHRC


def test_the_preview_notes_a_bashrc_it_will_refuse(tmp_path):
    """A user-owned hook file must not give a CLEAN preview plus an
    advertised command that fails the instant approval is supplied."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], layout=layout,
                             stat_body=stat_stub_uid_for(str(layout.bashrc), "1000 644"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "will refuse" in result.stdout, result.stdout
    assert "uid 1000" in result.stdout
    assert "hooks.bash.file" in result.stdout, "the note names the key an operator would change"


def test_a_clean_bashrc_gets_no_preview_note(tmp_path):
    """The inverse: the note must not appear for an ordinary file, or it is
    noise the operator learns to skip past."""
    layout = Layout(tmp_path)
    layout.bashrc.write_text(STOCK_BASHRC)
    result, _l = run_install(tmp_path, ["--system", "--dry-run"], layout=layout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "will refuse" not in result.stdout


# --------------------------------------------------------------------------
# the audit directory (ADR-0025)
# --------------------------------------------------------------------------

def test_the_system_install_creates_the_audit_dir_for_its_reader_group(tmp_path):
    """root:<spool_group> 02750 (ADR-0025). ADR-0004's invariant is that a
    MONITORED ACCOUNT CANNOT WRITE the audit trail, and no group or other
    `w` still carries it; what changed is that one group reads and nobody
    else does. Mutation: drop the chgrp or the chmod in spool_assert_here()."""
    result, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert result.returncode == 0, result.stderr
    info = layout.spool.stat()
    mode = info.st_mode & 0o7777
    assert mode == 0o2750, oct(mode)
    assert not mode & 0o022, "never group/other writable"
    assert info.st_gid == os.getgid(), "the reader group is the stamped one"
    assert layout.audit.parent == layout.spool
    assert layout.audit.name == H.AUDIT_FILENAME


def test_the_relink_puts_the_audit_dir_mode_back_every_poll(tmp_path):
    """The hooks beside this are REPORTED and never repaired, because their
    files are the distribution's. This directory is ours, so a chmod that
    blinds the trail is corrected on the next poll."""
    installed, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert installed.returncode == 0, installed.stderr
    assert (layout.spool.stat().st_mode & 0o7777) == 0o2750
    layout.spool.chmod(0o00750)
    result, layout = run_install(tmp_path, ["--relink"], fake_uid=0, layout=layout, script=layout.script)
    assert result.returncode == 0, result.stderr
    mode = layout.spool.stat().st_mode & 0o7777
    assert mode == 0o2750, "the relink must put its own directory back, not merely notice: %s" % oct(mode)


def test_a_relink_that_cannot_fix_the_audit_dir_still_relinks(tmp_path):
    """Non-fatal by construction: a directory this cannot repair must not
    take the relink -- and through ExecStartPre, the reaper -- down."""
    installed, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert installed.returncode == 0, installed.stderr
    shutil.rmtree(str(layout.spool))
    layout.spool.write_text("not a directory\n")   # `install -d` cannot win this
    result, layout = run_install(tmp_path, ["--relink"], fake_uid=0, layout=layout, script=layout.script)
    assert result.returncode == 0, "a failed audit-dir repair must not fail the relink: %s" % result.stderr


def test_a_blocked_audit_dir_reaches_the_journal_by_name(tmp_path):
    """The correction is not the whole point; the RECORD is. A relink that
    silently put the mode back would lose the fact that somebody tried."""
    installed, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert installed.returncode == 0, installed.stderr
    healthy, records = relink_with_a_recording_logger(tmp_path, layout, fake_uid=0, script=layout.script)
    assert healthy.returncode == 0, healthy.stderr
    assert not [r for r in records if r["action"] == "audit_dir" and r["state"] == "mode-corrected"], records

    layout.spool.chmod(0o755)
    result, records = relink_with_a_recording_logger(tmp_path, layout, fake_uid=0, script=layout.script)
    assert result.returncode == 0, result.stderr
    assert (layout.spool.stat().st_mode & 0o7777) == 0o2750
    assert [r for r in records if r["action"] == "audit_dir" and r["state"] == "mode-corrected"], (
        "the correction must reach journalctl -t walk-blocker: %s" % records)


def test_a_first_time_audit_dir_is_recorded_as_created_not_corrected(tmp_path):
    """`created` and `mode-corrected` are different events: one is a deploy,
    the other is somebody having changed it since."""
    installed, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert installed.returncode == 0, installed.stderr
    shutil.rmtree(str(layout.spool))
    result, records = relink_with_a_recording_logger(tmp_path, layout, fake_uid=0, script=layout.script)
    assert result.returncode == 0, result.stderr
    states = [r["state"] for r in records if r["action"] == "audit_dir"]
    assert "created" in states, records
    assert "mode-corrected" not in states, records


def test_the_stub_shells_and_sink_are_what_the_harness_says(tmp_path):
    """The harness's own load-bearing claims, pinned: the stub bash really
    gates on SHLVL and SSH_CLIENT like the real one, and the neutralized sink
    really is unreachable, so every "no record" assertion above means what
    it says."""
    fake_bin = populate_bin(tmp_path / "bin", tools=())
    hook = tmp_path / "hook"
    hook.write_text("PATH=/hooked:$PATH\n")
    env = {"PATH": str(fake_bin), "WALK_BLOCKER_TEST_BASHRC": str(hook)}
    fired = subprocess.run([str(fake_bin / "bash"), "-c", "printf %s \"$PATH\""],
                           capture_output=True, text=True,
                           env=dict(env, SHLVL="0", SSH_CLIENT="x")).stdout
    assert fired.startswith("/hooked:")
    nested = subprocess.run([str(fake_bin / "bash"), "-c", "printf %s \"$PATH\""],
                            capture_output=True, text=True,
                            env=dict(env, SHLVL="2", SSH_CLIENT="x")).stdout
    assert not nested.startswith("/hooked:")
    assert not os.path.exists(NO_LOGGER)
    assert shutil.which(TEST_LOGGER) is None
    assert BASH_STUB_SOURCES_HOOK != BASH_STUB_IGNORES_HOOK
    assert ZSH_STUB_IGNORES_HOOK
    assert STAT_ROOT_755.strip().endswith("printf '0 755\\n'")
    assert FIXTURE_POLICY.mounts["/opt/site-tools"] == ("cheap", None)
    stage_walk_job(tmp_path / "p")
    assert (tmp_path / "p" / "walk-job").exists()


def test_the_next_copy_dir_is_fresh_after_every_install(tmp_path):
    """Each unapproved run stands in for its own checkout; a directory reused
    across runs would let one run's stamped copy answer for another's."""
    layout = Layout(tmp_path)
    first = H.next_copy_dir(layout)
    stamped_install(tmp_path, dest=first, layout=layout)
    second = H.next_copy_dir(layout)
    assert second != first and not second.exists(), (first, second)


# --------------------------------------------------------------------------
# the mount report is on change, not on state (ADR-0019)
# --------------------------------------------------------------------------

STATE_NAME = "uncovered-mounts.state"


def _stateful_layout(tmp_path, **kw):
    """A layout whose spool exists, as the root relink's does after
    assert_audit_dir(): the report can remember what it said."""
    layout = Layout(tmp_path, **kw)
    layout.spool.mkdir(parents=True, exist_ok=True)
    return layout


def test_an_uncovered_mount_is_reported_once_not_every_poll(tmp_path):
    """The first relink names `/archive`; the second, with nothing changed,
    says nothing about mounts at all -- and the memory of what was said is a
    root-readable file in the spool, first line the boot it was written
    under."""
    layout = _stateful_layout(tmp_path)
    first, records = relink_with_a_recording_logger(tmp_path, layout)
    assert first.returncode == 0, first.stderr
    assert ("/archive", "nfs4", "expensive") in uncovered(records), records
    second, records = relink_with_a_recording_logger(tmp_path, layout)
    assert second.returncode == 0, second.stderr
    assert uncovered(records) == [], "a steady state must be silent: %s" % records
    lines = (layout.spool / STATE_NAME).read_text().splitlines()
    assert lines[0].startswith("boot "), lines
    assert "/archive nfs4" in lines[1:], lines


def test_a_mount_that_appears_uncovered_is_reported_when_it_appears(tmp_path):
    """A new remote export in the live table, and only it, is named on the
    poll it first appears -- ADR-0016's promise that a new resource is
    visible the moment it is guarded, kept without the repetition."""
    layout = _stateful_layout(tmp_path)
    relink_with_a_recording_logger(tmp_path, layout)
    layout.mount_table.write_text(FIXTURE_MOUNTS + "nas:/new /mnt/new nfs4 rw 0 0\n")
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 0, result.stderr
    assert uncovered(records) == [("/mnt/new", "nfs4", "expensive")], records


def test_a_mount_that_leaves_the_table_is_reported_unmounted_once(tmp_path):
    """The earlier line is answered: one `unmounted` record on the poll the
    mount is gone, then silence."""
    layout = _stateful_layout(tmp_path)
    layout.mount_table.write_text(FIXTURE_MOUNTS + "nas:/new /mnt/new nfs4 rw 0 0\n")
    relink_with_a_recording_logger(tmp_path, layout)
    layout.mount_table.write_text(FIXTURE_MOUNTS)
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 0, result.stderr
    assert uncovered(records) == [("/mnt/new", "nfs4", "unmounted")], records
    _result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert uncovered(records) == [], records


def test_a_mount_newly_covered_by_an_override_is_reported_covered(tmp_path):
    """A rebuild that adds a `[[filesystems.mounts]]` row for `/archive` is
    what the earlier line asked for; the next relink says `covered`, once,
    with the mount still in the table."""
    layout = _stateful_layout(tmp_path)
    _result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert ("/archive", "nfs4", "expensive") in uncovered(records), records

    covered = H.make_policy(mounts=dict(FIXTURE_POLICY.mounts, **{"/archive": ("expensive", 3)}))
    layout2 = Layout(tmp_path / "covered", spool=layout.spool, mount_table=layout.mount_table)
    stamped_install(tmp_path, dest=layout2.tmp_path / "copy", layout=layout2, policy=covered,
                    **{"derived.mount_overrides": H.render_shim_module.mount_overrides(covered)})
    result, records = relink_with_a_recording_logger(tmp_path, layout2, script=layout2.script)
    assert result.returncode == 0, result.stderr
    assert uncovered(records) == [("/archive", "nfs4", "covered")], records


def test_a_new_boot_reports_every_uncovered_mount_once_more(tmp_path):
    """A journal on volatile storage forgot the earlier line with the
    reboot; the memory's boot line is what makes the next relink say it
    again -- once, and nothing is called covered or unmounted against a
    table that changed for the reboot's own reasons."""
    layout = _stateful_layout(tmp_path)
    layout.mount_table.write_text(FIXTURE_MOUNTS + "nas:/new /mnt/new nfs4 rw 0 0\n")
    relink_with_a_recording_logger(tmp_path, layout)
    state = layout.spool / STATE_NAME
    lines = state.read_text().splitlines()
    lines[0] = "boot an-earlier-boot"
    state.write_text("\n".join(lines) + "\n")
    layout.mount_table.write_text(FIXTURE_MOUNTS)     # /mnt/new went with the reboot
    result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert result.returncode == 0, result.stderr
    got = uncovered(records)
    assert ("/archive", "nfs4", "expensive") in got, got
    assert not [r for r in got if r[2] != "expensive"], got
    _result, records = relink_with_a_recording_logger(tmp_path, layout)
    assert uncovered(records) == [], records


def test_without_a_writable_spool_the_report_repeats_every_poll(tmp_path):
    """No memory, no dedup: the unprivileged debug relink, or a spool not yet
    created, reports the whole set each time rather than nothing."""
    layout = Layout(tmp_path)
    assert not layout.spool.exists()
    for _ in range(2):
        result, records = relink_with_a_recording_logger(tmp_path, layout)
        assert result.returncode == 0, result.stderr
        assert ("/archive", "nfs4", "expensive") in uncovered(records), records
    assert not (layout.spool / STATE_NAME).exists()


def test_the_install_forgets_what_an_earlier_install_reported(tmp_path):
    """The first poll after a fresh install names every mount on its default
    once; the audit trail beside the memory is left alone."""
    layout = _stateful_layout(tmp_path)
    state = layout.spool / STATE_NAME
    state.write_text("boot x\n/archive nfs4\n")
    layout.audit.write_text("")
    result, layout = run_install(tmp_path, ["--system"],
                                 fake_uid=0, layout=layout)
    assert result.returncode == 0, result.stderr
    assert not state.exists(), "the install kept an earlier install's memory"
    assert layout.audit.exists()


def test_the_uninstall_removes_the_memory_and_keeps_the_spool(tmp_path):
    layout = _stateful_layout(tmp_path)
    layout.bin.mkdir(parents=True)
    state = layout.spool / STATE_NAME
    state.write_text("boot x\n/archive nfs4\n")
    layout.audit.write_text("")
    result, layout = run_install(tmp_path, ["--uninstall"], layout=layout, fake_uid=0)
    assert result.returncode == 0, result.stderr
    assert not state.exists()
    assert layout.spool.is_dir() and layout.audit.exists()
    assert "left in place" in result.stdout


def test_the_memory_is_readable_like_the_spool_whatever_the_umask(tmp_path):
    """ADR-0019 says the memory is readable like the rest of the spool, which
    is 0640 for the reader group (ADR-0025); a root relink under a 077 umask
    would otherwise leave it 0600 and make that sentence true only by the
    accident of a 022 default."""
    layout = _stateful_layout(tmp_path)
    before = os.umask(0o077)
    try:
        result, _records = relink_with_a_recording_logger(tmp_path, layout)
    finally:
        os.umask(before)
    assert result.returncode == 0, result.stderr
    mode = stat.S_IMODE((layout.spool / STATE_NAME).stat().st_mode)
    assert mode == 0o640, oct(mode)


# --------------------------------------------------------------------------
# the spool is pinned before anything is written into it (ADR-0025)
# --------------------------------------------------------------------------

def _installed(tmp_path):
    installed, layout = run_install(tmp_path, ["--system"], fake_uid=0)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return layout


def _states(records):
    return [r["state"] for r in records if r["action"] == "audit_dir"]


@pytest.mark.parametrize("shell", ["dash", "bash"])
def test_the_relink_puts_a_0755_spool_back_to_2750_with_its_group(tmp_path, shell):
    """The migration a node goes through at its next poll: an old spool at
    0755 with some other group comes back root:<spool_group> 02750, and each
    correction is journalled under its own word. Under both shells.

    The wrong group is MODELLED -- a non-root test has no second group to
    chgrp to -- by a stat that reports STALE_GID until the real chgrp has
    run. Mutations: drop the chgrp line, or the `group-corrected` report, and
    the group assertion or the record assertion fails."""
    if not shutil.which(shell):
        pytest.skip("%s is not installed" % shell)
    layout = _installed(tmp_path)
    layout.spool.chmod(0o00755)
    flag = tmp_path / "chgrp-ran"
    result, records = relink_with_a_recording_logger(
        tmp_path, layout, fake_uid=0, script=layout.script,
        shell=shutil.which(shell), chgrp_flag=flag,
        stat_body=H.stat_stub(stale_gid_until=flag))
    assert result.returncode == 0, result.stderr
    info = layout.spool.stat()
    assert stat.S_IMODE(info.st_mode) == 0o2750, oct(info.st_mode)
    assert info.st_gid == os.getgid()
    assert flag.exists(), "the relink never ran chgrp"
    states = _states(records)
    assert "mode-corrected" in states, records
    assert "group-corrected" in states, records


def test_a_spool_whose_group_really_differs_is_chgrped_back(tmp_path):
    """The same correction against a REAL group change, where the test user
    has a second group to make it with; skipped, visibly, where not."""
    others = [g for g in os.getgroups() if g != os.getgid()]
    if not others:
        pytest.skip("the test user has no supplementary group to chgrp to")
    layout = _installed(tmp_path)
    os.chown(str(layout.spool), -1, others[0])
    result, records = relink_with_a_recording_logger(
        tmp_path, layout, fake_uid=0, script=layout.script)
    assert result.returncode == 0, result.stderr
    assert layout.spool.stat().st_gid == os.getgid()
    assert "group-corrected" in _states(records), records


def test_a_reader_group_that_does_not_resolve_still_relinks(tmp_path):
    """Measured: `chgrp` to a name NSS does not know exits 1 with "invalid
    group" under dash and bash alike. The relink journals it and carries on
    -- through ExecStartPre, a relink that stopped here would stop nothing,
    but a Layer 1 reconcile that failed over its own directory's group would
    still be a reconcile that did not happen."""
    layout = _installed(tmp_path)
    stamped_install(tmp_path, dest=layout.shim_dir, layout=layout,
                    **{"install.spool_group": "no-such-group-wbtest"})
    result, records = relink_with_a_recording_logger(
        tmp_path, layout, fake_uid=0, script=layout.script)
    assert result.returncode == 0, result.stderr
    assert "group-failed" in _states(records), records
    assert layout.shims() == {"find", "grep"}


def test_a_spool_owned_by_someone_else_gets_no_write_and_no_chmod(tmp_path):
    """`owner-not-root`: reported, and nothing else. No mode change, no
    state file -- the memory is written only into a spool that is ours.
    Mutation: write the state through the path again, ungated, and the
    state file appears."""
    layout = _installed(tmp_path)
    layout.spool.chmod(0o00755)
    result, records = relink_with_a_recording_logger(
        tmp_path, layout, fake_uid=0, script=layout.script,
        stat_body=H.stat_stub(spool_owner=4242))
    assert result.returncode == 0, result.stderr
    assert "owner-not-root" in _states(records), records
    assert stat.S_IMODE(layout.spool.stat().st_mode) == 0o755, "chmod'd a spool that is not ours"
    assert not (layout.spool / STATE_NAME).exists(), "wrote into a spool that is not ours"
    assert not (layout.spool / (STATE_NAME + ".new")).exists()
    assert ("/archive", "nfs4", "expensive") in uncovered(records), (
        "no memory is not no report: %s" % records)


def _sentinel(tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("not the walk-blocker's\n")
    sentinel.chmod(0o600)
    return sentinel


def _sentinel_state(sentinel):
    info = os.lstat(str(sentinel))
    return (sentinel.read_bytes(), stat.S_IMODE(info.st_mode), info.st_gid)


def _swap_in_a_trap(layout, sentinel):
    """The attack: rename the spool aside and put a directory in its place
    holding links named like everything a writer writes, each at the
    sentinel. The replacement is the test user's, which under the fake root
    is "ours" -- so the pin accepts it, and the proof is that no write into
    it follows a link."""
    layout.spool.rename(str(layout.spool) + ".aside")
    layout.spool.mkdir()
    for name in (H.AUDIT_FILENAME, STATE_NAME, STATE_NAME + ".new",
                 "reaper-audit.jsonl", "reaper-state.json",
                 "reaper-state.json.tmp"):
        os.symlink(str(sentinel), str(layout.spool / name))


def test_the_relink_writes_no_link_planted_in_a_swapped_spool(tmp_path):
    """Every name the relink writes is a link at a sentinel; the sentinel's
    bytes, mode and group are unchanged afterwards. Mutation: replace the
    `rm -f` and the noclobber create with `: > "./$_um_state.new"`, and
    the sentinel is truncated (proved by hand in the worktree)."""
    layout = _installed(tmp_path)
    sentinel = _sentinel(tmp_path)
    _swap_in_a_trap(layout, sentinel)
    before = _sentinel_state(sentinel)
    result, _records = relink_with_a_recording_logger(
        tmp_path, layout, fake_uid=0, script=layout.script)
    assert result.returncode == 0, result.stderr
    assert _sentinel_state(sentinel) == before
    assert not (layout.spool / STATE_NAME).is_symlink(), "the memory went through the link"


def test_a_spool_that_is_a_link_to_a_directory_leaves_the_target_alone(tmp_path):
    """`symlink`: reported, and the target keeps its mode and group -- chmod
    and chgrp follow a link, which is exactly why neither is aimed at a
    spool that is one. Mutation: assert the mode through the path, the old
    `install -d -m` way, and the target becomes 2750."""
    layout = _installed(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    target.chmod(0o755)
    shutil.rmtree(str(layout.spool))
    os.symlink(str(target), str(layout.spool))
    before = (stat.S_IMODE(target.stat().st_mode), target.stat().st_gid)
    result, records = relink_with_a_recording_logger(
        tmp_path, layout, fake_uid=0, script=layout.script)
    assert result.returncode == 0, result.stderr
    assert "symlink" in _states(records), records
    assert (stat.S_IMODE(target.stat().st_mode), target.stat().st_gid) == before
    assert not (target / STATE_NAME).exists(), "the memory was written through the link"
