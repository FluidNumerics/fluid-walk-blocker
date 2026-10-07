"""The IP-hygiene gate's shape, as assertions.

ADR-0022 split the gate into a secretless structural half that runs everywhere
and a customer-term half that runs only in this repository. The load-bearing
part is an asymmetry that is easy to "simplify" away: on a pull request from a
fork the term job must FAIL, not skip, because a job skipped by a job-level
`if` reports as a *passing* required status check — so a quiet skip there is a
green merge button over a scan that never ran.

Nothing in the suite read `ci.yml` before this file, so reverting the split, or
turning that failure back into a skip, broke no test.

The workflow is parsed with PyYAML rather than read by hand. A bespoke reader
would be a second, unversioned YAML implementation, and its bugs would surface
as confident assertions about a workflow that says something else.
"""
import os
import re
import shlex

# A plain import, deliberately, not `pytest.importorskip`. PyYAML is a declared
# member of the `dev` group, so absent it the environment is broken and the
# suite should say so. Skipping instead would take all seven assertions below
# out of the run and report green for it -- which is the exact failure ADR-0022
# is about, reproduced inside the tests that enforce ADR-0022.
import yaml

from _tracked import tracked_files
from conftest import ROOT

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")
CANONICAL = "FluidNumerics/fluid-walk-blocker"
STRUCTURAL = "ip-hygiene-structural"
TERMS = "ip-hygiene-terms"


def _workflow():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _jobs():
    jobs = _workflow().get("jobs")
    # Anti-vacuity: every assertion below is a lookup into this mapping, so a
    # parse that silently yielded nothing would make all of them pass.
    assert isinstance(jobs, dict) and len(jobs) >= 4, (
        "parsed %r jobs out of ci.yml; the file or the parse is wrong, and "
        "every other assertion in this file would be vacuous" % (jobs,))
    return jobs


def _steps(job):
    return _jobs()[job].get("steps") or []


def _text(value):
    """Everything in a step, flattened, so a reference can be looked for
    without caring which key it lives under."""
    if isinstance(value, dict):
        return "\n".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(_text(v) for v in value)
    return str(value)


def test_both_halves_of_the_gate_exist():
    jobs = _jobs()
    for name in (STRUCTURAL, TERMS):
        assert name in jobs, (
            "%s is gone from ci.yml. ADR-0022 splits the gate in two and the "
            "docs name both halves" % name)


def test_the_structural_half_needs_no_secret_and_is_not_conditional():
    """It is the half a fork can run, so it must not depend on a secret it
    cannot have, and must not be gated on being this repository."""
    job = _jobs()[STRUCTURAL]
    assert "secrets." not in _text(job), (
        "%s references a secret. It runs in forks, where there is none, and "
        "depending on one there turns the half a fork CAN run into one it "
        "cannot (ADR-0022)" % STRUCTURAL)
    assert "if" not in job, (
        "%s has a job-level `if`. It is meant to run on every run in every "
        "repository; a condition here is the quiet skip ADR-0022 confines to "
        "the term half" % STRUCTURAL)


def test_the_term_half_runs_only_in_this_repository():
    job = _jobs()[TERMS]
    condition = str(job.get("if", ""))
    assert CANONICAL in condition and "github.repository" in condition, (
        "%s is no longer guarded on being this repository. Without that guard "
        "it fails in every fork over a secret the fork cannot hold, which "
        "teaches a fork maintainer that a red gate is normal (ADR-0022)"
        % TERMS)


def test_a_fork_pull_request_fails_the_term_half_rather_than_skipping_it():
    """The asymmetry the split exists for.

    A fork PR into this repository gets no secret. That case must fail loudly:
    a job-level `if` that skipped it would report as a PASSING required check.
    """
    steps = _steps(TERMS)
    fork_steps = [s for s in steps
                  if "head.repo.full_name" in str(s.get("if", ""))]
    assert len(fork_steps) == 1, (
        "expected exactly one step in %s keyed on the pull request's head "
        "repository, found %d. That step is what makes a fork PR fail instead "
        "of skip (ADR-0022)" % (TERMS, len(fork_steps)))

    step = fork_steps[0]
    condition = str(step["if"])
    assert "!=" in condition and "github.repository" in condition, (
        "the fork-pull-request step in %s no longer compares the head "
        "repository against this one" % TERMS)
    assert "pull_request" in condition, (
        "the fork-pull-request step in %s is not restricted to pull_request "
        "events, so it would fire on a push where the comparison is vacuous"
        % TERMS)

    body = _text(step.get("run", ""))
    assert "exit 3" in body, (
        "the fork-pull-request step in %s no longer exits non-zero. Failing is "
        "the whole point: a skip here counts as a passing required check "
        "(ADR-0022)" % TERMS)
    assert "::error::" in body, (
        "the fork-pull-request step in %s no longer emits an error annotation. "
        "A contributor who cannot fix this needs to be told why it failed"
        % TERMS)


def test_the_term_scan_still_requires_a_term_list():
    """`--require-terms` is the load-bearing check, not the shell's file test:
    an unset secret expands to empty, which `printf` writes as a lone newline
    that `test -s` calls non-empty. The scanner counts PARSED terms."""
    body = _text(_steps(TERMS))
    assert "--require-terms" in body, (
        "%s no longer passes --require-terms, so an absent or empty term list "
        "would pass as a clean scan" % TERMS)


def test_no_document_names_a_gate_job_that_does_not_exist():
    """The failure that started this: after the gate was split, a test's skip
    message still said "CI's ip-hygiene job enforces it" — a job that no longer
    existed. Nothing caught it but a reviewer reading the diff.

    Scoped to the `ip-hygiene` prefix on purpose. A general "every hyphenated
    word before 'job' is a job name" rule matches ordinary prose — this tree
    already says "wall-clock-bounded job", "schema-validation job" and
    "open-ended job", none of which name anything in `ci.yml`. The prefix is
    this repository's own coined name for the gate, so a token carrying it is a
    reference to a job here and nothing else.
    """
    import re

    names = set(_jobs())
    pattern = re.compile(r"\bip-hygiene[a-z0-9-]*")
    # This file states the rule, so it has to quote the dead name the rule
    # exists to catch. It is the only exemption, and it is a FILE rather than a
    # pattern so that a stale reference anywhere else still fails.
    exempt = {os.path.join("tests", "test_ci_workflow.py")}
    stale = []
    live = []
    scanned = 0
    for relative in tracked_files(suffixes=(".md", ".py", ".yml", ".sh")):
        if relative in exempt:
            continue
        scanned += 1
        with open(os.path.join(ROOT, relative), encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh, 1):
                for found in pattern.findall(line):
                    (stale if found not in names else live).append(
                        (relative, number, found))
    assert stale == [], (
        "a document names a gate job that is not in ci.yml (jobs are %s): %r"
        % (sorted(names & {STRUCTURAL, TERMS}), stale))
    # Anti-vacuity, and the reason this test needs it more than most: every
    # other assertion here fails closed on a bad parse, but this one asserts
    # over a list built from a file listing. A listing that reached nothing --
    # wrong root, an extension filter that stopped matching, an exemption that
    # grew -- leaves `stale` empty and passes while checking nothing.
    assert scanned > 10, (
        "scanned only %d files; the listing is not reaching the tree, so the "
        "assertion above passed over an empty list" % scanned)
    assert live, (
        "no reference to a gate job was found anywhere. The documentation does "
        "name both halves, so finding none means the matcher or the listing has "
        "stopped working rather than that the tree is clean")


def test_the_report_only_job_is_not_a_required_gate_by_accident():
    """`measure` is deliberately advisory: it is pull-request-only and
    continue-on-error, so it must never become something a merge waits on."""
    measure = _jobs().get("measure")
    assert measure is not None, "the measure job is gone"
    assert measure.get("continue-on-error") is True, (
        "measure is no longer continue-on-error. It is a measurement campaign "
        "on an uncharacterised runner, and a gate that flakes trains people to "
        "re-run CI until it is green")
    # The docstring above claims two properties, so assert both. A name or a
    # docstring promising coverage the assertions do not provide is the defect
    # this file exists to catch, and it is not exempt from it.
    assert "pull_request" in str(measure.get("if", "")), (
        "measure is no longer restricted to pull requests. On a push to main "
        "there is no merge base to compare against, so it has nothing to "
        "measure and would report a failure about the runner rather than the "
        "change")


def _run_measure_step(tmp_path, stub_rc, stub_out):
    """Run the measure job's ratio step for real, under bash -e as Actions
    runs it, with `measure.sh` replaced by a stub that prints `stub_out` and
    exits `stub_rc`. Returns (step exit, stdout, step summary)."""
    import subprocess

    steps = [s for s in _steps("measure") if "measure.sh" in str(s.get("run", ""))]
    assert len(steps) == 1, "expected one step in measure that runs measure.sh"
    shim = tmp_path / "node" / "shim"
    shim.mkdir(parents=True)
    (shim / "measure.sh").write_text(
        "cat <<'OUT'\n%s\nOUT\nexit %d\n" % (stub_out, stub_rc))
    runner = tmp_path / "runner"
    for side in ("base", "head"):
        (runner / side / "shim").mkdir(parents=True)
        (runner / side / "shim" / "guard.sh").write_text("#!/bin/sh\n")
    summary = tmp_path / "summary.md"
    env = dict(os.environ, RUNNER_TEMP=str(runner),
               GITHUB_STEP_SUMMARY=str(summary))
    r = subprocess.run(["bash", "-e", "-c", steps[0]["run"]], cwd=str(tmp_path),
                       capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, r.stdout, summary.read_text()


def test_a_ratio_run_that_compared_nothing_is_not_reported_as_green(tmp_path):
    """The step is continue-on-error, so its exit status alone reaches nobody:
    a run whose every pair was discarded showed the same green as one that
    compared two shims and found them equal. It has to say so where it is
    seen -- an annotation, and the step summary."""
    rc, out, summary = _run_measure_step(
        tmp_path, 3,
        "pair 1 DISCARDED: an overhead at or below zero\n"
        "=== 0/8 usable pairs ===\n"
        "measure.sh: only 0 pairs survived, below the\n"
        "  3 this gate needs. Each DISCARDED line above says\n"
        "  which check refused it.")
    assert rc == 3, "the step swallowed measure.sh's exit status"
    assert "::warning title=measure compared nothing::" in out, out
    assert "COMPARED NOTHING" in summary and "0/8" in summary, summary
    assert "pair 1 DISCARDED" in summary, \
        "the summary hides which check discarded"
    # The verdict line carries the count too, from its own sed, so this is
    # the only assertion that sees the grep keep measure.sh's own line.
    assert "=== 0/8 usable pairs ===" in summary, summary
    # measure.sh's stderr is captured in the same log, and its explanation
    # names DISCARDED in a sentence; the summary carries the lines, not prose.
    assert "line above says" not in summary, summary


def test_a_ratio_run_that_compared_carries_no_warning(tmp_path):
    """The inverse, or the test above passes against a step that warns on
    every run -- which is noise people learn to skip."""
    rc, out, summary = _run_measure_step(
        tmp_path, 0,
        "=== 8/8 usable pairs ===\n"
        "fast-path ratio (median)        1.004 x")
    assert rc == 0
    assert "::warning" not in out, out
    assert "compared 8/8 usable pairs" in summary, summary
    assert "=== 8/8 usable pairs ===" in summary, summary
    assert "fast-path ratio (median)" in summary, summary


def test_the_matrix_reaches_the_newest_interpreters():
    """Issue #130: a failure that exists only from 3.13 or 3.14 passed CI
    while the matrix stopped at 3.12, though `uv run` on a current
    workstation resolves 3.14."""
    versions = _jobs()["test"]["strategy"]["matrix"]["python-version"]
    assert [str(v) for v in versions] == [
        "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"], (
        "the test matrix is %r. 3.9 is the node floor (ADR-0015) and 3.14 is "
        "what a workstation resolves; both ends are load-bearing" % versions)


def test_one_row_runs_the_banner_tests_with_colour_forced():
    """The CI oracle for the autouse colour fixture in tests/conftest.py
    (issue #93). Only 3.14 argparse colours `--help`, and with output
    captured (no tty) only when the environment forces it, so without this
    step deleting the fixture fails no CI run. FORCE_COLOR is pinned in
    runuser's `env` list, beside everything else the child runs under: set
    on the step instead, it would reach the tests only as far as runuser,
    PAM and login.defs pass it through, and a step whose tests ran
    uncoloured would pass vacuously."""
    steps = [s for s in _steps("test") if "FORCE_COLOR" in _text(s)]
    assert len(steps) == 1, (
        "expected exactly one step in the test job that forces colour, found "
        "%d" % len(steps))
    step = steps[0]
    condition = " ".join(str(step.get("if", "")).split())
    if condition.startswith("${{") and condition.endswith("}}"):
        condition = condition[3:-2].strip()
    assert condition == "matrix.python-version == '3.14'", (
        "the forced-colour step is not keyed to the 3.14 row alone: %r"
        % step.get("if"))
    # Read the body as bash would: continuations joined, comments dropped.
    # It must be one command, `runuser -u ciuser -- env`, then NAME=value
    # words, then `uv run`. A second line or a `;`, `&&` or `|` could run
    # the tests outside that env list, and words an `echo` or a comment
    # merely mentions are not the env list at all.
    body = str(step.get("run", "")).replace("\\\n", " ")
    lines = [line for line in body.splitlines()
             if shlex.split(line, comments=True)]
    assert len(lines) == 1, (
        "the forced-colour step must be one command, found %d: %r"
        % (len(lines), lines))
    lexer = shlex.shlex(lines[0], posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words = list(lexer)
    operators = [w for w in words if w and set(w) <= set(";&|()<>")]
    assert not operators, (
        "the forced-colour step chains or redirects with %r" % operators)
    head = ["runuser", "-u", "ciuser", "--", "env"]
    assert words[:len(head)] == head, (
        "the forced-colour step does not start `%s`: %r"
        % (" ".join(head), words[:len(head)]))
    rest = words[len(head):]
    env_list = []
    while rest and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", rest[0]):
        env_list.append(rest.pop(0))
    assert rest[:2] == ["uv", "run"], (
        "runuser's env list is not followed by `uv run`: %r" % rest[:2])
    names = [w.split("=", 1)[0] for w in env_list]
    forced = [w.split("=", 1)[1] for w in env_list
              if w.startswith("FORCE_COLOR=")]
    assert forced == ["3"], (
        "runuser's env list must set FORCE_COLOR=3 once; it sets %r"
        % forced)
    # On 3.14 NO_COLOR, and PYTHON_COLORS=0, outrank FORCE_COLOR.
    assert not {"NO_COLOR", "PYTHON_COLORS"} & set(names), (
        "runuser's env list sets a variable that outranks FORCE_COLOR: %r"
        % names)
    assert "tests/test_banner.py" in rest, (
        "the forced-colour step does not run the banner tests")
