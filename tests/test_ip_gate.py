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
        pytest.skip("no customer term list on this machine; CI's ip-hygiene-terms job enforces it")
    assert r.returncode == 0, r.stdout
    assert r.stderr.strip().endswith("terms=on")


def test_the_default_root_is_the_scripts_repository_not_the_cwd(tmp_path):
    # Issue #164: "." as the default scanned whatever directory the caller
    # stood in. From an unrelated, empty directory the gate must still scan
    # this repository, and say so.
    r = run(["--terms", os.devnull], cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    # The summary counts files read: tracked regular files, not links.
    names = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"],
                           capture_output=True, check=True).stdout.split(b"\0")
    tracked = sum(1 for n in names if n and os.path.isfile(os.path.join(ROOT, n.decode()))
                  and not os.path.islink(os.path.join(ROOT, n.decode())))
    assert r.stderr.strip() == "%d file(s) under %s, 0 finding(s), terms=off" % (
        tracked, os.path.realpath(ROOT))


def test_a_symlinked_tools_dir_still_finds_the_repository(tmp_path):
    # The script's directory is resolved, not taken lexically: through a link
    # the lexical parent of tools/ is tmp_path, which is not a repository.
    (tmp_path / "linked").symlink_to(TOOLS)
    r = subprocess.run([sys.executable, str(tmp_path / "linked" / "check_no_site_literals.py"),
                        "--terms", os.devnull], capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert " under %s, " % os.path.realpath(ROOT) in r.stderr


def test_named_paths_without_root_are_read_from_the_cwd(tmp_path):
    # A relative path is the caller's, not the script's repository's: read
    # against REPO it named a file that is not there and passed.
    (tmp_path / "a.md").write_text("plain\n")
    r = run(["--terms", os.devnull, "a.md"], cwd=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stderr.strip() == "1 file(s) under %s, 0 finding(s), terms=off" % tmp_path


def test_files_from_without_root_reads_names_against_the_repository(tmp_path):
    # --files-from carries git's repo-relative names, so it keeps REPO as the
    # root, whatever the current directory.
    (tmp_path / "list").write_text("README.md\n")
    r = run(["--terms", os.devnull, "--files-from", "list"], cwd=str(tmp_path))
    # Only the root is asserted: README.md's content is the tree test's job.
    assert r.stderr.strip().startswith("1 file(s) under %s, " % os.path.realpath(ROOT)), (
        r.stdout + r.stderr)


def test_files_from_with_positional_paths_is_a_usage_error(tmp_path):
    # Issue #193: the positional path used to be dropped without a word, so a
    # file the caller named passed unscanned.
    (tmp_path / "list").write_text("a.md\n")
    (tmp_path / "a.md").write_text("plain\n")
    (tmp_path / "b.md").write_text("10.0.0.1\n")
    r = run(["--root", str(tmp_path), "--terms", os.devnull, "--files-from", "list", "b.md"])
    assert r.returncode == gate.EXIT_CONFIG, r.stdout + r.stderr
    assert "cannot be combined" in r.stderr
    assert r.stdout == ""


@pytest.mark.parametrize("how", ["positional", "files-from"])
def test_a_named_path_that_does_not_exist_is_a_config_error(tmp_path, how):
    # Issue #192: a mistyped name was counted as scanned and passed.
    (tmp_path / "a.md").write_text("plain\n")
    if how == "positional":
        args = ["a.md", "no-such-file.md"]
    else:
        (tmp_path / "list").write_text("a.md\nno-such-file.md\n")
        args = ["--files-from", str(tmp_path / "list")]
    r = run(["--root", str(tmp_path), "--terms", os.devnull] + args)
    assert r.returncode == gate.EXIT_CONFIG, r.stdout + r.stderr
    assert "do not exist" in r.stderr and "no-such-file.md" in r.stderr
    assert "Traceback" not in r.stderr


def test_a_named_dangling_symlink_exists_and_is_skipped(tmp_path):
    # lexists, not exists: a link is there, and links are skipped by policy.
    (tmp_path / "a.md").write_text("plain\n")
    os.symlink(tmp_path / "gone.md", tmp_path / "link.md")
    r = run(["--root", str(tmp_path), "--terms", os.devnull, "a.md", "link.md"])
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("case", ["untracked-tree", "empty-dir", "tracked-symlink-only",
                                  "named-symlink-only"])
def test_a_scan_that_finds_no_files_is_not_a_pass(tmp_path, case):
    # Issue #192: a copy of tools/ in a repository that tracks nothing under
    # the root reported "0 file(s)" and exited 0. A symlink is skipped unread,
    # so a tree or a name list holding only links read nothing either.
    (tmp_path / "target.md").write_text("plain\n")
    if case == "untracked-tree":
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        args = ["--root", str(tmp_path)]
    elif case == "empty-dir":
        (tmp_path / "empty").mkdir()
        args = ["--root", str(tmp_path), "empty"]
    else:
        os.symlink("target.md", str(tmp_path / "link.md"))
        if case == "tracked-symlink-only":
            subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
            subprocess.run(["git", "-C", str(tmp_path), "add", "link.md"], check=True)
            args = ["--root", str(tmp_path)]
        else:
            args = ["--root", str(tmp_path), "link.md"]
    r = run(args + ["--terms", os.devnull])
    assert r.returncode == gate.EXIT_CONFIG, r.stdout + r.stderr
    assert "no files to scan" in r.stderr
    # The refusal is not also a pass line: no "0 file(s) ... 0 finding(s)".
    assert "file(s) under" not in r.stderr


@pytest.mark.parametrize("names", ["", "sub\n", "link.md\n"],
                         ids=["empty", "only-a-directory", "only-a-symlink"])
def test_a_files_from_list_that_reads_no_file_is_still_a_pass(tmp_path, names):
    # The pre-commit hook's list is empty for a commit that only deletes, and
    # names no regular file for one that only changes a submodule pointer or a
    # symlink. Unlike a whole-tree scan, reading nothing here is not a refusal.
    (tmp_path / "sub").mkdir()
    os.symlink(tmp_path / "gone.md", tmp_path / "link.md")
    r = run(["--root", str(tmp_path), "--terms", os.devnull, "--files-from", "-"], input=names)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stderr.strip() == "0 file(s) under %s, 0 finding(s), terms=off" % tmp_path


def test_the_summary_counts_files_read_not_names_offered(tmp_path):
    # A directory named in --files-from (a submodule's gitlink, say) is not a
    # file read.
    (tmp_path / "a.md").write_text("plain\n")
    (tmp_path / "sub").mkdir()
    r = run(["--root", str(tmp_path), "--terms", os.devnull, "--files-from", "-"],
            input="a.md\nsub\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stderr.strip().startswith("1 file(s) under ")


def test_the_summary_names_the_root_it_scanned(tmp_path):
    (tmp_path / "a.md").write_text("plain\n")
    r = run(["--root", str(tmp_path), "--terms", os.devnull, "a.md"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert " under %s, " % tmp_path in r.stderr


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


def test_a_line_hitting_both_lists_reports_the_customer_term_and_nothing_else(sandbox):
    # Without --quiet a structural match prints its text; on a line a
    # customer term also matched, that text may BE the customer term.
    tree, terms = sandbox
    (tree / "a.md").write_text("connect to widget-corp-login at 10.0.0.1\n", encoding="utf-8")
    r = scan(tree, terms)
    assert r.returncode == gate.EXIT_FINDINGS
    assert r.stdout.strip() == "a.md:1: customer-term"
    assert "10.0.0.1" not in r.stdout + r.stderr


@pytest.mark.parametrize("prefix,newline", [(b"\xef\xbb\xbf", b"\n"), (b"", b"\r\n"),
                                            (b"\xef\xbb\xbf", b"\r\n")],
                         ids=["bom", "crlf", "bom+crlf"])
def test_a_term_file_with_a_bom_or_crlf_still_matches(tmp_path, prefix, newline):
    # Either shape is what an editor on another platform produces. The first
    # term must not compile with U+FEFF or a carriage return glued on.
    terms = tmp_path / "terms.txt"
    terms.write_bytes(prefix + b"widget-corp-login" + newline + b"other-made-up" + newline)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.md").write_text("see widget-corp-login\n", encoding="utf-8")
    r = scan(tree, terms)
    assert r.returncode == gate.EXIT_FINDINGS, r.stderr
    assert r.stdout.strip() == "a.md:1: customer-term"


def test_a_non_utf8_term_file_is_a_clean_config_error(tmp_path):
    terms = tmp_path / "terms.txt"
    terms.write_bytes("widget-corp-login\n".encode("utf-16"))
    (tmp_path / "a.md").write_text("x\n", encoding="utf-8")
    r = run(["--root", str(tmp_path), "--terms", str(terms), "a.md"])
    assert r.returncode == gate.EXIT_CONFIG
    assert "Traceback" not in r.stderr
    assert "not UTF-8" in r.stderr
    assert "widget" not in r.stderr


def test_an_empty_pattern_regex_is_refused_not_matched_everywhere(tmp_path):
    patterns = tmp_path / "p.txt"
    patterns.write_text("empty: \n", encoding="utf-8")
    (tmp_path / "a.md").write_text("anything\n", encoding="utf-8")
    r = run(["--root", str(tmp_path), "--patterns", str(patterns), "--terms", os.devnull, "a.md"])
    assert r.returncode == gate.EXIT_CONFIG
    assert "expected `name: regex`" in r.stderr


@pytest.mark.parametrize("case", ["not-a-repo", "missing-root", "no-git", "files-from-missing", "files-from-dir"])
def test_a_broken_environment_is_a_clean_config_error(tmp_path, case):
    # Exit 1 means findings. A broken environment must never read as one,
    # and must never print a traceback.
    env = {**os.environ, "WALK_BLOCKER_FORBIDDEN_TERMS": "", "XDG_CONFIG_HOME": str(tmp_path / "nocfg")}
    if case == "not-a-repo":
        (tmp_path / "a.md").write_text("x\n", encoding="utf-8")
        args = ["--root", str(tmp_path)]
    elif case == "missing-root":
        args = ["--root", str(tmp_path / "nope")]
    elif case == "no-git":
        args = ["--root", str(tmp_path)]
        env["PATH"] = str(tmp_path / "emptybin")
    elif case == "files-from-missing":
        args = ["--root", str(tmp_path), "--files-from", str(tmp_path / "nope.txt")]
    else:
        args = ["--root", str(tmp_path), "--files-from", str(tmp_path)]
    r = run(args + ["--terms", os.devnull], env=env)
    assert r.returncode == gate.EXIT_CONFIG, (r.returncode, r.stderr)
    assert "Traceback" not in r.stderr


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


def run_list(tree, terms, data, via):
    """Run the gate over a --files-from list given as raw bytes, from a file
    or from stdin. Bytes, not text: the test is about what the bytes say."""
    args = [sys.executable, os.path.join(TOOLS, "check_no_site_literals.py"),
            "--root", str(tree), "--terms", str(terms), "--quiet", "--files-from"]
    if via == "file":
        lst = tree.parent / "list"
        lst.write_bytes(data)
        return subprocess.run(args + [str(lst)], capture_output=True)
    return subprocess.run(args + ["-"], input=data, capture_output=True)


@pytest.mark.parametrize("data", [b"a.md\nb.md\n", b"a.md\r\nb.md\r\n", b"a.md\r\nb.md",
                                  b"a.md\rb.md\r", b"a.md\0b.md\0", b"a.md\0b.md"],
                         ids=["lf", "crlf", "crlf-unterminated", "bare-cr", "nul", "nul-unterminated"])
def test_files_from_reads_the_same_names_from_a_file_and_from_stdin(sandbox, data):
    # Issue #216: stdin kept "\r" on every name of a CRLF list, so the same
    # list named two files read from a file and two missing paths from stdin.
    # Both sources must now read both files and say the same thing about them.
    tree, terms = sandbox
    (tree / "a.md").write_text("10.0.0.1\n", encoding="utf-8")
    (tree / "b.md").write_text("10.0.0.2\n", encoding="utf-8")
    (tree / "c.md").write_text("10.0.0.3\n", encoding="utf-8")
    by_file = run_list(tree, terms, data, "file")
    by_stdin = run_list(tree, terms, data, "stdin")
    for r in (by_file, by_stdin):
        assert r.returncode == gate.EXIT_FINDINGS, r.stderr
        assert r.stdout.decode().splitlines() == ["a.md:1: ipv4", "b.md:1: ipv4"]
        assert r.stderr.decode().startswith("2 file(s) under ")
    assert (by_file.returncode, by_file.stdout, by_file.stderr) == \
        (by_stdin.returncode, by_stdin.stdout, by_stdin.stderr)


@pytest.mark.parametrize("via", ["file", "stdin"])
def test_a_nul_separated_list_keeps_cr_and_lf_inside_a_name(sandbox, via):
    # The pre-commit hook's `git diff -z` list is exact: a name may contain
    # "\r" or "\n", and splitting there would turn one real file into two
    # names that do not exist.
    tree, terms = sandbox
    (tree / "cr\rname.md").write_text("10.0.0.1\n", encoding="utf-8")
    (tree / "lf\nname.md").write_text("10.0.0.2\n", encoding="utf-8")
    r = run_list(tree, terms, b"cr\rname.md\0lf\nname.md\0", via)
    assert r.returncode == gate.EXIT_FINDINGS, r.stderr
    assert r.stderr.decode().startswith("2 file(s) under ")


def test_parse_names_splits_one_way_per_list():
    assert gate.parse_names(b"") == []
    assert gate.parse_names(b"a\r\nb\rc\n\nd") == ["a", "b", "c", "d"]
    # Any NUL makes the whole list NUL-separated, and nothing else splits it.
    assert gate.parse_names(b"a\r\nb\0c\rd\0") == ["a\r\nb", "c\rd"]
    # A name that is not UTF-8 keeps its own bytes.
    assert os.fsencode(gate.parse_names(b"\xff.md\n")[0]) == b"\xff.md"


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
                 "version 1.2.3", "about a dozen users", "2026-09-16 accepted",
                 "listen on 127.0.0.1:8080", "bind to 0.0.0.0", "an example host 192.0.2.10",
                 "use the `@dataclass` decorator", "OnCalendar=*:07,17,27,37,47,57:30",
                 "user-1000.slice", "--time 1-00:00:00"]


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
