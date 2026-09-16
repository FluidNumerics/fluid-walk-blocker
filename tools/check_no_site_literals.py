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

Stdlib only, Python 3.9, so it runs on a bare runner and on a node.
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
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
        if not sep or not name.strip():
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
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            try:
                out.append(re.compile(line, re.IGNORECASE))
            except re.error:
                die("term list line %d: bad regex" % n)
    return out


def tracked_files(root):
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"],
                         capture_output=True, check=True).stdout
    return [os.path.join(root, p.decode()) for p in out.split(b"\0") if p]


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
    ap.add_argument("--root", default=".")
    ap.add_argument("--patterns", default=DEFAULT_PATTERNS)
    ap.add_argument("--terms", default=None,
                    help="customer term list; default $WALK_BLOCKER_FORBIDDEN_TERMS, "
                         "then ~/.config/walk-blocker/forbidden-terms.txt if present")
    ap.add_argument("--require-terms", action="store_true",
                    help="exit 3 instead of scanning structurally only")
    ap.add_argument("--files-from", help="NUL- or newline-separated list, - for stdin")
    ap.add_argument("--quiet", action="store_true", help="never print matched text")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)

    rules, allow = load_patterns(a.patterns)
    terms_path = (a.terms or os.environ.get("WALK_BLOCKER_FORBIDDEN_TERMS")
                  or (DEFAULT_TERMS if os.path.exists(DEFAULT_TERMS) else None))
    terms = load_terms(terms_path) if terms_path else []
    if a.require_terms and not terms:
        sys.stderr.write("check_no_site_literals: no customer term list "
                         "(--terms, $WALK_BLOCKER_FORBIDDEN_TERMS or %s)\n" % DEFAULT_TERMS)
        return EXIT_NO_TERMS

    if a.files_from:
        data = sys.stdin.read() if a.files_from == "-" else open(a.files_from).read()
        files = [os.path.join(root, p) for p in re.split(r"[\0\n]", data) if p]
    elif a.paths:
        files = list(expand([os.path.join(root, p) for p in a.paths]))
    else:
        files = tracked_files(root)

    findings = []
    for f in files:
        if os.path.isfile(f):
            findings.extend(scan_file(f, os.path.relpath(f, root), rules, allow,
                                      terms, a.quiet))
    for line in findings:
        print(line)
    sys.stderr.write("%d file(s), %d finding(s), terms=%s\n"
                     % (len(files), len(findings), "on" if terms else "off"))
    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
