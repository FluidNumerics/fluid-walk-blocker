"""The runbook's exit-code table lists every status `deploy.py` can return.

A wrapper or an operator branching on `deploy.py`'s status reads one table in
`docs/operating.md` (issue #106). The codes are read out of the source with
`ast`, not listed here. A status counts when it is an integer that a function
returns, alone or first in a tuple, or passes to `SystemExit`, `sys.exit`,
`os._exit` or `exit`, written as a literal, as either branch of a conditional
expression or an operand of `or`/`and`, as a module-level integer constant
(plain or annotated), or as a name the same function assigns an integer
literal. A new code in any of those shapes fails here until it has a row;
`test_the_scanner_sees_each_shape_a_new_status_can_take` pins each shape.
A status computed at run time from something else is beyond any static
scan, and is the reason the table is also checked by reading.

Two statuses are not integers in the source. argparse exits 2 on a usage
error, and `run()` and the `install.sh` call pass a command's own status
through `SystemExit`; each has a row of its own, pinned below.
"""
import ast
import os
import re

import pytest

from conftest import ROOT

DEPLOY_PY = os.path.join(ROOT, "node", "deploy.py")
OPERATING = os.path.join(ROOT, "docs", "operating.md")
HEADING = "### What `deploy.py` exits with"
PASSTHROUGH = "a command's own status"

# The function each mode enters through, for the per-mode check.
MODE_ENTRIES = {
    "system_execute": "`--system`",
    "system_preview": "`--system --dry-run`",
    "system_uninstall": "`--uninstall`",
    "system_verify": "`--verify`",
}

# Calls that end the process with their first argument as the status.
EXITS = {("SystemExit",), ("exit",), ("sys", "exit"), ("os", "_exit")}


def _int_literal(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    return None


def _assigned_ints(statements):
    """{name: {ints}} for every name assigned an integer literal, by plain,
    tuple or annotated assignment, anywhere in `statements`' subtrees."""
    found = {}

    def note(target, value):
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
            for t, v in zip(target.elts, value.elts):
                note(t, v)
        elif isinstance(target, ast.Name) and _int_literal(value) is not None:
            found.setdefault(target.id, set()).add(_int_literal(value))

    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    note(target, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                note(node.target, node.value)
    return found


def _call_name(func):
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        return tuple(reversed(parts))
    return None


def codes_by_function(source=None):
    """{function name: set of integer statuses it returns or exits with},
    and the functions that exit with a status no static read can name."""
    if source is None:
        with open(DEPLOY_PY, encoding="utf-8") as fh:
            source = fh.read()
    tree = ast.parse(source)
    # Module level only: a function's own assignments are added per function.
    module = _assigned_ints([n for n in tree.body
                             if isinstance(n, (ast.Assign, ast.AnnAssign))])
    codes, passthrough = {}, set()

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _assigned_ints(func.body)

        def as_codes(node):
            if isinstance(node, ast.Tuple) and node.elts:
                node = node.elts[0]
            if isinstance(node, ast.IfExp):
                return as_codes(node.body) | as_codes(node.orelse)
            if isinstance(node, ast.BoolOp):
                return set().union(*(as_codes(v) for v in node.values))
            if _int_literal(node) is not None:
                return {_int_literal(node)}
            if isinstance(node, ast.Name):
                return local.get(node.id, set()) | module.get(node.id, set())
            return set()

        for node in ast.walk(func):
            if isinstance(node, ast.Return) and node.value is not None:
                found = as_codes(node.value)
            elif (isinstance(node, ast.Call) and node.args
                  and _call_name(node.func) in EXITS):
                found = as_codes(node.args[0])
                if not found:
                    passthrough.add(func.name)
            else:
                continue
            if found:
                codes.setdefault(func.name, set()).update(found)
    return codes, passthrough


COLUMNS = 4


def table_rows(text=None):
    """The table's body rows, read the way a Markdown renderer reads them.

    The table is the first run of consecutive lines that start with `|` after
    the heading, and it ends at the first line that does not. A blank line,
    an HTML comment or a code fence ends a rendered table there too, so a row
    past one of them is not a row an operator sees. A plain text line right
    after a row does not end it: GFM renders that line as one more row. The
    reader stops there all the same, which can only drop rows, so the test
    fails loudly on that shape rather than passing a status without a row.
    Every row must have
    exactly COLUMNS cells. A cell splits at a bare `|`, never at a `\\|`,
    which GFM renders as a pipe inside the cell.
    """
    if text is None:
        with open(OPERATING, encoding="utf-8") as fh:
            text = fh.read()
    assert HEADING in text, "docs/operating.md has no %r heading" % HEADING
    section = text[text.index(HEADING):]
    section = section[:section.index("\n## ")]
    lines = section.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("|")),
                 None)
    assert start is not None, "no table under %r" % HEADING
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in
                 re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        assert len(cells) == COLUMNS, (
            "exit-code table row has %d cells, not %d: %s"
            % (len(cells), COLUMNS, line))
        if cells[0] in ("Exit", "---"):
            continue
        rows.append(cells)
    return rows


def _codes_in_table():
    return {int(r[0]) for r in table_rows() if re.fullmatch(r"\d+", r[0])}


def test_the_source_scan_finds_the_codes_we_know_are_there():
    codes, passthrough = codes_by_function()
    every = set().union(*codes.values())
    assert {0, 3, 4, 6, 10} <= every
    assert {"run", "system_execute"} <= passthrough


def test_every_status_deploy_py_returns_has_a_row_and_every_row_is_one():
    codes, _ = codes_by_function()
    in_source = set().union(*codes.values()) | {2}
    in_table = _codes_in_table()
    assert in_source - in_table == set(), (
        "deploy.py can exit with %s, which docs/operating.md's exit-code "
        "table does not list" % sorted(in_source - in_table))
    assert in_table - in_source == set(), (
        "the exit-code table lists %s, which deploy.py never returns"
        % sorted(in_table - in_source))


def test_each_mode_has_a_row_for_every_code_its_entry_point_returns():
    codes, _ = codes_by_function()
    rows = table_rows()
    for func, mode in MODE_ENTRIES.items():
        for code in codes[func]:
            assert any(r[0] == str(code)
                       and (mode in r[1].split(", ") or r[1] == "every mode")
                       for r in rows), (
                "%s returns %d, and no row lists it for %s" % (func, code, mode))


def test_argparse_and_a_command_s_own_status_have_their_rows():
    with open(DEPLOY_PY, encoding="utf-8") as fh:
        source = fh.read()
    assert "argparse.ArgumentParser(" in source and "parser.error(" in source
    assert any(r[0] == "2" for r in table_rows())
    _, passthrough = codes_by_function()
    if passthrough:
        assert any(r[0] == PASSTHROUGH for r in table_rows())


# Each shape a new status can take in deploy.py. The scanner must find 13 in
# every one: a shape it cannot see is a new code that passes without a row.
NEW_STATUS_SHAPES = {
    "literal": "def f():\n    return 13\n",
    "first of a tuple": "def f():\n    return 13, []\n",
    "conditional": "def f(x):\n    return 6 if x else 13\n",
    "or": "def f(rc):\n    return rc or 13\n",
    "module constant": "NEW = 13\ndef f():\n    return NEW\n",
    "annotated module constant": "NEW: int = 13\ndef f():\n    return NEW\n",
    "tuple-assigned constant": "A, NEW = 0, 13\ndef f():\n    return NEW\n",
    "local name": "def f():\n    code = 13\n    return code\n",
    "local name in a tuple": "def f():\n    rc = 13\n    return rc, []\n",
    "nested function": "def f():\n    def g():\n        return 13\n    return g()\n",
    "SystemExit": "def f():\n    raise SystemExit(13)\n",
    "SystemExit of a local name": "def f():\n    n = 13\n    raise SystemExit(n)\n",
    "sys.exit": "import sys\ndef f():\n    sys.exit(13)\n",
    "os._exit": "import os\ndef f():\n    os._exit(13)\n",
    "exit": "def f():\n    exit(13)\n",
}


@pytest.mark.parametrize("shape", sorted(NEW_STATUS_SHAPES))
def test_the_scanner_sees_each_shape_a_new_status_can_take(shape):
    codes, _ = codes_by_function(NEW_STATUS_SHAPES[shape])
    assert 13 in set().union(*codes.values()), (
        "a new status written as %r is invisible to the scan" % shape)


def test_a_status_the_scan_cannot_name_is_a_passthrough():
    _, passthrough = codes_by_function(
        "import sys\ndef f(r):\n    sys.exit(r.returncode)\n")
    assert passthrough == {"f"}


def test_a_row_the_renderer_would_not_show_is_not_a_row():
    with open(OPERATING, encoding="utf-8") as fh:
        text = fh.read()
    ten = next(line for line in text.splitlines() if line.startswith("| 10 |"))
    for hidden in ("<!--\n%s\n-->" % ten, "```\n%s\n```" % ten,
                   "\nA paragraph.\n\n%s" % ten, "\n%s" % ten):
        rows = table_rows(text.replace(ten, hidden))
        assert not any(r[0] == "10" for r in rows), hidden


def test_a_bare_pipe_inside_a_cell_is_refused():
    with open(OPERATING, encoding="utf-8") as fh:
        text = fh.read()
    one = next(line for line in text.splitlines() if line.startswith("| 1 |"))
    with pytest.raises(AssertionError, match="cells"):
        table_rows(text.replace(one, one.replace("drift:", "`a | b` drift:")))


def test_an_escaped_pipe_stays_inside_its_cell():
    with open(OPERATING, encoding="utf-8") as fh:
        text = fh.read()
    one = next(line for line in text.splitlines() if line.startswith("| 1 |"))
    escaped = one.replace("drift:", "`a\\|b` drift:")
    rows = table_rows(text.replace(one, escaped))
    assert [r[1] for r in rows if r[0] == "1"] == ["`--verify`"]


def test_a_doc_without_the_heading_names_the_missing_heading():
    with open(OPERATING, encoding="utf-8") as fh:
        text = fh.read()
    with pytest.raises(AssertionError, match="has no .* heading"):
        table_rows(text.replace(HEADING, "### Something else"))


def test_a_heading_with_no_table_under_it_names_the_missing_table():
    text = "%s\n\nThe table went missing.\n\n## Next\n" % HEADING
    with pytest.raises(AssertionError, match="no table under"):
        table_rows(text)
