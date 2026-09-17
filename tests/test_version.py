"""VERSION is the only hand-written version, and it has exactly one shape."""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import walk_blocker  # noqa: E402
from walk_blocker import paths  # noqa: E402


def test_version_file_is_bare_semver():
    with open(os.path.join(ROOT, "VERSION"), encoding="ascii") as fh:
        raw = fh.read()
    # Stricter than hatch's own pattern in pyproject.toml, which tolerates
    # trailing whitespace: the FILE is pinned bare here even though every
    # reader is defensive. Do not "fix" the two into agreement.
    assert raw.endswith("\n") and "\n" not in raw[:-1]
    assert paths.VERSION_RE.match(raw[:-1]), repr(raw)


def test_the_package_reports_the_version_file():
    with open(os.path.join(ROOT, "VERSION"), encoding="ascii") as fh:
        assert walk_blocker.__version__ == fh.read().strip()


@pytest.mark.parametrize("bad", ["", "1.2", "v1.2.3", "1.2.3-rc1", "1.2.3'",
                                 "١.2.3", "1.2.3\n4.5.6"])
def test_a_malformed_version_is_refused_not_passed_through(tmp_path, monkeypatch, bad):
    vf = tmp_path / "VERSION"
    vf.write_text(bad + "\n", encoding="utf-8")
    monkeypatch.setattr(paths, "version_file", lambda: str(vf))
    with pytest.raises((ValueError, UnicodeDecodeError)):
        paths.read_version()


def test_python_m_walk_blocker_answers_version():
    out = subprocess.run([sys.executable, "-m", "walk_blocker", "--version"],
                         capture_output=True, text=True, cwd=ROOT,
                         env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")})
    assert out.returncode == 0
    assert out.stdout.strip().splitlines()[-1] == "walk-blocker %s" % walk_blocker.__version__


def test_license_is_bsd_3_clause_and_names_the_holder():
    """Three numbered clauses and the disclaimer, so a truncated or
    two-clause licence fails rather than passing as "a BSD licence"."""
    with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
        text = fh.read()
    assert text.startswith("BSD 3-Clause License")
    assert "Copyright (c) 2026, Trevor Keller, PhD" in text
    for clause in ("1. Redistributions of source code",
                   "2. Redistributions in binary form",
                   "3. Neither the name of the copyright holder"):
        assert clause in text, clause
    assert "AS IS" in text and "NO EVENT SHALL" in text


def test_the_payload_carries_the_licence(tmp_path):
    """Clause 1 travels with the redistribution: a node that has the code
    has the terms, beside it, without asking anyone."""
    from walk_blocker import build, paths
    out = tmp_path / "payload"
    code = build.build(os.path.join(ROOT, "examples", "site.example.toml"), str(out))
    assert code == 0
    shipped = out / "LICENSE"
    assert shipped.exists()
    with open(str(shipped), encoding="utf-8") as fh:
        assert fh.read() == open(paths.license_file(), encoding="utf-8").read()
