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

# A plain import, deliberately, not `pytest.importorskip`. PyYAML is a declared
# member of the `dev` group, so absent it the environment is broken and the
# suite should say so. Skipping instead would take all seven assertions below
# out of the run and report green for it -- which is the exact failure ADR-0022
# is about, reproduced inside the tests that enforce ADR-0022.
import yaml

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
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in {".git", "__pycache__", "node_modules"}
                   and os.path.join(base, d) != os.path.join(ROOT, "examples", "payload")]
        for name in sorted(files):
            if not name.endswith((".md", ".py", ".yml", ".sh")):
                continue
            relative = os.path.relpath(os.path.join(base, name), ROOT)
            if relative in exempt:
                continue
            scanned += 1
            with open(os.path.join(base, name), encoding="utf-8", errors="replace") as fh:
                for number, line in enumerate(fh, 1):
                    for found in pattern.findall(line):
                        (stale if found not in names else live).append(
                            (relative, number, found))
    assert stale == [], (
        "a document names a gate job that is not in ci.yml (jobs are %s): %r"
        % (sorted(names & {STRUCTURAL, TERMS}), stale))
    # Anti-vacuity, and the reason this test needs it more than most: every
    # other assertion here fails closed on a bad parse, but this one asserts
    # over a list built by a filesystem walk. A walk that reached nothing --
    # wrong root, an extension filter that stopped matching, an exemption that
    # grew -- leaves `stale` empty and passes while checking nothing.
    assert scanned > 10, (
        "scanned only %d files; the walk is not reaching the tree, so the "
        "assertion above passed over an empty list" % scanned)
    assert live, (
        "no reference to a gate job was found anywhere. The documentation does "
        "name both halves, so finding none means the matcher or the walk has "
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
