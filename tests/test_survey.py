"""node/survey.py: the tier-three mount survey (ADR-0016), driven against a
fake mount table and a fake statvfs child. Nothing here touches a real mount
or writes outside tmp_path."""
import datetime
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

from walk_blocker import cli, config, paths  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "survey_under_test", os.path.join(paths.node_dir(), "survey.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


survey = _load()

MOUNT_TABLE = """\
/dev/sda1 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid 0 0
fast:/export /scratch lustre rw,flock 0 0
nas:/home /home nfs4 rw,addr=nas.example.org 0 0
box:/srv /mnt/box oddfs rw 0 0
/dev/sdb1 /opt/site-tools ext4 rw 0 0
overlay /var/lib/docker/overlay2/abc/merged overlay rw 0 0
weirdfs /mnt/x weirdfs rw,_netdev 0 0
/dev/sdc1 /mnt/with\\040space ext4 rw 0 0
"""

# A child that answers like statvfs without touching a filesystem.
FAKE_STATS = {"capacity_bytes": 2 ** 40, "free_bytes": 2 ** 39,
              "inodes": 5 * 10 ** 6, "inodes_free": 10 ** 3}
FAKE_CHILD = [sys.executable, "-c",
              "import json, sys; print(json.dumps(%s))" % json.dumps(FAKE_STATS)]
HANGING_CHILD = [sys.executable, "-c", "import time; time.sleep(30)"]
FAILING_CHILD = [sys.executable, "-c", "raise OSError('Transport endpoint is not connected')"]


@pytest.fixture
def mount_table(tmp_path):
    path = tmp_path / "mounts"
    path.write_text(MOUNT_TABLE, encoding="utf-8")
    return str(path)


def by_mountpoint(rows):
    return {r["mountpoint"]: r for r in rows}


def test_classification_from_table_fields(mount_table):
    rows = by_mountpoint(survey.survey(mount_table, timeout=5, statvfs_command=FAKE_CHILD))
    assert rows["/scratch"]["remote_reason"] == "type"
    assert rows["/home"]["remote_reason"] == "type"
    assert rows["/mnt/box"]["remote_reason"] == "source"
    assert rows["/mnt/x"]["remote_reason"] == "option"
    assert rows["/opt/site-tools"]["remote"] is False
    assert rows["/opt/site-tools"]["default_class"] == "cheap"
    assert rows["/"]["default_class"] == "cheap"
    for r in rows.values():
        assert r["default_class"] == ("expensive" if r["remote"] else "cheap")
        assert r["measured"] is True
        assert r["capacity_bytes"] == 2 ** 40 and r["inodes"] == 5 * 10 ** 6
    assert rows["/mnt/with space"]["fstype"] == "ext4"


def test_pseudo_filesystems_are_skipped_unless_all(mount_table):
    default = by_mountpoint(survey.survey(mount_table, timeout=5, statvfs_command=FAKE_CHILD))
    assert "/proc" not in default
    assert "/var/lib/docker/overlay2/abc/merged" not in default
    everything = by_mountpoint(survey.survey(mount_table, timeout=5, include_all=True,
                                             statvfs_command=FAKE_CHILD))
    assert "/proc" in everything and "/var/lib/docker/overlay2/abc/merged" in everything
    assert everything["/var/lib/docker/overlay2/abc/merged"]["default_class"] == "cheap"


def test_remote_proxy_off_leaves_only_the_type_test(mount_table):
    rows = by_mountpoint(survey.survey(mount_table, timeout=5, statvfs_command=FAKE_CHILD,
                                       remote_proxy=False))
    assert rows["/scratch"]["remote"] is True
    assert rows["/mnt/box"]["remote"] is False
    assert rows["/mnt/x"]["remote"] is False


def test_site_fstype_list_is_glob_aware(mount_table):
    rows = by_mountpoint(survey.survey(mount_table, timeout=5, statvfs_command=FAKE_CHILD,
                                       remote_fstypes=["odd*"], remote_proxy=False))
    assert rows["/mnt/box"]["remote_reason"] == "type"
    assert rows["/scratch"]["remote"] is False


def test_a_hanging_child_is_cut_by_the_timeout(mount_table):
    started = time.monotonic()
    rows = survey.survey(mount_table, timeout=0.3, statvfs_command=HANGING_CHILD)
    elapsed = time.monotonic() - started
    assert rows and all(r["measured"] is False for r in rows)
    assert all(r["error"].startswith("timeout") for r in rows)
    assert all(r["capacity_bytes"] is None for r in rows)
    # Seven mounts at 0.3 s each, plus process start; well under the 30 s sleep.
    assert elapsed < 20


def test_a_failing_child_is_reported_not_raised(mount_table):
    rows = survey.survey(mount_table, timeout=5, statvfs_command=FAILING_CHILD)
    assert all(r["measured"] is False for r in rows)
    assert "Transport endpoint" in rows[0]["error"]


def test_unstartable_child_is_reported_not_raised(mount_table, tmp_path):
    rows = survey.survey(mount_table, timeout=5,
                         statvfs_command=[str(tmp_path / "no-such-interpreter")])
    assert all(r["error"].startswith("cannot start child") for r in rows)


def test_the_real_child_measures_a_local_directory(tmp_path):
    table = tmp_path / "mounts"
    table.write_text("x %s ext4 rw 0 0\n" % tmp_path, encoding="utf-8")
    rows = survey.survey(str(table), timeout=10)
    assert rows[0]["measured"] is True, rows[0]["error"]
    assert rows[0]["capacity_bytes"] > 0 and rows[0]["inodes"] >= 0


def test_the_snippet_is_python_39_syntax():
    compile(survey.STATVFS_SNIPPET, "<snippet>", "exec")


def test_render_table_marks_unmeasured(mount_table):
    rows = survey.survey(mount_table, timeout=0.3, statvfs_command=HANGING_CHILD)
    text = survey.render_table(rows)
    assert text.splitlines()[0].split() == [
        "mountpoint", "fstype", "remote", "default_class", "capacity", "inodes", "measured"]
    assert "unmeasured: timeout" in text
    assert "yes (option)" in text


def test_proposed_block_parses_and_validates(mount_table):
    rows = survey.survey(mount_table, timeout=5, statvfs_command=FAKE_CHILD)
    block = survey.render_toml(rows)
    assert "delete this entry to keep the default" in block
    proposed = tomllib.loads(block)["filesystems"]["mounts"]
    assert sorted(m["path"] for m in proposed) == ["/home", "/mnt/box", "/mnt/x", "/scratch"]
    assert all(m["class"] == "expensive" for m in proposed)
    # A measured mount carries its figures and the date they were read; an
    # unmeasured one proposes none, so nothing invented reaches site.toml.
    by_path = {m["path"]: m for m in proposed}
    measured = [r for r in rows if r["measured"] and r["mountpoint"] in by_path]
    assert measured, "the fixture measures nothing"
    for r in measured:
        m = by_path[r["mountpoint"]]
        assert (m["inodes"], m["capacity_bytes"]) == (r["inodes"], r["capacity_bytes"])
        assert m["surveyed"] == datetime.date.today().isoformat()
    for r in rows:
        if not r["measured"] and r["mountpoint"] in by_path:
            assert "inodes" not in by_path[r["mountpoint"]]
    dated = tomllib.loads(survey.render_toml(rows, today="2026-02-03"))["filesystems"]["mounts"]
    assert {m.get("surveyed") for m in dated if "inodes" in m} == {"2026-02-03"}

    # Spliced into the example site in place of its own overrides.
    with open(os.path.join(ROOT, "examples", "site.example.toml"), "rb") as fh:
        site = tomllib.load(fh)
    site["filesystems"]["mounts"] = proposed
    assert config.from_dict(site).lookup("filesystems.mounts[0].class") == "expensive"

    # And appended to a minimal site as text, the way an administrator would.
    minimal = ('schema_version = 1\n[site]\ndisplay_name = "Minimal"\n'
               '[slurm]\npartition = "p"\n[timer]\non_calendar = "*:00:30"\n')
    config.from_dict(tomllib.loads(minimal + "\n" + block))


def test_render_toml_skips_mounts_an_override_covers(mount_table):
    rows = survey.survey(mount_table, timeout=5, statvfs_command=FAKE_CHILD)
    for r in rows:
        r["override"] = "cheap" if r["mountpoint"] == "/home" else None
    proposed = tomllib.loads(survey.render_toml(rows))["filesystems"]["mounts"]
    assert "/home" not in [m["path"] for m in proposed]


def test_standalone_script_runs_on_the_system_python(mount_table):
    """The node runs whatever python3 it has; the script must not need uv."""
    interpreter = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    r = subprocess.run([interpreter, os.path.join(paths.node_dir(), "survey.py"),
                        "--mounts", mount_table, "--timeout", "0.5", "--json"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    rows = by_mountpoint(json.loads(r.stdout))
    assert rows["/scratch"]["default_class"] == "expensive"
    assert "/proc" not in rows


def test_cli_survey_marks_overrides(mount_table, capsys):
    example = os.path.join(ROOT, "examples", "site.example.toml")
    assert cli.main(["survey", "--mounts", mount_table, "--timeout", "0.3",
                     "--site", example, "--json"]) == 0
    rows = by_mountpoint(json.loads(capsys.readouterr().out))
    assert rows["/home"]["override"] == "expensive"
    assert rows["/home"]["override_maxdepth"] == 4
    assert rows["/opt/site-tools"]["override"] == "cheap"
    assert rows["/scratch"]["override"] is None

    assert cli.main(["survey", "--mounts", mount_table, "--timeout", "0.3",
                     "--site", example]) == 0
    out = capsys.readouterr().out
    assert "override" in out.splitlines()[0]
    assert 'path = "/home"' not in out  # already decided by the site


def test_cli_survey_without_site_needs_no_schema(mount_table, capsys):
    assert cli.main(["survey", "--mounts", mount_table, "--timeout", "0.3"]) == 0
    out = capsys.readouterr().out
    assert "[[filesystems.mounts]]" in out
    assert "override" not in out.splitlines()[0]
