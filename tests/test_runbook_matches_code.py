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
    ones, and every word assigned to `$sg_spool_why`, the one variable a
    call may journal instead. A call through any other variable fails here,
    rather than journalling a state this scan cannot see."""
    sh = _read(INSTALL_SH)
    calls = re.findall(r"sg_report audit_dir (\S+)", sh)
    states = {c for c in calls if re.fullmatch(r"[a-z-]+", c)}
    indirect = sorted(set(calls) - states)
    assert indirect == ['"$sg_spool_why"'], indirect
    states |= set(re.findall(r"sg_spool_why=([a-z-]+)", sh))
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
    # The claims themselves, not words a contradicting sentence would share.
    assert clause.startswith(" when coverage shrank "), clause
    assert "a name that starts being wrapped writes no record" in clause
    assert not re.search(r"added|addition also writes", clause), clause


def _uninstall_paragraph():
    return _joined(next(p for p in _section(_read(OPERATING), "14.").split("\n\n")
                        if "It reverses the install" in p))


def test_the_runbook_lists_the_journal_revocation_the_uninstall_performs():
    body = _read(DEPLOY_PY)
    body = body[body.index("def system_uninstall("):]
    assert "journal_revoke_command(" in body and '"rm", "-f", dropin' in body
    para = _uninstall_paragraph()
    assert ("removes the journal drop-in under `[install].tmpfiles_dir` and "
            "revokes, under each journal directory that exists, the read it "
            "granted to the gids it records (ADR-0026)") in para


def test_the_runbook_does_not_say_the_uninstall_removes_the_prefix():
    sh = _read(INSTALL_SH)
    arm = sh[sh.index("    uninstall)\n"):]
    arm = arm[:arm.index("\nesac")]
    # The arm removes $BIN and touches nothing else under the prefix.
    assert 'rmdir "$BIN"' in arm and "$PREFIX" not in arm
    para = _uninstall_paragraph()
    assert "It does not remove the prefix: the payload stays under it." in para
    assert "removes `<prefix>/bin`" in para
    helper = next(p for p in _section(_read(OPERATING), "14.").split("\n\n")
                  if p.startswith("`install.sh --uninstall` exists too"))
    text = para + " " + _joined(helper)
    # A regression guard, not a contradiction detector: these are the two
    # phrases the runbook was wrong with (issue #89). A string pin cannot
    # rule out every sentence that contradicts the pinned one; that is
    # review's job, not this test's.
    for wrong in ("removes the prefix", "without removing the payload"):
        assert wrong not in text, "the runbook again says %r" % wrong


def test_the_runbook_says_a_best_effort_hook_is_written_only_when_its_shell_resolves():
    sh = _read(INSTALL_SH)
    assert re.search(r'for _wh in \$SG_HOOKS_BEST_EFFORT; do\n\s*if command -v "\$_wh"', sh)
    text = _read(OPERATING)
    start = text.index("What it writes, all root-owned")
    writes = _joined(text[start:text.index("What it verifies", start)])
    hooks = next(item for item in writes.split(" - ") if "hook block" in item)
    assert ("in each `best-effort` shell's only when its binary resolves and "
            "the required hooks were proven first") in hooks
    assert "whether or not" not in hooks
    # fish's kind is `dropin` whatever its gate (hook_select), so the
    # drop-in clause must not read as belonging to one class.
    assert "fish, in either class, gets a dedicated `conf.d` drop-in" in hooks
    assert re.search(r"fish\)\n\s*HK_FILE=\$FISH_CONF_FILE; HK_PKG=''; HK_KIND=dropin", sh)
