"""The runbook's exit-code table lists every status `deploy.py` can return.

A wrapper or an operator branching on `deploy.py`'s status reads one table in
`docs/operating.md` (issue #106). The codes are read out of the source with
`ast`, not listed here: every integer a function returns (alone, or first in
a tuple), every integer `SystemExit` is raised with, and every module-level
integer constant a function returns by name. A new code, as a literal or as a
named constant, fails here until it has a row.

Two statuses are not integers in the source. argparse exits 2 on a usage
error, and `run()` and the `install.sh` call pass a command's own status
through `SystemExit`; each has a row of its own, pinned below.
"""
import ast
import os
import re

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


def _tree():
    with open(DEPLOY_PY, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _int_constants(tree):
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            names = target.elts if isinstance(target, ast.Tuple) else [target]
            values = (node.value.elts if isinstance(node.value, ast.Tuple)
                      else [node.value])
            for name, value in zip(names, values):
                if (isinstance(name, ast.Name) and isinstance(value, ast.Constant)
                        and type(value.value) is int):
                    found[name.id] = value.value
    return found


def codes_by_function():
    """{function name: set of integer statuses it returns or raises}, and
    the functions that pass a non-literal status to SystemExit."""
    tree = _tree()
    constants = _int_constants(tree)
    codes, passthrough = {}, set()

    def as_code(node):
        if isinstance(node, ast.Tuple) and node.elts:
            node = node.elts[0]
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        return None

    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Return) and node.value is not None:
                code = as_code(node.value)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "SystemExit" and node.args):
                code = as_code(node.args[0])
                if code is None:
                    passthrough.add(func.name)
            else:
                continue
            if code is not None:
                codes.setdefault(func.name, set()).add(code)
    return codes, passthrough


def table_rows():
    with open(OPERATING, encoding="utf-8") as fh:
        text = fh.read()
    section = text[text.index(HEADING):]
    section = section[:section.index("\n## ")]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("|") or cells[0] in ("Exit", "---"):
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
