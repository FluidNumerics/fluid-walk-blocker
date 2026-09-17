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
    assert len(lines) >= 20, "not the maze"
    folded = unicodedata.normalize("NFKC", banner).upper()
    assert "FLUIDNUMERICS" in folded
    assert "WALKBLOCKER" in folded
    assert "asciiart.eu" in _read("README.md")


# The repertoire the artwork draws from, and the whole of it. ASCII and the
# box-drawing block are in every monospace font; the two walkers are not, and
# are here because the artist kept them deliberately after the rest was
# redrawn -- one cell each, in the outermost column, where a substituted
# advance width shifts a wall by one rather than by thirteen.
WALKERS = "\U0001fbc7\U0001fbc8"


def _out_of_repertoire(text):
    return sorted({c for c in text
                   if not (c.isascii() or "\u2500" <= c <= "\u257f" or c in WALKERS)})


def test_the_banner_uses_only_glyphs_a_monospace_font_will_have():
    """The defect this pins: the wordmarks were once set in Mathematical
    Alphanumeric Symbols, which GitHub's monospace stack does not cover, so a
    browser substituted a proportional font and the two rows carrying them
    claimed sixty-odd cells against every other row's fifty-three. The walls
    bent. Nothing caught it, because every row was the same number of CODE
    POINTS and `east_asian_width` calls those characters neutral -- neither
    property models font fallback.

    So this constrains the repertoire rather than the width. A future
    revision that reaches for a styled letter, an emoji or a symbol outside
    the block fails here, at CI, rather than on someone's screen."""
    stray = _out_of_repertoire(readme_banner())
    assert not stray, (
        "the banner uses %r, outside ASCII, the box-drawing block and the two "
        "walkers -- a monospace stack may not carry it, and a substituted "
        "glyph's advance width is what bent the walls last time"
        % ["U+%04X %s" % (ord(c), unicodedata.name(c, "?")) for c in stray])


def test_every_row_is_the_same_number_of_code_points_and_none_needs_escaping():
    """Necessary and, on its own, not sufficient -- see the test above, which
    exists because this one passed while the walls were visibly bent."""
    banner = readme_banner()
    assert len({len(line) for line in banner.split("\n")}) == 1
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
