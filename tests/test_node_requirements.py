"""`docs/node-requirements.md` against the tree it describes (ADR-0032).

The table is prose a reader trusts, so the parts of it that can drift from
the code are checked here: every trusted binary has a row, every path a row
names exists, every source is one of the three a requirement may come from,
and a version figure appears only where a row names the feature it is for.
Whether a row's feature is still relied on is a review question.
"""

import json
import os
import re

from conftest import ROOT

DOC = os.path.join(ROOT, "docs", "node-requirements.md")
SCHEMA = os.path.join(ROOT, "schema", "site.schema.json")

SOURCES = {"upstream documentation", "measured on the tool"}
ADR_SOURCE = re.compile(r"^ADR-(\d{4})$")
NONE_STATED = "none stated"

# The rows that state a version. A third is a deliberate change: ADR-0032
# allows a figure only where the code relies on a feature with a known first
# version and there is a clear need to say it, and this set is where that
# judgement is recorded.
VERSIONED = {"logger", "python3"}

HEADER = ["Dependency", "Feature relied on", "Floor", "Source", "Relied on by"]


def _cells(line):
    # A pipe inside a code span is not a column break. None of the cells
    # carries one today; split on unescaped pipes only, so one would not
    # silently shift every column after it.
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def parse(text):
    """Every row of every requirements table, as a dict keyed by HEADER."""
    rows = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("|") and _cells(lines[i]) == HEADER:
            assert set(lines[i + 1].replace("|", "").strip()) <= set("-: "), (
                "a header row not followed by a separator: %r" % lines[i])
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                cells = _cells(lines[i])
                assert len(cells) == len(HEADER), "malformed row: %r" % lines[i]
                rows.append(dict(zip(HEADER, cells)))
                i += 1
        else:
            i += 1
    return rows


def _rows():
    with open(DOC, encoding="utf-8") as fh:
        rows = parse(fh.read())
    assert rows, "no requirements table found in %s" % DOC
    return rows


def _trusted_key(row):
    m = re.search(r"\[trusted_binaries\]\.([a-z0-9_]+)", row["Dependency"])
    return m.group(1) if m else None


def _schema_trusted_binaries():
    with open(SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)
    return set(schema["properties"]["trusted_binaries"]["properties"])


def test_every_trusted_binary_has_a_row():
    keyed = {_trusted_key(r) for r in _rows()} - {None}
    missing = _schema_trusted_binaries() - keyed
    assert not missing, (
        "[trusted_binaries] keys with no row in docs/node-requirements.md: %s"
        % sorted(missing))
    stale = keyed - _schema_trusted_binaries()
    assert not stale, "rows for keys the schema no longer has: %s" % sorted(stale)


def test_every_relied_on_path_exists():
    for row in _rows():
        paths = re.findall(r"`([^`]+)`", row["Relied on by"])
        assert paths, "row %r names nothing that relies on it" % row["Dependency"]
        for path in paths:
            assert os.path.exists(os.path.join(ROOT, path)), (
                "row %r names %s, which does not exist" % (row["Dependency"], path))


def test_every_source_is_one_of_the_three():
    for row in _rows():
        source = row["Source"]
        m = ADR_SOURCE.match(source)
        if m:
            adrs = os.listdir(os.path.join(ROOT, "docs", "adr"))
            assert any(a.startswith(m.group(1) + "-") for a in adrs), (
                "row %r cites %s, which is not an ADR in this tree"
                % (row["Dependency"], source))
        else:
            assert source in SOURCES, (
                "row %r has source %r; a requirement comes only from upstream "
                "documentation, a measurement on the tool, or an ADR (ADR-0032)"
                % (row["Dependency"], source))


def test_a_stated_floor_names_the_feature_it_is_for():
    for row in _rows():
        floor = row["Floor"]
        if floor == NONE_STATED:
            continue
        m = re.match(r"^.*\d+(?:\.\d+)*, for (.+)$", floor)
        assert m, ("row %r states the floor %r; a version is written as "
                   "'<name> <version>, for <feature>'" % (row["Dependency"], floor))
        assert m.group(1) in row["Feature relied on"], (
            "row %r's floor is for %r, which its feature cell does not name"
            % (row["Dependency"], m.group(1)))


def test_only_the_deliberate_rows_state_a_version():
    versioned = set()
    for row in _rows():
        if row["Floor"] == NONE_STATED:
            continue
        key = _trusted_key(row)
        assert key, "row %r states a version but is not a trusted binary" % row["Dependency"]
        versioned.add(key)
    assert versioned == VERSIONED, (
        "rows stating a version: %s; expected %s. A new figure is a decision "
        "(ADR-0032): change VERSIONED here in the same commit, and say why"
        % (sorted(versioned), sorted(VERSIONED)))


def test_the_parser_finds_a_dropped_row():
    """The oracle for the first test: the same table with the awk row gone
    must be reported missing, so the check cannot pass by parsing nothing."""
    with open(DOC, encoding="utf-8") as fh:
        text = fh.read()
    pruned = "\n".join(line for line in text.splitlines()
                       if "`[trusted_binaries].awk`" not in line)
    keyed = {_trusted_key(r) for r in parse(pruned)} - {None}
    assert _schema_trusted_binaries() - keyed == {"awk"}
