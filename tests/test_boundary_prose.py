"""Every prose statement of the `unscoped_depth` boundary agrees with the code.

`search_rules.judge_root()` refuses a root whose depth is LESS THAN
`unscoped_depth`, so a root at exactly that depth is allowed. Three documents
and a schema description state that boundary in words, and words drift from
the predicate they describe: PR #43 fixed three refusal arms that named one
level too deep, and its own review then found the schema description making
the same error and an ADR sentence that could be read either way (#44).

Pinning the two strings that were wrong would assert the phrasing and not the
claim, and would not see a third file. So this reads each statement the way
`test_shim_agrees` reads the refusal's advice: it parses the sentence down to
the shallowest depth the sentence ADVERTISES as allowed, as an offset from the
configured value, and requires that offset to be zero. Any wording whose
meaning parses correctly passes, whatever words it uses.

A wording the parser does not know is a failure, not a pass. That is
deliberate: a new way of saying it has to declare which depth it means, here,
once, rather than being assumed to mean the right one.
"""
import json
import os
import re

import pytest

from conftest import ROOT

# Where the boundary may legitimately be stated. `examples/payload/` is
# excluded on purpose: it is generated, and `build --check` already proves it
# byte-identical to these sources. Scanning both would double every finding
# and teach the next reader to edit a generated file.
PROSE_GLOBS = ("README.md", "CLAUDE.md", "docs/*.md", "docs/adr/*.md",
               "node/docs/*.md.in")
SCHEMA = os.path.join("schema", "site.schema.json")

KEY = "unscoped_depth"

# The forms this project uses, each with the offset from `unscoped_depth` of
# the shallowest root it advertises as allowed. Zero is correct. The non-zero
# entries are the two wordings that were actually wrong, kept so the parser
# RECOGNISES them and reports the number they mean, rather than failing as if
# they were unknown.
_KEYREF = r"(?:`\[filesystems\]\.unscoped_depth`|this level|this many|this depth|\d+)"
FORMS = (
    # "a root at least N components below the mount point"
    (re.compile(r"at least\s+" + _KEYREF + r"\s+(?:directory\s+)?components?\s+below"), 0),
    # "any root at N or deeper", "a root at this level or deeper"
    (re.compile(r"at\s+" + _KEYREF + r"\s+or\s+deeper"), 0),
    # "N components or deeper below the mount point"
    (re.compile(_KEYREF + r"\s+(?:directory\s+)?components?\s+or\s+deeper"), 0),
    # The same boundary stated from the REFUSED side: "fewer components than
    # this will be blocked". If fewer than N is blocked, N itself is allowed,
    # so this is the canonical claim in the mirror and its offset is also 0.
    (re.compile(r"fewer\s+(?:directory\s+)?components?\s+than\s+"
                r"(?:this|" + _KEYREF + r")"), 0),
    (re.compile(r"fewer\s+than\s+" + _KEYREF + r"\s+(?:directory\s+)?components?"), 0),
    # WRONG, and named so it parses: "start deeper than N components below"
    (re.compile(r"deeper\s+than\s+" + _KEYREF + r"\s+(?:directory\s+)?components?\s+below"), 1),
    # WRONG: "a root this many components or fewer below ... is refused"
    (re.compile(_KEYREF + r"\s+(?:directory\s+)?components?\s+or\s+fewer\s+below"), 1),
    # AMBIGUOUS, and read the way a reviewer read it: "at or below N"
    (re.compile(r"at\s+or\s+below\s+" + _KEYREF), 1),
)

# A unit only has to parse if it actually makes a claim about where a root may
# start. A bare mention of the key does not -- ADR-0007 names the key twice
# more without stating a direction, and both must pass untouched.
CLAIM_MARKERS = (
    "components below", "component below", "components or", "component or",
    "or deeper", "or fewer", "at least", "deeper than", "at or below",
    "this level or", "fewer components",
)


def _unwrapped_units(text):
    """Hard-wrapped prose, split into paragraphs and then into clauses.

    Both are needed: a sentence spans lines in every one of these documents,
    and a list item ends in a semicolon rather than a full stop.
    """
    for block in re.split(r"\n\s*\n", text):
        joined = " ".join(line.strip() for line in block.split("\n")).strip()
        if not joined:
            continue
        for unit in re.split(r"(?<=[.;])\s+", joined):
            if unit.strip():
                yield unit.strip()


def _advertised_offset(unit):
    """How far below `unscoped_depth` the shallowest allowed root is, per this
    sentence. `None` when no form matches."""
    for pattern, offset in FORMS:
        if pattern.search(unit):
            return offset
    return None


def _claims_a_boundary(unit):
    return any(marker in unit for marker in CLAIM_MARKERS)


def _prose_sources():
    """(label, text) for every source document that may state the boundary."""
    import glob
    for pattern in PROSE_GLOBS:
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding="utf-8") as fh:
                yield rel, fh.read()


def _schema_descriptions():
    """(label, text) for the schema. The key's own description does not
    contain the token -- the property NAME is the reference -- so it is
    yielded under a label that carries the name."""
    with open(os.path.join(ROOT, SCHEMA), encoding="utf-8") as fh:
        data = json.load(fh)
    found = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        for name, value in node.items():
            if not isinstance(value, dict):
                continue
            if name == KEY and "description" in value:
                found.append((SCHEMA + ":" + KEY, value["description"]))
            elif "description" in value and KEY in value["description"]:
                found.append((SCHEMA + ":" + name, value["description"]))
            walk(value, path + "/" + name)

    walk(data, "")
    return found


def boundary_claims():
    """Every unit, anywhere in the source tree, that states the boundary."""
    claims = []
    for label, text in list(_prose_sources()) + _schema_descriptions():
        implicit = label.endswith(":" + KEY)      # the key's own description
        for unit in _unwrapped_units(text):
            if not (implicit or KEY in unit):
                continue
            if _claims_a_boundary(unit):
                claims.append((label, unit))
    return claims


def test_the_scan_finds_the_statements_we_know_are_there():
    """A detector that silently matches nothing would pass every assertion
    below. Pin the documents, not the count: a new one is welcome, a
    disappearing one is the bug this guards."""
    labels = {label for label, _ in boundary_claims()}
    assert "README.md" in labels
    assert "docs/alternatives.md" in labels
    assert "docs/adr/0007-no-namespace-index.md" in labels
    assert SCHEMA + ":" + KEY in labels


@pytest.mark.parametrize("label,unit", boundary_claims(),
                         ids=lambda v: v if isinstance(v, str) and "/" in v else "")
def test_every_statement_of_the_boundary_advertises_the_depth_the_code_allows(
        label, unit, policy):
    offset = _advertised_offset(unit)
    assert offset is not None, (
        "%s states the boundary in a wording this test cannot parse, so its "
        "meaning is unchecked. Add the form to FORMS with the offset it means."
        "\n  %s" % (label, unit))
    assert offset == 0, (
        "%s advertises the shallowest allowed root as unscoped_depth + %d; "
        "judge_root() allows exactly unscoped_depth.\n  %s"
        % (label, offset, unit))


def test_a_bare_mention_of_the_key_is_not_a_claim_and_is_left_alone():
    """ADR-0007 names the key twice without stating a direction. Requiring a
    canonical form there would make the test fire on correct prose, which is
    how a checker like this gets deleted."""
    for unit in ("`[filesystems].unscoped_depth` is untouched: a deeper "
                 "*bounded* walk is a different claim and is governed by the "
                 "ceiling.",
                 "- `[filesystems].unscoped_depth`"):
        assert not _claims_a_boundary(unit), unit


def test_the_parser_reads_the_two_wordings_that_were_wrong():
    """Both are real: the refusal said the first until #43, and the schema
    said the second until #44. The parser has to RECOGNISE them, not merely
    fail on them, or the failure message cannot say what the text means."""
    assert _advertised_offset(
        "start deeper than `[filesystems].unscoped_depth` components below the "
        "mount point.") == 1
    assert _advertised_offset(
        "A root this many components or fewer below an expensive mount point "
        "is refused.") == 1
    assert _advertised_offset(
        "any root at or below `[filesystems].unscoped_depth` passes "
        "unbounded.") == 1
    # and the canonical ones
    assert _advertised_offset(
        "a root at least `[filesystems].unscoped_depth` components below the "
        "mount point") == 0
    assert _advertised_offset(
        "A root at this level or deeper on an expensive mount will walk "
        "unbounded.") == 0
    # the same claim from the refused side, which is how the schema now says
    # its second half
    assert _advertised_offset(
        "fewer components than this will be blocked on this node.") == 0
    assert _advertised_offset(
        "A root fewer than `[filesystems].unscoped_depth` components below an "
        "expensive mount is refused.") == 0
