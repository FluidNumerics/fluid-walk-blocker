"""One maze, everywhere it is printed.

The banner on the README is the canonical copy. Every script that prints it
carries its own literal -- the shell ones cannot import, and a runtime file
read for a banner is a dependency nothing else needs -- so the copies are
pinned here against the README rather than generated. The version line
stays LAST on --version, and exact, so `tail -n 1` still answers.
"""
import os
import re
import subprocess
import sys
import unicodedata

import pytest

from conftest import ROOT
from _install_helpers import SH
import walk_blocker

README = os.path.join(ROOT, "README.md")
PY_CARRIERS = ["node/reaper.py", "node/deploy.py", "node/survey.py",
               "src/walk_blocker/cli.py"]
SH_CARRIERS = {"node/shim/install.sh": "sg_banner", "node/walk-job": "wj_banner"}


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def readme_banner():
    text = _read("README.md")
    match = re.search(r"^# fluid-walk-blocker\n\n```text\n(.*?)\n```\n", text, re.S)
    assert match, "the README no longer opens with the fenced banner"
    return match.group(1)


def test_the_readme_banner_is_a_maze_with_an_attribution():
    banner = readme_banner()
    lines = banner.split("\n")
    assert len(lines) >= 20 and len({len(l) for l in lines}) <= 2, "not the maze"
    # The wordmarks, through a compatibility fold: the art sets them in
    # mathematical italics, and which styling the artwork uses is the
    # artist's business, not this test's.
    folded = unicodedata.normalize("NFKC", banner)
    assert "FluidNumerics" in folded
    assert "walkblocker" in folded
    assert "asciiart.eu" in _read("README.md")
    # Nothing a shell single-quoted printf argument or a Python triple-quoted
    # literal would have to escape: that is what lets the copies be literal.
    assert not any(c in banner for c in "%$`\\'\""), banner


@pytest.mark.parametrize("rel", PY_CARRIERS)
def test_each_python_carrier_holds_the_readme_banner(rel):
    match = re.search(r'^BANNER = """\\\n(.*?)\n"""$', _read(rel), re.S | re.M)
    assert match, "%s carries no BANNER literal" % rel
    assert match.group(1) == readme_banner(), rel


@pytest.mark.parametrize("rel", sorted(SH_CARRIERS))
def test_each_shell_carrier_prints_the_readme_banner(rel):
    text = _read(rel)
    fn = SH_CARRIERS[rel]
    body = re.search(r"^%s\(\) \{\n(.*?)^\}$" % fn, text, re.S | re.M)
    assert body, "%s has no %s()" % (rel, fn)
    lines = re.findall(r"^        '(.*)' \\$", body.group(1), re.M)
    assert "\n".join(lines) == readme_banner(), rel


def test_the_installed_shell_scripts_print_it_before_the_version(tmp_path, shim_env):
    """Through the real interpreter, with PATH empty: printf is a builtin."""
    from _install_helpers import stamped_install
    layout = stamped_install(tmp_path, dest=tmp_path / "alone")
    out = subprocess.run([SH, str(layout.script), "--version"],
                         capture_output=True, text=True, env={"PATH": ""})
    assert out.returncode == 0, out.stderr
    assert out.stdout == readme_banner() + "\n\nwalk-blocker %s\n" % walk_blocker.__version__
    out = subprocess.run([SH, str(layout.script), "--help"],
                         capture_output=True, text=True, env={"PATH": ""})
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith(readme_banner() + "\n") and "usage: install.sh" in out.stdout


def test_the_python_tools_print_it_on_help_and_version(node_fs):
    """The stamped node scripts and the workstation tool alike, and the
    version line stays last and exact."""
    import _reaper_helpers as RH
    from _deploy_helpers import load_stamped_deploy, site_values
    reaper = RH.load_stamped_reaper(RH.site_values())
    for tool in (reaper, load_stamped_deploy(site_values())):
        for flag in ("--version", "--help"):
            proc = subprocess.run([sys.executable, tool.__file__, flag],
                                  capture_output=True, text=True)
            assert proc.returncode == 0, (tool.__file__, flag, proc.stderr)
            if flag == "--version":
                # First, and the version line last and exact.
                assert proc.stdout.startswith(readme_banner() + "\n"), tool.__file__
                assert proc.stdout.rstrip("\n").splitlines()[-1] == \
                    "walk-blocker %s" % walk_blocker.__version__
            else:
                # argparse prints its usage line first; the banner opens the
                # description under it, unwrapped.
                assert proc.stdout.startswith("usage: "), tool.__file__
                assert ("\n\n" + readme_banner() + "\n\n") in proc.stdout, tool.__file__
    for flag in ("--version", "--help"):
        proc = subprocess.run([sys.executable, "-m", "walk_blocker", flag],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        if flag == "--version":
            assert proc.stdout.startswith(readme_banner() + "\n"), flag
        else:
            assert ("\n\n" + readme_banner() + "\n\n") in proc.stdout, flag


def test_the_banner_survives_a_locale_that_cannot_encode_it(tmp_path):
    """LC_ALL=C on a node's Python 3.9 may or may not coerce to UTF-8; either
    way --version must answer, not trace back."""
    from _deploy_helpers import load_stamped_deploy, site_values
    deploy = load_stamped_deploy(site_values())
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONIOENCODING="ascii")
    env.pop("PYTHONUTF8", None)
    proc = subprocess.run([sys.executable, deploy.__file__, "--version"],
                          capture_output=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.rstrip(b"\n").splitlines()[-1] == \
        ("walk-blocker %s" % walk_blocker.__version__).encode()
