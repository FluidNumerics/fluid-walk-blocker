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
    assert raw.endswith("\n") and raw.count("\n") == 1
    assert paths.VERSION_RE.match(raw.strip())


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
    assert out.stdout.strip() == "walk-blocker %s" % walk_blocker.__version__


def test_license_names_the_owner_and_year():
    with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
        text = fh.read()
    assert "Fluid Numerics LLC" in text
    assert "2026" in text
    assert "All rights reserved" in text
