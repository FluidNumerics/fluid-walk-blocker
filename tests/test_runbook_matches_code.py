"""The runbook's lists of what the node does are the lists the code has.

`docs/operating.md` describes four things an operator checks a node
against: the `audit_dir` states the reconcile journals, when it writes
`coverage_change`, what `deploy.py --uninstall` undoes, and which shells get
a hook; the `action` values the reaper's trail carries joined them later.
Each was found short of or different from the code (issue #89, issue #98,
issue #99, issue #204), and a reader checking a node against a short list
reads the missing item as a fault. Where the code's set can be read out of
the source it is, so a state added to `install.sh` without a runbook entry
fails here. The prose tests pin the true sentence; they do not forbid every
sentence that could contradict it.
"""
import os
import re

from conftest import ROOT

OPERATING = os.path.join(ROOT, "docs", "operating.md")
INSTALL_SH = os.path.join(ROOT, "node", "shim", "install.sh")
DEPLOY_PY = os.path.join(ROOT, "node", "deploy.py")
REAPER_PY = os.path.join(ROOT, "node", "reaper.py")


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
    calls = re.findall(r"^(.*)\n\s*sg_report coverage_change (.*)$", sh, re.M)
    # The change record, link_farm()'s; then ADR-0031's three: `unknown` when
    # the memory cannot be read, and on a due poll the standing drift and
    # the standing `unknown`, each marked. None fires on an addition.
    assert [(c[0].strip(), c[1]) for c in calls] == [
        ('if [ "$((_dropped + SWEPT_N))" -gt 0 ]; then',
         '"unwrapped-$((_dropped + SWEPT_N))"'),
        ("# so, once now and on the cadence until an install reseeds.", "unknown"),
        ("# names go to stdout, under the unit, as link_farm()'s do.",
         '"unwrapped-$_ln_sn" reasserted'),
        ('if [ "$_ln_origin" = relink ]; then', "unknown reasserted"),
    ], calls
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


def _uncovered_mount_bullet():
    text = _read(OPERATING)
    start = text.index("- an **`uncovered_mount`** record")
    return _joined(text[start:text.index("\n\n", start)])


def test_the_runbook_names_the_reassertion_marker_and_its_key():
    """ADR-0030: a standing uncovered mount is re-asserted, marked, on the
    cadence `[timer].reassert_interval_s` sets. A reader who sees the
    marker in the journal and not in the runbook reads a repeat as a
    change."""
    sh = _read(INSTALL_SH)
    calls = re.findall(r"sg_report uncovered_mount \S+ \S+ (\S+)(?: (\S+))?\n", sh)
    assert ("expensive", "reasserted") in calls, calls
    assert "@@timer.reassert_interval_s@@" in sh
    bullet = _uncovered_mount_bullet()
    assert '`"reasserted": true`' in bullet, bullet
    assert "`[timer].reassert_interval_s`" in bullet, bullet
    assert "`covered` and `unmounted` are never re-asserted" in bullet, bullet


def test_the_runbook_names_the_drift_reassertion_and_unknown():
    """ADR-0031: coverage drift is the second standing condition. A reader
    who meets a marked `coverage_change`, or an `unknown`, in the journal
    finds both in the runbook's reconcile bullet, with the two meanings of
    N and the key that sets the cadence."""
    sh = _read(INSTALL_SH)
    assert 'sg_report coverage_change "unwrapped-$_ln_sn" reasserted' in sh
    assert "sg_report coverage_change unknown reasserted" in sh
    assert "\n        sg_report coverage_change unknown\n" in sh
    clause = _reconcile_bullet().split("`coverage_change`", 1)[1].split("`relink_refused`")[0]
    assert "`unwrapped-N` with `\"reasserted\": true`" in clause, clause
    assert "`[timer].reassert_interval_s`" in clause, clause
    assert "`coverage_change` `unknown`" in clause, clause
    assert "until `deploy.py --system` reseeds it" in clause, clause
    assert "on an unmarked record it counts the links that went on that poll" in clause
    assert "on a marked one it counts the remembered names still unlinked" in clause
    assert "A swept name is never re-asserted" in clause, clause
    assert "`<spool_dir>/linked-names.state`" in clause, clause


def test_the_standing_definition_excludes_a_name_unlinked_on_this_poll():
    """ADR-0031: a name unlinked on this poll gets link_farm()'s change
    record and is not counted as standing, so the marked N leaves it out.
    The definition once read "this poll did not link it", which counts it,
    while the code skips it (review round 1 on the #152 PR). The code's
    exclusion and both definitions are pinned together."""
    sh = _read(INSTALL_SH)
    assert '|| linked_in_list "$_ln_name" "$SG_DROPPED_NAMES"; then' in sh
    adr = _joined(_read(os.path.join(
        ROOT, "docs", "adr",
        "0031-coverage-drift-is-a-standing-condition.md")))
    assert ("the table still wraps it, and this poll neither linked it nor "
            "unlinked it.") in adr, adr
    assert "this poll did not link it" not in adr
    clause = _joined(
        _reconcile_bullet().split("`coverage_change`", 1)[1]
        .split("`relink_refused`")[0])
    assert ("the remembered names still unlinked, other than any that poll "
            "unlinked, which get the change record only") in clause, clause


def test_the_runbook_promotes_by_the_site_key_and_names_no_retired_flag():
    """ADR-0034: promotion is `[reaper].action = "kill"` compiled into the
    unit, not a hand edit of the unit, and `--kill-others` is gone from the
    parser, so it is gone from every page an operator or a user reads. The
    ADRs are records and keep their history; they are not scanned."""
    section = _joined(_section(_read(OPERATING), "12. Promoting to `--kill`"))
    assert '`[reaper].action = "kill"`' in section, section
    assert "not a `site.toml` value" not in section
    for rel in (("docs", "operating.md"), ("README.md",),
                ("node", "docs", "what-to-run-instead.md.in"),
                ("docs", "site-config.md"), ("docs", "plan.md"), ("CLAUDE.md",),
                ("node", "reaper.py"), ("node", "deploy.py")):
        text = _read(os.path.join(ROOT, *rel))
        assert "--kill-others" not in text, rel
        assert "skipped_other_user" not in text, rel


def _terminate_body():
    src = _read(REAPER_PY)
    body = src[src.index("def terminate("):]
    return body[:body.index("\ndef ")]


def trail_actions():
    """Every `action` value reaper.py can write: a Finding's default, each
    assignment to a finding's action, each literal `"action"` in a record,
    and each word terminate() returns. An action set any other way is
    invisible to this scan; the known-subset test below is what notices the
    scan going stale."""
    src = _read(REAPER_PY)
    found = set(re.findall(r'self\.action = "([a-z_]+)"', src))
    found |= set(re.findall(r'finding\.action = "([a-z_]+)"', src))
    found |= set(re.findall(r'"action": "([a-z_]+)"', src))
    found |= set(re.findall(r'return "([a-z_]+)"', _terminate_body()))
    return found


def test_the_action_scan_finds_the_actions_we_know_are_there():
    assert {"reported", "reported_never_killed", "blind", "skipped_kill_cap",
            "already_gone", "terminated", "killed", "signalled_but_wedged",
            "signal_failed", "kill_error"} <= trail_actions()


def _action_habit():
    text = _read(OPERATING)
    start = text.index("- **`action` is what was done")
    return _joined(text[start:text.index("\n- ", start + 1)])


def test_every_action_the_reaper_writes_is_defined_in_the_runbook():
    """Issue #204: the runbook named some of the trail's actions, and a
    reader meeting a row with one it did not name had nothing to check it
    against. The trail-reading habit defines every one, each followed by
    its clause."""
    habit = _action_habit()
    missing = sorted(a for a in trail_actions()
                     if not re.search(r"`%s`[:,]" % re.escape(a), habit))
    assert not missing, (
        "reaper.py writes actions the runbook's trail-reading habit does not "
        "define: %s" % ", ".join(missing))


def test_the_runbook_times_the_kill_recheck_as_the_code_does():
    """Issue #203: the post-`SIGKILL` re-check is a fixed one-second sleep
    in terminate(); `[reaper].settle_s` is the wait before a second PSI
    reading and nothing in the kill path reads it."""
    body = _terminate_body()
    assert "signal.SIGKILL)" in body and "\n    sleep(1)\n" in body, body
    assert "settle" not in body
    section = _joined(_section(_read(OPERATING), "12. Promoting to `--kill`"))
    assert "a re-check one second after `SIGKILL`" in section, section
    assert "settle_s" not in section
    assert "the re-check one second later" in _action_habit()
