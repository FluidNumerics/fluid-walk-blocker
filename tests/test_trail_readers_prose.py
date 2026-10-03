"""Every sentence that shuts an account out of the audit trail says it is the
mode bits that do so.

ADR-0025: "nobody outside the group reads the trail" is true of the mode bits
only. A named ACL entry, usually inherited from the spool's parent, lets more
accounts read it, and the deploy reports each such entry rather than removing
it. A sentence that states the exclusion as plain fact tells an operator who
then finds an outside reader that something is broken. PR #94 qualified two
such sentences; issue #95 and issue #96 found four more, and a fifth sat in
the runbook beside them.

Pinning those would not see the next, so this reads every sentence in the
source prose that excludes a reader and requires it to name the mode bits or
the ACL. Two places are exempt by ADR-0023, not by convenience: a block quote,
which is superseded wording kept verbatim, and an ADR's `## Context`, which is
what was true then and is not updated.
"""
import glob
import os
import re

import pytest

from conftest import ROOT

# `examples/payload/` is generated, and `build --check` proves it identical.
PROSE_GLOBS = ("README.md", "CLAUDE.md", "docs/*.md", "docs/adr/*.md",
               "node/docs/*.md.in")

# The ways this tree has said that an account cannot read the trail.
EXCLUDES = re.compile(
    r"nobody\s+(?:else|outside)|no\s+one\s+else|outside\s+the\s+group"
    r"|cannot\s+read\s+the\s+trail|refused\s+the\s+`tail`|closed\s+to\s+them",
    re.IGNORECASE)
QUALIFIED = re.compile(r"mode\s+bits|\bACL\b")


def _without_context(text):
    return re.sub(r"(?ms)^## Context\n.*?(?=^## )", "", text)


def _units(text):
    """Hard-wrapped prose as clauses, with block-quoted lines dropped."""
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.split("\n")
                 if not ln.lstrip().startswith(">")]
        joined = " ".join(lines).strip()
        for unit in re.split(r"(?<=[.;])\s+", joined):
            if unit.strip():
                yield unit.strip()


def exclusion_claims():
    claims = []
    for pattern in PROSE_GLOBS:
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if rel.startswith(os.path.join("docs", "adr")):
                text = _without_context(text)
            claims.extend((rel, unit) for unit in _units(text)
                          if EXCLUDES.search(unit))
    return claims


def test_the_scan_finds_the_statements_we_know_are_there():
    """A detector that matches nothing passes the check below vacuously."""
    labels = {label for label, _ in exclusion_claims()}
    for expected in ("docs/operating.md", "docs/site-config.md",
                     "docs/adr/0012-audit-trail-is-readable.md",
                     "docs/adr/0019-uncovered-mounts-are-reported-on-change.md",
                     "docs/adr/0025-spool-readable-by-a-site-group.md"):
        assert expected in labels


@pytest.mark.parametrize("label,unit", exclusion_claims(),
                         ids=lambda v: v if isinstance(v, str) and "/" in v else "")
def test_every_exclusion_from_the_trail_is_qualified_by_the_mode_bits(label, unit):
    assert QUALIFIED.search(unit), (
        "%s says an account cannot read the trail without saying that is the "
        "mode bits' answer; a named ACL entry can widen it (ADR-0025).\n  %s"
        % (label, unit))
