"""The runbook's lists of what the node does are the lists the code has.

`docs/operating.md` describes three things an operator checks a node
against: the `audit_dir` states the reconcile journals, when it writes
`coverage_change`, and what `deploy.py --uninstall` undoes. Each was found
short of the code (issue #89, issue #99), and a reader checking a node
against a short list reads the missing item as a fault. Where the code's set
can be read out of the source it is, so a state added to `install.sh`
without a runbook entry fails here.
"""
import os
import re

from conftest import ROOT

OPERATING = os.path.join(ROOT, "docs", "operating.md")
INSTALL_SH = os.path.join(ROOT, "node", "shim", "install.sh")
DEPLOY_PY = os.path.join(ROOT, "node", "deploy.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _section(text, heading):
    start = text.index("\n## " + heading)
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def _joined(text):
    return " ".join(text.split())


def _reconcile_bullet():
    text = _read(OPERATING)
    start = text.index("- the **reconcile's reports**")
    return _joined(text[start:text.index("\n- ", start + 1)])


def audit_dir_states():
    """Every STATE install.sh can journal under `audit_dir`: the literal
    ones, and the words spool_fit() hands it through `$sg_spool_why`."""
    sh = _read(INSTALL_SH)
    states = set(re.findall(r"sg_report audit_dir ([a-z-]+)", sh))
    assert 'sg_report audit_dir "$sg_spool_why"' in sh
    body = sh[sh.index("spool_fit() {"):]
    body = body[:body.index("\n}\n")]
    states |= set(re.findall(r"sg_spool_why=([a-z-]+)", body))
    return states


def test_the_state_scan_finds_the_states_we_know_are_there():
    assert {"absent", "unmarked", "mode-corrected", "symlink"} <= audit_dir_states()


def test_every_audit_dir_state_the_reconcile_journals_is_in_the_runbook():
    bullet = _reconcile_bullet()
    missing = sorted(s for s in audit_dir_states() if "`%s`" % s not in bullet)
    assert not missing, (
        "install.sh journals audit_dir states the runbook's reconcile bullet "
        "does not name: %s" % ", ".join(missing))


def test_the_runbook_says_coverage_change_is_written_only_when_coverage_shrinks():
    sh = _read(INSTALL_SH)
    calls = re.findall(r"^(.*)\n\s*sg_report coverage_change", sh, re.M)
    assert calls == ['    if [ "$((_dropped + SWEPT_N))" -gt 0 ]; then'], calls
    clause = _reconcile_bullet().split("`coverage_change`", 1)[1].split("`relink_refused`")[0]
    assert "shrank" in clause
    assert "no record" in clause


def _uninstall_paragraph():
    return _joined(next(p for p in _section(_read(OPERATING), "14.").split("\n\n")
                        if "It reverses the install" in p))


def test_the_runbook_lists_the_journal_revocation_the_uninstall_performs():
    body = _read(DEPLOY_PY)
    body = body[body.index("def system_uninstall("):]
    assert "journal_revoke_command(" in body and '"rm", "-f", dropin' in body
    para = _uninstall_paragraph()
    assert "journal drop-in" in para and "revokes" in para and "ADR-0026" in para


def test_the_runbook_does_not_say_the_uninstall_removes_the_prefix():
    sh = _read(INSTALL_SH)
    arm = sh[sh.index("    uninstall)\n"):]
    arm = arm[:arm.index("\nesac")]
    # The arm removes $BIN and touches nothing else under the prefix.
    assert 'rmdir "$BIN"' in arm and "$PREFIX" not in arm
    para = _uninstall_paragraph()
    assert "and removes the prefix" not in para
    assert "`<prefix>/bin`" in para


def test_the_runbook_says_a_best_effort_hook_is_written_only_when_its_shell_resolves():
    sh = _read(INSTALL_SH)
    assert re.search(r'for _wh in \$SG_HOOKS_BEST_EFFORT; do\n\s*if command -v "\$_wh"', sh)
    text = _read(OPERATING)
    start = text.index("What it writes, all root-owned")
    writes = _joined(text[start:text.index("What it verifies", start)])
    hooks = next(item for item in writes.split(" - ") if "hook block" in item)
    assert "best-effort" in hooks and "resolves" in hooks
