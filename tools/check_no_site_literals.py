#!/usr/bin/env python3
"""Refuse site literals in the generic tree (CLAUDE.md "IP hygiene", ADR-0014).

Two lists, one scanner:

  * STRUCTURAL patterns live in the repo (tools/forbidden-patterns.txt): the
    SHAPES of a site fact -- an IPv4 literal, a five-digit uid, a login-node
    hostname shape, a storage capacity, a comma-grouped count.
  * CUSTOMER terms live OUTSIDE the repo -- ~/.config/walk-blocker/
    forbidden-terms.txt locally, the FORBIDDEN_TERMS secret in CI -- because
    a denylist of a customer's hostnames committed to the generic tree would
    itself be the leak. A customer hit is reported by position only; the term,
    the match and the line are never printed, not even on error.

exit 0 clean | 1 findings | 2 config/usage error | 3 --require-terms unmet

A named path that does not exist, a scan that finds no file to read (except
with --files-from, whose list may legitimately name no regular file), and
--files-from combined with positional paths are all exit 2: none of them is a
scan of what the caller meant.

Stdlib only, Python 3.9, so it runs on a bare runner and on a node.
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys

# realpath, not abspath: through a symlinked tools/ the lexical parent is the
# link's directory, not the repository.
HERE = os.path.dirname(os.path.realpath(__file__))
# The default root for a whole-tree scan is the tree this copy of the script
# belongs to, not the current directory: run from the main checkout against a
# worktree's script, "." scanned the main checkout and passed on a tree nobody
# asked about.
REPO = os.path.dirname(HERE)
DEFAULT_PATTERNS = os.path.join(HERE, "forbidden-patterns.txt")
DEFAULT_TERMS = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "walk-blocker", "forbidden-terms.txt")
# A waiver silences STRUCTURAL categories on its own line only, and needs a
# reason of at least eight characters so "ok" is not one. It never silences a
# customer term: those are checked before the waiver is even looked at.
WAIVER = re.compile(r"#\s*site-literal-ok:\s*\S.{7,}")

EXIT_CLEAN, EXIT_FINDINGS, EXIT_CONFIG, EXIT_NO_TERMS = 0, 1, 2, 3


def die(msg):
    sys.stderr.write("check_no_site_literals: %s\n" % msg)
    raise SystemExit(EXIT_CONFIG)


def load_patterns(path):
    """`name: regex` lines define categories; `allow CATEGORY GLOB` (or
    `allow * GLOB`) exempts paths from one category or all of them."""
    rules, allow = [], {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        die("cannot read patterns: %s" % exc)
    except UnicodeDecodeError:
        die("patterns file is not UTF-8: %s" % path)
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("allow "):
            parts = line.split(None, 2)
            if len(parts) != 3:
                die("%s:%d: expected `allow CATEGORY GLOB`" % (path, n))
            allow.setdefault(parts[1], []).append(parts[2])
            continue
        name, sep, regex = line.partition(":")
        if not sep or not name.strip() or not regex.strip():
            # An empty regex matches every line: a silent fail-closed that
            # would flag the whole tree under one category.
            die("%s:%d: expected `name: regex`" % (path, n))
        try:
            rules.append((name.strip(), re.compile(regex.strip())))
        except re.error as exc:
            die("%s:%d: bad regex: %s" % (path, n, exc))
    return rules, allow


def load_terms(path):
    """Compiled case-insensitively. An error names a LINE NUMBER, never the
    line: the whole point of this list is that its contents stay off every
    log and every screen."""
    out = []
    try:
        # utf-8-sig strips a byte-order mark. Without it the first term
        # compiles with U+FEFF glued to its front and never matches, while
        # the summary still says terms=on -- a silent fail-open (review
        # round 2 on the gate's PR). splitlines() already absorbs CRLF.
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        die("cannot read term list: %s" % exc.__class__.__name__)
    except UnicodeDecodeError:
        # A UTF-16 list (BOM ff fe) lands here. The message names neither
        # the path's contents nor the offending bytes.
        die("term list is not UTF-8")
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            try:
                out.append(re.compile(line, re.IGNORECASE))
            except re.error:
                die("term list line %d: bad regex" % n)
    return out


def tracked_files(root):
    try:
        proc = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                              capture_output=True, check=False)
    except OSError as exc:
        die("cannot run git: %s" % exc.__class__.__name__)
    if proc.returncode != 0:
        # Not a repository, or no such root. git's own stderr is not
        # echoed: an exit code and a category are enough for the caller.
        die("git ls-files failed under %s (exit %d); pass paths explicitly or fix --root"
            % (root, proc.returncode))
    return [os.path.join(root, p.decode()) for p in proc.stdout.split(b"\0") if p]


def parse_names(data):
    """The names in a --files-from list: the same names for the same bytes,
    whether they came from a file or from stdin.

    A list holding any NUL byte is NUL-separated (`git ... -z`) and split on
    NUL alone: every other byte, "\\r" and "\\n" included, belongs to a name,
    because a name may legitimately contain either. Otherwise the list is
    newline-separated, and "\\n", "\\r\\n" and a bare "\\r" each end a name --
    the universal-newline reading a file opened in text mode always had, so
    a newline list from a file yields the names it did before. A newline
    list cannot carry a name containing "\\r" or "\\n"; use -z for those. A
    list mixing NUL and newlines is NUL-separated, so its newlines sit inside
    names that do not exist and the scan stops with exit 2, never narrower.

    Empty names are dropped. Names are decoded as the filesystem encodes
    them (os.fsdecode), so a name that is not UTF-8 resolves to its own file
    rather than to a replacement-character name that does not exist."""
    parts = data.split(b"\0") if b"\0" in data else data.splitlines()
    return [os.fsdecode(p) for p in parts if p]


def expand(paths):
    for p in paths:
        if os.path.isdir(p):
            for d, _dirs, files in os.walk(p):
                for f in files:
                    yield os.path.join(d, f)
        else:
            yield p


def is_binary(path):
    with open(path, "rb") as fh:
        return b"\0" in fh.read(8192)


def scan_file(path, rel, rules, allow, terms, quiet):
    found = []
    if os.path.islink(path) or is_binary(path):
        return found
    exempt = {name for name, _rx in rules
              if any(fnmatch.fnmatch(rel, g)
                     for g in allow.get(name, []) + allow.get("*", []))}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if any(rx.search(line) for rx in terms):
                found.append("%s:%d: customer-term" % (rel, lineno))
                # Nothing else about this line is reported. A structural
                # match on the same line could print the very text the
                # customer term matched (review round 1 on the gate's PR).
                continue
            if WAIVER.search(line):
                continue
            for name, rx in rules:
                if name in exempt:
                    continue
                m = rx.search(line)
                if m:
                    shown = "" if quiet else ": " + m.group(0)[:40]
                    found.append("%s:%d: %s%s" % (rel, lineno, name, shown))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*",
                    help="files or directories; default is every tracked file")
    ap.add_argument("--root", default=None,
                    help="tree to scan; default is the repository holding "
                         "this script, or the current directory for named paths "
                         "(not with --files-from, which uses the repository)")
    ap.add_argument("--patterns", default=DEFAULT_PATTERNS)
    ap.add_argument("--terms", default=None,
                    help="customer term list; default $WALK_BLOCKER_FORBIDDEN_TERMS, "
                         "then ~/.config/walk-blocker/forbidden-terms.txt if present")
    ap.add_argument("--require-terms", action="store_true",
                    help="exit 3 instead of scanning structurally only")
    ap.add_argument("--files-from", help="NUL- or newline-separated list, - for stdin")
    ap.add_argument("--quiet", action="store_true", help="never print matched text")
    a = ap.parse_args(argv)
    if a.files_from and a.paths:
        # Issue #193: --files-from used to win and the positional paths were
        # never read, so a file the caller named passed unscanned. Scanning
        # both would mix two roots (git's repo-relative names and the shell's
        # cwd-relative ones); refusing is the smaller surprise. argparse's
        # error() exits 2, which is EXIT_CONFIG.
        ap.error("--files-from and positional paths cannot be combined; "
                 "put every name in the list, or name them all as arguments")
    # Paths named on the command line mean what they mean to the caller's
    # shell, so without --root they resolve against the current directory.
    # A whole-tree scan and --files-from (git's repo-relative names) use REPO.
    if a.root:
        root = os.path.abspath(a.root)
    elif a.paths and not a.files_from:
        root = os.getcwd()
    else:
        root = REPO

    rules, allow = load_patterns(a.patterns)
    terms_path = (a.terms or os.environ.get("WALK_BLOCKER_FORBIDDEN_TERMS")
                  or (DEFAULT_TERMS if os.path.exists(DEFAULT_TERMS) else None))
    terms = load_terms(terms_path) if terms_path else []
    if a.require_terms and not terms:
        sys.stderr.write("check_no_site_literals: no customer term list "
                         "(--terms, $WALK_BLOCKER_FORBIDDEN_TERMS or %s)\n" % DEFAULT_TERMS)
        return EXIT_NO_TERMS

    if a.files_from:
        # Both sources are read as bytes and parsed by one function. Issue
        # #216: stdin used to be read as text with no newline translation and
        # a file with universal newlines, so a CRLF list kept "\r" on every
        # name from stdin only.
        if a.files_from == "-":
            data = sys.stdin.buffer.read()
        else:
            try:
                with open(a.files_from, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                die("cannot read --files-from %s: %s" % (a.files_from, exc.__class__.__name__))
        named = [os.path.join(root, p) for p in parse_names(data)]
        files = named
    elif a.paths:
        named = [os.path.join(root, p) for p in a.paths]
        files = None
    else:
        named = []
        files = tracked_files(root)

    # Issue #192: a name the caller gave that is not there used to be counted
    # as scanned and pass, so a typo read as a clean bill. lexists, not
    # exists: a named symlink is there, and scan_file skips it by policy.
    missing = [p for p in named if not os.path.lexists(p)]
    if missing:
        die("%d named path(s) do not exist under %s, first: %s"
            % (len(missing), root, os.path.relpath(missing[0], root)))
    if files is None:
        files = list(expand(named))

    findings, scanned = [], 0
    for f in files:
        # A symlink is skipped by policy and read by nothing, so it is not
        # counted: a tree holding only links read no file and must not pass.
        if os.path.isfile(f) and not os.path.islink(f):
            scanned += 1
            findings.extend(scan_file(f, os.path.relpath(f, root), rules, allow,
                                      terms, a.quiet))
    if scanned == 0 and not a.files_from:
        # A whole-tree scan, or named directories, that found nothing to read
        # is a scan of the wrong tree (a copy of tools/ inside another
        # repository, an empty payload directory), not a pass. Any
        # --files-from list is exempt, empty or not: the pre-commit hook's
        # list for a commit that only deletes is empty, and for one that only
        # changes symlinks or a submodule pointer it names no regular file.
        die("no files to scan under %s; pass paths explicitly or fix --root" % root)
    # A name os.fsdecode could not decode as UTF-8 carries surrogates that a
    # strict stdout refuses to encode; the traceback would exit 1 with the
    # rest of the findings unprinted. stderr is already backslashreplace.
    if findings and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    for line in findings:
        print(line)
    # The root is named so a pass says which tree it passed. The count is of
    # files actually read, not of names offered.
    sys.stderr.write("%d file(s) under %s, %d finding(s), terms=%s\n"
                     % (scanned, root, len(findings), "on" if terms else "off"))
    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
