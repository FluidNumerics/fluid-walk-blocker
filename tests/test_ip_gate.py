"""The IP-hygiene gate: the tree is clean, and the scanner does what it says.

Every self-test uses MADE-UP terms written to tmp_path. The real customer
term list is never read by a test that could print anything derived from it.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

import check_no_site_literals as gate  # noqa: E402


def run(args, **kw):
    return subprocess.run([sys.executable, os.path.join(TOOLS, "check_no_site_literals.py")] + args,
                          capture_output=True, text=True, **kw)


# --- the tree -----------------------------------------------------------------

def test_the_tracked_tree_has_no_structural_site_literals():
    r = run(["--root", ROOT, "--terms", os.devnull])
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stderr.strip().endswith("terms=off")


def test_the_customer_term_list_is_consulted_when_present():
    r = run(["--root", ROOT, "--require-terms", "--quiet"])
    if r.returncode == gate.EXIT_NO_TERMS:
        pytest.skip("no customer term list on this machine; CI's ip-hygiene job enforces it")
    assert r.returncode == 0, r.stdout
    assert r.stderr.strip().endswith("terms=on")


# --- the scanner, against made-up terms ----------------------------------------

@pytest.fixture
def sandbox(tmp_path):
    terms = tmp_path / "terms.txt"
    terms.write_text("# made up\n\\bwidget-corp-login\\b\nuid=12345\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    return tree, terms


def scan(tree, terms, *extra):
    return run(["--root", str(tree), "--terms", str(terms), *extra, "."])


def test_a_customer_term_is_reported_by_position_only(sandbox):
    tree, terms = sandbox
    (tree / "a.md").write_text("first line\nthe host widget-corp-login is here\n", encoding="utf-8")
    r = scan(tree, terms)
    assert r.returncode == gate.EXIT_FINDINGS
    assert r.stdout.strip() == "a.md:2: customer-term"
    assert "acme" not in r.stdout + r.stderr
    assert "the host" not in r.stdout + r.stderr


def test_a_waiver_silences_a_structural_hit_but_never_a_customer_term(sandbox):
    tree, terms = sandbox
    (tree / "a.md").write_text(
        "addr 10.0.0.1  # site-literal-ok: documentation example address\n"
        "widget-corp-login  # site-literal-ok: this waiver must not work\n", encoding="utf-8")
    r = scan(tree, terms)
    assert r.returncode == gate.EXIT_FINDINGS
    assert r.stdout.strip() == "a.md:2: customer-term"


def test_a_short_waiver_reason_does_not_count(sandbox):
    tree, terms = sandbox
    (tree / "a.md").write_text("addr 10.0.0.1  # site-literal-ok: ok\n", encoding="utf-8")
    r = scan(tree, terms)
    assert r.returncode == gate.EXIT_FINDINGS and "ipv4" in r.stdout


def test_require_terms_exits_3_without_a_list(tmp_path):
    (tmp_path / "a.md").write_text("clean\n", encoding="utf-8")
    r = run(["--root", str(tmp_path), "--require-terms", "."],
            env={**os.environ, "WALK_BLOCKER_FORBIDDEN_TERMS": "", "HOME": str(tmp_path),
                 "XDG_CONFIG_HOME": str(tmp_path / "nocfg")})
    assert r.returncode == gate.EXIT_NO_TERMS
    assert "no customer term list" in r.stderr


def test_a_bad_term_regex_names_the_line_number_not_the_line(tmp_path):
    terms = tmp_path / "terms.txt"
    terms.write_text("# c\n\\bfine\\b\nbroken(regex\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("x\n", encoding="utf-8")
    r = run(["--root", str(tmp_path), "--terms", str(terms), "a.md"])
    assert r.returncode == gate.EXIT_CONFIG
    assert "line 3" in r.stderr
    assert "broken" not in r.stderr


def test_binary_files_and_symlinks_are_skipped(sandbox):
    tree, terms = sandbox
    (tree / "blob.bin").write_bytes(b"widget-corp-login\0\xff")
    (tree / "real.md").write_text("clean\n", encoding="utf-8")
    os.symlink(tree / "real.md", tree / "link.md")
    r = scan(tree, terms)
    assert r.returncode == 0, r.stdout


def test_allow_globs_scope_one_category(tmp_path):
    patterns = tmp_path / "p.txt"
    patterns.write_text("ipv4: \\b(?:\\d{1,3}\\.){3}\\d{1,3}\\b\nallow ipv4 docs/*.md\n", encoding="utf-8")
    tree = tmp_path / "tree"
    (tree / "docs").mkdir(parents=True)
    (tree / "docs" / "a.md").write_text("10.0.0.1\n", encoding="utf-8")
    (tree / "b.md").write_text("10.0.0.1\n", encoding="utf-8")
    r = run(["--root", str(tree), "--patterns", str(patterns), "--terms", os.devnull, "."])
    assert r.returncode == gate.EXIT_FINDINGS
    assert r.stdout.strip() == "b.md:1: ipv4: 10.0.0.1"


def test_quiet_hides_matched_text(sandbox):
    tree, terms = sandbox
    (tree / "a.md").write_text("10.0.0.1\n", encoding="utf-8")
    r = scan(tree, terms, "--quiet")
    assert r.stdout.strip() == "a.md:1: ipv4"


def test_files_from_stdin_scans_only_the_named_files(sandbox):
    tree, terms = sandbox
    (tree / "named.md").write_text("10.0.0.1\n", encoding="utf-8")
    (tree / "other.md").write_text("10.0.0.2\n", encoding="utf-8")
    r = run(["--root", str(tree), "--terms", str(terms), "--files-from", "-", "--quiet"],
            input="named.md\n")
    assert r.stdout.strip() == "named.md:1: ipv4"


EXEMPLARS = [
    ("ipv4", "connect to 10.0.0.1 now"),
    ("uid-literal", "process uid=12345 ran"),
    ("site-hostname", "ssh xyz-acme-login"),
    ("slurm-jobid", "the file slurm-1234567.out"),
    ("big-count", "held 1,234,567,890 entries"),
    ("storage-capacity", "a 7 PiB namespace"),
    ("three-decimal-percent", "stalled 12.345 % of the time"),
    ("snowflake-id", "pid 123456789 was"),
    ("measured-date", "measured on the node 2001-01-01 at noon"),
    ("slack-channel", "posted in #ops-alerts today"),
    ("github-handle", "ping @octocat about it"),
]
NON_EXEMPLARS = ["@pytest.fixture", "#!/bin/sh", "uptime=1000000.0", "# Heading",
                 "version 1.2.3", "about a dozen users", "2026-09-16 accepted"]


@pytest.mark.parametrize("category,text", EXEMPLARS, ids=[c for c, _ in EXEMPLARS])
def test_each_structural_pattern_fires_on_its_exemplar(tmp_path, category, text):
    (tmp_path / "a.md").write_text(text + "\n", encoding="utf-8")
    r = run(["--root", str(tmp_path), "--terms", os.devnull, "a.md"])
    assert r.returncode == gate.EXIT_FINDINGS
    assert ("a.md:1: %s" % category) in r.stdout, r.stdout


@pytest.mark.parametrize("text", NON_EXEMPLARS)
def test_ordinary_text_stays_quiet(tmp_path, text):
    (tmp_path / "a.md").write_text(text + "\n", encoding="utf-8")
    r = run(["--root", str(tmp_path), "--terms", os.devnull, "a.md"])
    assert r.returncode == 0, r.stdout


def test_the_format_exemplar_uses_made_up_terms_and_is_valid():
    terms = gate.load_terms(os.path.join(TOOLS, "forbidden-terms.example.txt"))
    assert len(terms) == 3
    # The exemplar must never be mistaken for the real list: every term in it
    # matches something in its own comment header ("made up") or nowhere in
    # the tree except itself.
    r = run(["--root", ROOT, "--terms", os.path.join(TOOLS, "forbidden-terms.example.txt"), "--quiet"])
    assert r.returncode == 0, r.stdout
