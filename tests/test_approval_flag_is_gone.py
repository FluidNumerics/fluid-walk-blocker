"""No document still describes the install as needing an approval ceremony.

ADR-0021 removed `--i-have-approval`: the gate is root alone, and `--system`
installs. The code change is small and the prose change is not, which is the
shape this repository keeps getting wrong -- `CLAUDE.md` calls prose
load-bearing for exactly this reason.

It got it wrong here too, and that is why this file exists. The commit that
removed the flag updated `docs/operating.md`'s INSTALL step and missed its
PREVIEW step, which went on telling an operator, as root, to run
`deploy.py --system` and calling that a preview that "exits without writing".
After ADR-0021 that command installs. Every test passed, all eight CI checks
passed, and the runbook told a reader to deploy a root timer onto a shared
login node while they believed they were reading a plan.

The same round found the class repeating inside the record of the change
itself: ADR-0021's own Consequences claimed ADR-0005 and ADR-0006 had been
reworded when they had not been.

This scans for the STEM `approv`, not for the phrasings that happened to be
wrong. `test_boundary_prose.py` argues that case in its own docstring: pinning
the strings you already fixed asserts the phrasing and not the claim, and does
not see the next file. "once approval is given" would sail past a list of the
four phrases this round actually corrected.
"""
import os
import re

import pytest

from conftest import ROOT

# Documents a human reads and acts on. `examples/payload/` is excluded for the
# reason `test_boundary_prose.py` gives: it is generated, `build --check`
# already proves it byte-identical to these sources, and scanning both would
# report every finding twice and teach the next reader to edit a generated
# file.
SCANNED = [
    "README.md",
    "CLAUDE.md",
    "docs",
    ".github/instructions",
    ".claude",
]

# ADR-0004 and ADR-0021 discuss the removed flag deliberately: 0004 is the
# record whose gate it was, and 0021 is the record that removed it.
#
# ADR-0004's exemption is NOT a convenience. `test_adr_conventions.py`'s
# `test_every_narrows_line_quotes_text_its_target_still_contains` asserts that
# ADR-0021's Narrows line -- which quotes the clause naming `--i-have-approval`
# -- still appears verbatim in ADR-0004's body. Banning the token there would
# make the two tests unsatisfiable together, and the one that broke would be
# the ADR convention, not this one.
EXEMPT_FILES = {
    os.path.join("docs", "adr", "0004-root-owned-deploy-only.md"),
    os.path.join("docs", "adr", "0021-root-is-the-authorization.md"),
}

# One legitimate unrelated use: ADR-0007 is about every refusal naming a
# sanctioned alternative, and calls that alternative "approved". Waived by the
# sentence, not by the file, so a second `approv` appearing in ADR-0007 for a
# reason to do with the installer would still fail.
WAIVED_LINES = {
    (os.path.join("docs", "adr", "0007-no-namespace-index.md"),
     "Every command Layer 1 refuses is required to have an approved alternative"),
}

STEM = re.compile(r"approv", re.I)
TOKEN = "--i-have-approval"


def _documents():
    for entry in SCANNED:
        path = os.path.join(ROOT, entry)
        if os.path.isfile(path):
            yield entry
            continue
        for base, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(names):
                if name.endswith(".md"):
                    full = os.path.join(base, name)
                    yield os.path.relpath(full, ROOT)


def _lines(relative):
    with open(os.path.join(ROOT, relative), encoding="utf-8") as fh:
        return fh.read().splitlines()


DOCUMENTS = sorted(_documents())


def test_the_scan_actually_reaches_the_documents():
    """A glob that matches nothing passes every assertion under it.

    The runbook and the ADR corpus are the two places the missed prose lived,
    so their presence is asserted rather than assumed.
    """
    assert len(DOCUMENTS) > 15, DOCUMENTS
    assert os.path.join("docs", "operating.md") in DOCUMENTS
    assert any(d.startswith(os.path.join("docs", "adr")) for d in DOCUMENTS)


@pytest.mark.parametrize("relative", DOCUMENTS)
def test_no_document_names_the_removed_flag(relative):
    """A deleted token, so this is a strict literal ban."""
    if relative in EXEMPT_FILES:
        return
    hits = [(n, l) for n, l in enumerate(_lines(relative), 1) if TOKEN in l]
    assert hits == [], "%s still names the removed flag: %r" % (relative, hits)


@pytest.mark.parametrize("relative", DOCUMENTS)
def test_no_document_describes_an_approval_ceremony(relative):
    """The stem, because the failure was never about one phrasing."""
    if relative in EXEMPT_FILES:
        return
    hits = []
    for number, line in enumerate(_lines(relative), 1):
        if not STEM.search(line):
            continue
        if any(relative == f and waived in line for f, waived in WAIVED_LINES):
            continue
        hits.append((number, line.strip()))
    assert hits == [], (
        "%s describes an approval step; the gate is root alone (ADR-0021): %r"
        % (relative, hits))


def test_the_runbook_never_advertises_a_bare_system_command_as_a_preview():
    """The specific defect, pinned where the stem scan cannot see it.

    `--system` is correct in an INSTALL instruction and catastrophic in a
    PREVIEW one, and the stem scan cannot tell them apart -- the missed
    section did not contain the word "approved" in its command block at all.
    So: every fenced `deploy.py --system` line in the runbook is checked
    against the nearest preceding heading. A heading that talks about
    inspecting must carry `--dry-run`.
    """
    lines = _lines(os.path.join("docs", "operating.md"))
    heading = ""
    offenders = []
    for line in lines:
        if line.startswith("#"):
            heading = line
        if "deploy.py --system" not in line:
            continue
        inspecting = re.search(r"preview|dry.run|inspect|check",
                               heading, re.I)
        if inspecting and "--dry-run" not in line:
            offenders.append((heading.strip(), line.strip()))
    assert offenders == [], (
        "an inspection step advertises a command that installs: %r" % offenders)
