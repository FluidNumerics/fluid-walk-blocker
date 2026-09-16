"""`walk-blocker build`: deterministic, self-checking, refuses what it did
not make. Every build here is of the fictional example site (ADR-0014).

Tests that need the rendered shim skip until `walk_blocker.render.shim`
exists; everything else in the payload is exercised regardless.
"""
import filecmp
import io
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import walk_blocker  # noqa: E402
from walk_blocker import build, cli, paths  # noqa: E402
from walk_blocker.render import manifest  # noqa: E402

EXAMPLE = os.path.join(ROOT, "examples", "site.example.toml")


def _build(site, out, check=False):
    """Run the build with its streams captured; return (code, stdout, stderr)."""
    o, e = io.StringIO(), io.StringIO()
    code = build.build(str(site), str(out), check=check, out=o, err=e)
    return code, o.getvalue(), e.getvalue()


def _mode(path):
    return stat.S_IMODE(os.lstat(str(path)).st_mode)


def _walk(root):
    root = str(root)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            yield os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")


@pytest.fixture(scope="module")
def payload(tmp_path_factory):
    out = tmp_path_factory.mktemp("build") / "payload"
    code, _o, _e = _build(EXAMPLE, out)
    assert code == 0
    return out


@pytest.fixture(scope="module")
def payload_again(tmp_path_factory):
    out = tmp_path_factory.mktemp("build-again") / "payload"
    code, _o, _e = _build(EXAMPLE, out)
    assert code == 0
    return out


# ---------------------------------------------------------------- shape --

def test_the_payload_has_the_documented_layout(payload):
    have = set(_walk(payload))
    for rel in ("site.toml", "site.lock.json", "README.md", "search_rules.py",
                "survey.py", "docs/evidence.md", "docs/site-config.md",
                "docs/operating.md", "shim/measure.sh", "shim/measure-flags.sh",
                "docs/adr/0013-site-config-compiled-at-build.md", build.BUILD_MARKER):
        assert rel in have, rel
    assert not any(rel.startswith(".") and rel != build.BUILD_MARKER for rel in have)
    assert not any("__pycache__" in rel or rel.endswith(".pyc") for rel in have)


def test_shim_files_are_present_with_their_modes(payload):
    pytest.importorskip("walk_blocker.render.shim")
    assert _mode(payload / "shim" / "guard.sh") == 0o755
    assert _mode(payload / "shim" / "wrapped_names.sh") == 0o644
    # The benchmark is run by an operator; the flag probe is sourced/read.
    assert _mode(payload / "shim" / "measure.sh") == 0o755
    assert _mode(payload / "shim" / "measure-flags.sh") == 0o644


def test_everything_else_is_0644_and_directories_0755(payload):
    for rel in _walk(payload):
        want = 0o755 if rel in build.EXECUTABLE else 0o644
        assert _mode(payload / rel) == want, rel
    for dirpath, dirnames, _f in os.walk(str(payload)):
        for d in dirnames:
            assert _mode(os.path.join(dirpath, d)) == 0o755


def test_no_unfilled_placeholder_in_any_output_file(payload):
    """Excluding the verbatim prose: ADR-0013 describes the `@@KEY@@`
    template convention by name, which is not an unfilled placeholder."""
    for rel in _walk(payload):
        if rel.startswith("docs/") or rel == "README.md":
            continue
        with open(str(payload / rel), "rb") as fh:
            assert b"@@" not in fh.read(), rel


def test_verbatim_copies_are_byte_identical_to_their_sources(payload):
    pairs = [
        ("site.toml", EXAMPLE),
        ("search_rules.py", paths.rules_file()),
        ("survey.py", os.path.join(paths.node_dir(), "survey.py")),
        ("shim/measure.sh", os.path.join(paths.node_dir(), "shim", "measure.sh")),
        ("shim/measure-flags.sh", os.path.join(paths.node_dir(), "shim", "measure-flags.sh")),
        ("README.md", paths.readme_file()),
        ("docs/evidence.md", os.path.join(paths.docs_dir(), "evidence.md")),
    ]
    for rel, source in pairs:
        assert filecmp.cmp(str(payload / rel), source, shallow=False), rel


@pytest.mark.skipif(not os.path.exists("/usr/bin/python3"), reason="no /usr/bin/python3")
def test_every_python_file_compiles_on_the_system_interpreter(payload, tmp_path):
    """The node's interpreter is the distro's (ADR-0015), not uv's."""
    for rel in _walk(payload):
        if rel.endswith(".py"):
            r = subprocess.run(["/usr/bin/python3", "-m", "py_compile", str(payload / rel)],
                               capture_output=True, text=True,
                               env=dict(os.environ, PYTHONPYCACHEPREFIX=str(tmp_path)))
            assert r.returncode == 0, (rel, r.stderr)


# ------------------------------------------------------------ manifest --

def test_manifest_lists_every_file_with_its_hash(payload):
    lock = manifest.read_manifest(str(payload / manifest.FILENAME))
    assert set(lock) == {"schema_version", "walk_blocker_version", "version",
                         "site_sha256", "files"}
    assert lock["version"] == paths.read_version()
    assert lock["walk_blocker_version"] == walk_blocker.__version__
    assert lock["schema_version"] == 1
    with open(EXAMPLE, "rb") as fh:
        assert lock["site_sha256"] == manifest.sha256(fh.read())
    on_disk = set(_walk(payload)) - {manifest.FILENAME, build.BUILD_MARKER}
    assert set(lock["files"]) == on_disk
    for rel, digest in lock["files"].items():
        with open(str(payload / rel), "rb") as fh:
            assert manifest.sha256(fh.read()) == digest, rel


def test_manifest_is_canonical_json_with_only_the_five_keys(payload):
    """No timestamp, no hostname, no build-machine fact: the key set is
    closed (asserted above) and the bytes are the canonical dump, so two
    builds of the same inputs cannot differ here."""
    raw = (payload / manifest.FILENAME).read_bytes()
    assert raw.endswith(b"\n")
    lock = json.loads(raw)
    assert raw == manifest.manifest_bytes(lock)  # sorted keys, two-space indent
    assert list(lock) == sorted(lock)


def test_manifest_refuses_to_list_itself():
    with pytest.raises(ValueError):
        manifest.render_manifest(b"", "1.0.0", 1, "1.0.0", {manifest.FILENAME: b""})


def test_write_manifest_round_trips(tmp_path):
    lock = manifest.write_manifest(str(tmp_path), EXAMPLE, "1.0.0", 1, "1.0.0",
                                   {"a": b"x", "b/c": b"y"})
    assert manifest.read_manifest(str(tmp_path / manifest.FILENAME)) == lock
    assert set(lock["files"]) == {"a", "b/c"}
    assert _mode(tmp_path / manifest.FILENAME) == 0o644


# --------------------------------------------------------- determinism --

def test_two_builds_are_deep_equal_including_modes(payload, payload_again):
    def deep(dc):
        assert not dc.left_only and not dc.right_only, (dc.left_only, dc.right_only)
        assert not dc.diff_files and not dc.funny_files, (dc.diff_files, dc.funny_files)
        for sub in dc.subdirs.values():
            deep(sub)
    deep(filecmp.dircmp(str(payload), str(payload_again), ignore=[], hide=[]))
    for rel in _walk(payload):
        assert _mode(payload / rel) == _mode(payload_again / rel), rel
    assert ((payload / manifest.FILENAME).read_bytes()
            == (payload_again / manifest.FILENAME).read_bytes())


# ------------------------------------------------------------------ check --

def test_check_is_clean_on_a_fresh_build(payload):
    code, out, _e = _build(EXAMPLE, payload, check=True)
    assert (code, out) == (0, "")


def test_check_writes_nothing(payload):
    before = {rel: (payload / rel).read_bytes() for rel in _walk(payload)}
    mtimes = {rel: os.stat(str(payload / rel)).st_mtime_ns for rel in before}
    _build(EXAMPLE, payload, check=True)
    assert {rel: (payload / rel).read_bytes() for rel in _walk(payload)} == before
    assert {rel: os.stat(str(payload / rel)).st_mtime_ns for rel in before} == mtimes


@pytest.fixture
def scratch_payload(payload, tmp_path):
    """A private copy of the module-scoped payload to damage."""
    copy = tmp_path / "payload"
    shutil.copytree(str(payload), str(copy))
    return copy


def test_check_flags_a_changed_byte(scratch_payload):
    with open(str(scratch_payload / "README.md"), "ab") as fh:
        fh.write(b"\n")
    code, out, _e = _build(EXAMPLE, scratch_payload, check=True)
    assert code == 1
    assert out.splitlines() == ["differs: README.md"]


def test_check_flags_a_deleted_file(scratch_payload):
    os.unlink(str(scratch_payload / "survey.py"))
    code, out, _e = _build(EXAMPLE, scratch_payload, check=True)
    assert code == 1
    assert out.splitlines() == ["missing: survey.py"]


def test_check_flags_an_extra_file(scratch_payload):
    (scratch_payload / "docs" / "notes.md").write_text("stray\n")
    (scratch_payload / ".hidden").write_text("stray\n")
    code, out, _e = _build(EXAMPLE, scratch_payload, check=True)
    assert code == 1
    assert out.splitlines() == ["extra: .hidden", "extra: docs/notes.md"]


def test_check_flags_a_mode_change(scratch_payload):
    os.chmod(str(scratch_payload / "survey.py"), 0o755)
    code, out, _e = _build(EXAMPLE, scratch_payload, check=True)
    assert code == 1
    assert out.splitlines() == ["mode: survey.py is 0755, expected 0644"]


def test_check_flags_a_stale_manifest_after_a_config_edit(scratch_payload, tmp_path):
    """The site changed, the payload did not: site.toml and the lock differ."""
    edited = tmp_path / "site.toml"
    edited.write_text(open(EXAMPLE).read().replace("fanout_n = 4", "fanout_n = 5"))
    code, out, _e = _build(edited, scratch_payload, check=True)
    assert code == 1
    assert "differs: site.toml" in out.splitlines()
    assert "differs: site.lock.json" in out.splitlines()


def test_check_against_a_missing_directory_differs(tmp_path):
    code, out, _e = _build(EXAMPLE, tmp_path / "nowhere", check=True)
    assert code == 1 and out.startswith("missing:")


# --------------------------------------------------------------- refusal --

def test_refuses_a_non_empty_directory_without_the_marker(tmp_path):
    out = tmp_path / "theirs"
    out.mkdir()
    (out / "precious").write_text("do not delete\n")
    code, _o, err = _build(EXAMPLE, out)
    assert code == 2
    assert build.BUILD_MARKER in err and "refusing" in err
    assert (out / "precious").read_text() == "do not delete\n"
    assert os.listdir(str(out)) == ["precious"]


def test_refuses_a_path_that_is_a_file(tmp_path):
    out = tmp_path / "file"
    out.write_text("x")
    code, _o, err = _build(EXAMPLE, out)
    assert code == 2 and "not a directory" in err
    assert out.read_text() == "x"


def test_builds_into_an_empty_directory(tmp_path):
    out = tmp_path / "empty"
    out.mkdir()
    code, _o, _e = _build(EXAMPLE, out)
    assert code == 0 and (out / build.BUILD_MARKER).exists()


def test_rebuilds_a_directory_that_carries_the_marker(scratch_payload):
    (scratch_payload / "leftover").write_text("from an older build\n")
    with open(str(scratch_payload / "README.md"), "ab") as fh:
        fh.write(b"drift\n")
    code, _o, _e = _build(EXAMPLE, scratch_payload)
    assert code == 0
    assert not (scratch_payload / "leftover").exists()
    assert _build(EXAMPLE, scratch_payload, check=True)[0] == 0


def test_a_failed_build_leaves_no_staging_litter_and_the_old_payload(scratch_payload, monkeypatch):
    def boom(*_a, **_k):
        raise build.BuildError("simulated")
    before = sorted(_walk(scratch_payload))
    monkeypatch.setattr(build, "render_payload", boom)
    code, _o, err = _build(EXAMPLE, scratch_payload)
    assert code == 2 and "simulated" in err
    assert sorted(_walk(scratch_payload)) == before
    assert os.listdir(str(scratch_payload.parent)) == ["payload"]


# ------------------------------------------------------------ config errors --

def test_a_config_error_exits_2_with_the_validate_wording(tmp_path):
    bad = tmp_path / "site.toml"
    bad.write_text(open(EXAMPLE).read().replace("maxdepth_allowed = 2", "maxdepth_allowed = 99"))
    code, out, err = _build(bad, tmp_path / "out")
    assert code == 2 and out == ""
    assert "filesystems.maxdepth_allowed" in err
    assert not (tmp_path / "out").exists()


def test_a_schema_error_exits_2(tmp_path):
    bad = tmp_path / "site.toml"
    bad.write_text(open(EXAMPLE).read().replace('gate = "required"', 'gate = "sometimes"'))
    code, _o, err = _build(bad, tmp_path / "out")
    assert code == 2 and "/hooks/" in err


def test_an_unreadable_or_malformed_site_exits_2(tmp_path):
    code, _o, err = _build(tmp_path / "absent.toml", tmp_path / "out")
    assert code == 2 and "absent.toml" in err
    bad = tmp_path / "site.toml"
    bad.write_text("this is = not toml = at all\n")
    code, _o, err = _build(bad, tmp_path / "out")
    assert code == 2


def test_an_unfilled_placeholder_fails_the_build(monkeypatch, tmp_path):
    shim = pytest.importorskip("walk_blocker.render.shim")
    monkeypatch.setattr(build, "render_shim", lambda *a: shim.render_shim(*a) + "@@LEFT@@\n")
    code, _o, err = _build(EXAMPLE, tmp_path / "out")
    assert code == 2 and "@@" in err and "shim/guard.sh" in err
    assert not (tmp_path / "out").exists()


def test_a_verbatim_file_with_a_marker_outside_consumers_fails(monkeypatch, tmp_path):
    """A marker nobody stamps is a literal nobody checks."""
    rules = tmp_path / "search_rules.py"
    rules.write_text("X = 'old'  # GENERATED from VERSION\n")
    monkeypatch.setattr(paths, "rules_file", lambda: str(rules))
    code, _o, err = _build(EXAMPLE, tmp_path / "out")
    assert code == 2 and "CONSUMERS" in err and "search_rules.py" in err


# ------------------------------------------------------------------- CLI --

def test_cli_build_exit_codes(tmp_path, capsys):
    out = tmp_path / "out"
    assert cli.main(["build", "--site", EXAMPLE, "--out", str(out)]) == 0
    assert cli.main(["build", "--site", EXAMPLE, "--out", str(out), "--check"]) == 0
    (out / "extra").write_text("x")
    assert cli.main(["build", "--site", EXAMPLE, "--out", str(out), "--check"]) == 1
    assert capsys.readouterr().out.strip().endswith("extra: extra")
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "x").write_text("x")
    assert cli.main(["build", "--site", EXAMPLE, "--out", str(theirs)]) == 2
    assert cli.main(["build", "--site", str(tmp_path / "none.toml"), "--out", str(out)]) == 2


def test_cli_build_requires_both_arguments():
    with pytest.raises(SystemExit) as info:
        cli.main(["build", "--site", EXAMPLE])
    assert info.value.code == 2


def test_python_m_walk_blocker_build(tmp_path):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    r = subprocess.run([sys.executable, "-m", "walk_blocker", "build", "--site", EXAMPLE,
                        "--out", str(tmp_path / "out")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / manifest.FILENAME).exists()
    r = subprocess.run([sys.executable, "-m", "walk_blocker", "build", "--site", EXAMPLE,
                        "--out", str(tmp_path / "out"), "--check"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_other_subcommands_are_untouched():
    parser = cli.build_parser()
    names = parser._subparsers._group_actions[0].choices
    assert set(names) == {"validate", "schema", "survey", "build"}


# ------------------------------------------------------- committed example --

def test_the_committed_example_payload_is_current():
    """`examples/payload/` is a build product (ADR-0013); `--check` is the
    oracle, here and in CI."""
    example = os.path.join(ROOT, "examples", "payload")
    if not os.path.isdir(example):
        pytest.skip("examples/payload has not been built")
    code, out, _e = _build(EXAMPLE, example, check=True)
    assert code == 0, out
