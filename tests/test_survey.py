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
    assert rows["/mnt/with\\040space"]["fstype"] == "ext4"


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
        "mountpoint", "fstype", "remote", "default_class", "capacity", "inodes", "in_use", "measured"]
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
        assert (m["inodes_used"], m["capacity_bytes"]) == (r["inodes_used"], r["capacity_bytes"])
        assert r["inodes_used"] == r["inodes"] - r["inodes_free"]
        assert m["surveyed"] == datetime.date.today().isoformat()
    for r in rows:
        if not r["measured"] and r["mountpoint"] in by_path:
            assert "inodes_used" not in by_path[r["mountpoint"]]
    dated = tomllib.loads(survey.render_toml(rows, today="2026-02-03"))["filesystems"]["mounts"]
    assert {m.get("surveyed") for m in dated if "inodes_used" in m} == {"2026-02-03"}

    # Spliced into the example site in place of its own overrides.
    with open(os.path.join(ROOT, "examples", "site.example.toml"), "rb") as fh:
        site = tomllib.load(fh)
    site["filesystems"]["mounts"] = proposed
    assert config.from_dict(site).lookup("filesystems.mounts[0].class") == "expensive"

    # And appended to a minimal site as text, the way an administrator would.
    minimal = ('schema_version = 1\n[site]\ndisplay_name = "Minimal"\n'
               '[install]\nspool_group = "wbaudit"\ntrusted_groups = []\n'
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


def test_a_filesystem_that_reports_more_free_inodes_than_it_has_reads_as_zero():
    """`inodes_used` is a subtraction of two numbers the filesystem reports
    independently, and a network filesystem that answers a synthetic total
    can report more free than total. The page would otherwise show a
    negative count, which the schema refuses; clamped at the source, where
    the arithmetic is."""
    row = survey.measure("/x", timeout=5, statvfs_command=[
        sys.executable, "-c",
        "import json; print(json.dumps({'capacity_bytes': 1, 'free_bytes': 1,"
        " 'inodes': 10, 'inodes_free': 25}))"])
    assert row["measured"] is True
    assert row["inodes"] == 10 and row["inodes_free"] == 25
    assert row["inodes_used"] == 0


@pytest.mark.parametrize("locale", ("C", "C.UTF-8"))
def test_a_non_utf8_mount_table_is_surveyed_not_raised(tmp_path, locale):
    """Issue #163, for the other node reader of the mount table. survey.py
    already decoded with errors="replace", so this pins behaviour that held
    before the fix; it is not the fix's oracle. A source byte that is not
    UTF-8 is replaced, not raised on, and the `host:` source still reads as
    remote, as the shim's byte readers class it."""
    mounts = tmp_path / "mounts"
    mounts.write_bytes(b"/dev/sda1 / ext4 rw 0 0\n"
                       b"\377h:/e /mnt/b xfs rw 0 0\n"
                       b"fast /mnt/\377c wekafs rw 0 0\n")
    environ = {k: v for k, v in os.environ.items()
               if not k.startswith(("LC_", "PYTHON")) and k != "LANG"}
    environ["LC_ALL"] = locale
    for extra in (["--json"], []):
        r = subprocess.run([sys.executable,
                            os.path.join(paths.node_dir(), "survey.py"),
                            "--mounts", str(mounts), "--timeout", "0.5"] + extra,
                           capture_output=True, env=environ, timeout=60)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        if extra:
            rows = by_mountpoint(json.loads(r.stdout))
            assert rows["/mnt/b"]["remote_reason"] == "source"
            assert rows["/mnt/\\377c"]["remote_reason"] == "type"
            assert rows["/mnt/\\377c"]["nameable"] is False


def test_survey_splits_mount_fields_as_the_shim_does(tmp_path):
    """Issue #172. splitlines() and str.split() break a line on \\v, \\f,
    U+00A0 and U+0085 as well as on space and tab; the shim's readers do
    not. Each `host:` source below holds one of them, and each row is still
    remote by its source, at its own mount point."""
    mounts = tmp_path / "mounts"
    mounts.write_bytes(b"/dev/sda1 / ext4 rw 0 0\n"
                       b"h\x0b:/e /mnt/v xfs rw 0 0\n"
                       b"h\x0c:/e /mnt/f xfs rw 0 0\n"
                       b"h\xc2\xa0:/e /mnt/n xfs rw 0 0\n"
                       b"h\xc2\x85:/e /mnt/x xfs rw 0 0\n")
    rows = by_mountpoint(survey.survey(str(mounts), timeout=0.5,
                                       statvfs_command=FAKE_CHILD))
    assert sorted(rows) == ["/", "/mnt/f", "/mnt/n", "/mnt/v", "/mnt/x"]
    for point in ("/mnt/f", "/mnt/n", "/mnt/v", "/mnt/x"):
        assert rows[point]["remote_reason"] == "source", rows[point]


def test_survey_keeps_a_carriage_return_inside_its_field(tmp_path):
    """Issue #172, from PR #174's review. The shim's readers end a row on
    \\n alone, so a \\r is a byte of its field. Universal newlines ended the
    row on it: `/m\\rp` became a mount point `p`. The \\r is shown as
    \\015, the octal form survey prints for a byte site.toml cannot name
    (issue #173)."""
    mounts = tmp_path / "mounts"
    mounts.write_bytes(b"/dev/sda1 / ext4 rw 0 0\n"
                       b"h:/e /m\rp xfs rw 0 0\n"
                       b"h\r:/e /mnt/r xfs rw 0 0\r\n")
    rows = by_mountpoint(survey.survey(str(mounts), timeout=0.5,
                                       statvfs_command=FAKE_CHILD))
    assert sorted(rows) == ["/", "/m\\015p", "/mnt/r"]
    for point in ("/m\\015p", "/mnt/r"):
        assert rows[point]["remote_reason"] == "source", rows[point]


# A child that reports the bytes it was handed, as hex, in the inode count's
# place: what statvfs would have been asked to measure.
ARGV_CHILD = [sys.executable, "-c",
              "import json, os, sys; print(json.dumps({'capacity_bytes': 1,"
              " 'free_bytes': 0, 'inodes': int(os.fsencode(sys.argv[1]).hex(), 16),"
              " 'inodes_free': 0}))"]

# Fictional mount points: a byte that is not UTF-8, a space the kernel
# escapes, a non-ASCII name that is valid UTF-8, and a plain one.
UNNAMEABLE_TABLE = (b"a:/x /mnt/\377c nfs4 rw 0 0\n"
                    b"b:/y /mnt/with\\040sp nfs4 rw 0 0\n"
                    b"c:/z /mnt/caf\xc3\xa9 nfs4 rw 0 0\n"
                    b"d:/w /mnt/plain nfs4 rw 0 0\n")
UNNAMEABLE = {
    "/mnt/\\377c": b"/mnt/\377c",
    "/mnt/with\\040sp": b"/mnt/with sp",
    "/mnt/caf\\303\\251": b"/mnt/caf\xc3\xa9",
}


@pytest.fixture
def unnameable_table(tmp_path):
    path = tmp_path / "mounts"
    path.write_bytes(UNNAMEABLE_TABLE)
    return str(path)


def test_statvfs_is_handed_the_real_mount_point_bytes(unnameable_table):
    """Issue #173. The table was read with replacement, so a byte that is
    not UTF-8 became U+FFFD and statvfs was asked about a path that does
    not exist. The child now gets the kernel's bytes, unescaped."""
    rows = by_mountpoint(survey.survey(unnameable_table, timeout=5,
                                       statvfs_command=ARGV_CHILD))
    expected = dict(UNNAMEABLE, **{"/mnt/plain": b"/mnt/plain"})
    assert sorted(rows) == sorted(expected)
    for shown, raw in expected.items():
        assert rows[shown]["measured"] is True, rows[shown]["error"]
        assert rows[shown]["inodes"] == int(raw.hex(), 16), shown


def test_a_mount_point_is_shown_with_every_unnameable_byte_octal_escaped():
    """The kernel's own `\\ooo`, for space, tab, newline and backslash,
    extended to every byte outside the sink_path set; a nameable path is
    unchanged."""
    assert survey.display_path(b"/mnt/plain-1.x_y") == "/mnt/plain-1.x_y"
    assert survey.display_path(b"/a b\tc\nd\\e") == "/a\\040b\\011c\\012d\\134e"
    assert survey.display_path(b"/\xff") == "/\\377"
    assert survey.display_path(b"/caf\xc3\xa9") == "/caf\\303\\251"
    assert survey.display_path(b"/a+b") == "/a\\053b"
    # Every escape reads back with the one decoder the mount table needs.
    for raw in UNNAMEABLE.values():
        shown = survey.display_path(raw).encode("ascii")
        assert survey._unescape(shown) == raw


@pytest.mark.parametrize("raw, ok", [
    (b"/mnt/plain", True), (b"/", False), (b"/mnt/./x", False),
    (b"/mnt/../x", False), (b"/mnt/x/", False), (b"/mnt//x", False),
    (b"/mnt/a b", False), (b"/mnt/\xff", False), (b"/mnt/a+b", False),
    (b"/mnt/x\n", False),
])
def test_nameable_is_the_schemas_sink_path(raw, ok):
    assert survey.nameable(raw) is ok


def test_a_mount_point_site_toml_cannot_name_is_reported_not_proposed(
        unnameable_table):
    """Issue #173. A `path` for these would fail validate, or, decoded with
    replacement, name a path that is not the mount. Each is measured and
    named, escaped, in a comment; only the plain one gets an entry, and that
    entry still validates."""
    rows = survey.survey(unnameable_table, timeout=5, statvfs_command=FAKE_CHILD)
    block = survey.render_toml(rows, today="2026-02-03")
    proposed = tomllib.loads(block)["filesystems"]["mounts"]
    assert [m["path"] for m in proposed] == ["/mnt/plain"]
    for shown in UNNAMEABLE:
        header = [line for line in block.splitlines()
                  if line.startswith("# %s: " % shown)]
        assert len(header) == 1, block
        assert "capacity" in header[0]
    assert block.count("Not proposed: site.toml cannot name") == len(UNNAMEABLE)
    assert "issue #184" in block
    assert block.isascii()
    minimal = ('schema_version = 1\n[site]\ndisplay_name = "Minimal"\n'
               '[install]\nspool_group = "wbaudit"\ntrusted_groups = []\n'
               '[slurm]\npartition = "p"\n[timer]\non_calendar = "*:00:30"\n')
    config.from_dict(tomllib.loads(minimal + "\n" + block))


def test_an_unmeasured_mount_point_is_reported_in_ascii(unnameable_table):
    """The child's error quotes the path it was handed, the real bytes; the
    comment escapes it, as it escapes the mount point."""
    child = [sys.executable, "-c",
             "import os, sys; raise SystemExit("
             "'cannot stat %s' % os.fsdecode(sys.argv[1]))"]
    rows = survey.survey(unnameable_table, timeout=5, statvfs_command=child)
    assert not any(r["measured"] for r in rows)
    block = survey.render_toml(rows, today="2026-02-03")
    assert "unmeasured (cannot stat /mnt/caf\\xe9" in block, block
    assert block.isascii(), block


def test_json_and_text_output_carry_the_escaped_form(unnameable_table):
    """Valid JSON with no lone surrogate, from node/survey.py run as a
    script, in a C locale as well as a UTF-8 one. The payload's copy is
    the same file; test_build's payload check fails when it is stale."""
    environ = {k: v for k, v in os.environ.items()
               if not k.startswith(("LC_", "PYTHON")) and k != "LANG"}
    for locale in ("C", "C.UTF-8"):
        environ["LC_ALL"] = locale
        r = subprocess.run([sys.executable,
                            os.path.join(paths.node_dir(), "survey.py"),
                            "--mounts", unnameable_table, "--timeout", "0.5",
                            "--json"], capture_output=True, env=environ, timeout=60)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        r.stdout.decode("ascii")
        rows = by_mountpoint(json.loads(r.stdout))
        assert set(UNNAMEABLE) < set(rows)
        assert all(rows[shown]["nameable"] is False for shown in UNNAMEABLE)
        assert rows["/mnt/plain"]["nameable"] is True
        text = subprocess.run([sys.executable,
                               os.path.join(paths.node_dir(), "survey.py"),
                               "--mounts", unnameable_table, "--timeout", "0.5"],
                              capture_output=True, env=environ, timeout=60)
        assert text.returncode == 0
        assert b"/mnt/caf\\303\\251" in text.stdout
