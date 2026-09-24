"""The ADR conventions in docs/adr/TEMPLATE.md, as assertions.

These exist because review round 1 on the governance PR found two Narrows
lines quoting text their target ADR did not contain, and one ADR whose body
had been edited in place instead of narrowed. Prose here is load-bearing, so
the conventions are pinned rather than re-derived each round.
"""
import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADR_DIR = os.path.join(ROOT, "docs", "adr")
ADRS = sorted(p for p in glob.glob(os.path.join(ADR_DIR, "[0-9][0-9][0-9][0-9]-*.md")))
SECTIONS = ["## Context", "## Decision", "## Consequences",
            "## Re-measure when", "## Site config touched"]
NARROWS = re.compile(r'^\*\*Narrows:\*\* ADR-(\d{4}), "(.+)"\s*$', re.M)
CLOSING = 'This is not to be "fixed" by a later agent.'
SETTLED_AGAINST_REVIEW = {"0005", "0006", "0007", "0009", "0013"}
SUPERSEDED = "\n## Superseded wording\n"

# ADR-0023: a `Narrows:` quote resolves inside the target's `## Superseded
# wording` section, not merely somewhere in its body. These two narrowings are
# exempt, each for a reason that has to be argued rather than absorbed by a
# looser matcher -- which is why this is a list and not a weaker assertion.
# An exempt pair still has to carry its clause somewhere; it just stays put.
NARROWS_EXEMPT = {
    # ADR-0007's clause lives in `## Context`, which TEMPLATE.md defines as what
    # was true when the decision was necessary. Its value is that it was NOT
    # updated, so the correction stays in the Status note instead.
    ("0016", "0007"),
    # ADR-0012 keeps ADR-0004's clause in full -- it narrows only an inference
    # about readability that was never part of it. The clause still states what
    # holds, and filing it under superseded wording would call a live clause dead.
    ("0012", "0004"),
}


def _superseded_section(text):
    """The `## Superseded wording` body, or None.

    Stops at the closing paragraph (ADR-0005 has both) AND at any following
    section. Cutting only at the closing paragraph reads a later section's text
    as part of this one in a record that has no closing paragraph, which would
    resolve a Narrows quote against prose that is not the superseded wording.
    """
    if SUPERSEDED not in text:
        return None
    body = text.split(SUPERSEDED, 1)[1]
    for stop in (CLOSING, "\n## "):
        body = body.split(stop, 1)[0]
    return body


def _words(text):
    return re.sub(r"\s+", " ", re.sub(r"[*`]", "", text)).strip().lower()


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _number(path):
    return os.path.basename(path)[:4]


def _by_number(number):
    matches = [p for p in ADRS if _number(p) == number]
    assert len(matches) == 1, "ADR-%s: expected exactly one file, found %r" % (number, matches)
    return matches[0]


def test_there_are_twenty_four_adrs_numbered_without_gaps():
    numbers = [_number(p) for p in ADRS]
    assert numbers == ["%04d" % i for i in range(1, len(numbers) + 1)]
    assert len(numbers) == 24


@pytest.mark.parametrize("path", ADRS, ids=_number)
def test_each_adr_has_the_template_sections_in_order(path):
    text = _read(path)
    positions = [text.find(s + "\n") for s in SECTIONS]
    assert all(p >= 0 for p in positions), "missing section(s) in %s" % path
    assert positions == sorted(positions), "sections out of order in %s" % path
    assert text.startswith("# ADR-%s: " % _number(path))
    assert "**Status:** accepted, " in text
    assert "**Evidence:**" in text
    assert "<!--" not in text, "template comment left in %s" % path
    assert "NNNN" not in text and "YYYY" not in text


@pytest.mark.parametrize("path", ADRS, ids=_number)
def test_the_closing_paragraph_appears_only_where_the_decision_was_settled_against_review(path):
    present = CLOSING in _read(path)
    assert present == (_number(path) in SETTLED_AGAINST_REVIEW), path


def test_every_narrows_line_quotes_text_its_target_records_as_superseded():
    """The point of ADR-0023, as an assertion.

    The previous form resolved the quote anywhere after `## Context`, so it
    passed whether or not the clause had moved -- and passed while a record
    carried the clause in two places at once, which made it vacuous for the
    record that quoted itself. Resolving it inside `## Superseded wording`
    is what makes the assertion mean its own name.
    """
    seen = 0
    for path in ADRS:
        for target, clause in NARROWS.findall(_read(path)):
            seen += 1
            target_text = _read(_by_number(target))
            # A quote must be a clause, not a fragment: a three-word phrase is
            # a substring of almost any body by coincidence, so existence would
            # prove nothing. Every live quote has at least five words.
            assert len(_words(clause).split()) >= 5, (
                "ADR-%s's Narrows quote %r is too short to identify a clause"
                % (_number(path), clause))
            assert "narrowed by ADR-%s" % _number(path) in target_text, (
                "ADR-%s lacks the reciprocal Status note for ADR-%s" % (target, _number(path)))
            # Compare words, not markup: the target may wrap the clause across
            # lines and carry emphasis or code spans the quote does not.
            if (_number(path), target) in NARROWS_EXEMPT:
                # Exempt from MOVING, not from existing. The clause stays where
                # it is and must still be there.
                body = target_text.split("\n## Context", 1)[1]
                assert _words(clause) in _words(body), (
                    "ADR-%s narrows ADR-%s on %r, which is exempt from moving "
                    "but has gone missing from ADR-%s entirely"
                    % (_number(path), target, clause, target))
                continue
            section = _superseded_section(target_text)
            assert section is not None, (
                "ADR-%s narrows ADR-%s, which has no `## Superseded wording` "
                "section to record the clause in (ADR-0023)"
                % (_number(path), target))
            assert _words(clause) in _words(section), (
                "ADR-%s narrows ADR-%s on %r, which ADR-%s does not record "
                "under `## Superseded wording`" % (_number(path), target, clause, target))
            # ADR-0023 says the clause MOVES. Presence in the section does not
            # prove that: a copy satisfies it while the stale sentence stays in
            # the section a reader quotes, which is the whole defect. Assert the
            # absence too, or the record that quotes itself passes again.
            vacated = target_text.split("\n## Context", 1)[1].split(SUPERSEDED, 1)[0]
            assert _words(clause) not in _words(vacated), (
                "ADR-%s narrows ADR-%s on %r, which ADR-%s still carries in the "
                "section it came from -- that is a copy, not a move (ADR-0023)"
                % (_number(path), target, clause, target))
    assert seen >= 8, "expected at least the eight known Narrows lines"


# A prose reference to the corpus as a RANGE, e.g. "`0001` through `0023`" or
# "`docs/adr/0001` … `0023`". The connective is required: without it this also
# matches a citation pair like "(ADR-0001, ADR-0002)", which is not a range and
# must not be rewritten when a record is added.
ADR_RANGE = re.compile(r"0001[^0-9\n]*?(?:through|…|\bto\b)[^0-9\n]*?00(\d\d)")
# Every `**Narrows:**` line, however malformed. NARROWS is the strict form; a
# line this matches and NARROWS does not is one the convention test skips
# silently.
NARROWS_LOOSE = re.compile(r"^\*\*Narrows:\*\*", re.M)


def _prose_files():
    """Tracked markdown outside the generated payload.

    `examples/payload/` is excluded for the reason `test_boundary_prose.py`
    gives: it is a build product, `build --check` already proves it identical to
    these sources, and scanning both reports every finding twice.
    """
    out = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in {".git", "__pycache__", "node_modules"}
                   and os.path.join(base, d) != os.path.join(ROOT, "examples", "payload")]
        for name in sorted(names):
            if name.endswith(".md"):
                out.append(os.path.relpath(os.path.join(base, name), ROOT))
    return sorted(out)


def test_every_adr_range_in_prose_names_the_real_last_record():
    """The corpus size is pinned by the count test, but it is also WRITTEN, in
    prose, in files no other test reads.

    When a record is added the count test fails and gets fixed; the prose can
    stay a record behind with everything green. It was hand-updated in three
    consecutive pull requests, which is how often a hand-updated reference is
    eventually forgotten.
    """
    last = "%04d" % len(ADRS)
    stale, seen = [], {}
    for relative in _prose_files():
        with open(os.path.join(ROOT, relative), encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                for match in ADR_RANGE.finditer(line):
                    seen.setdefault(relative, 0)
                    seen[relative] += 1
                    if match.group(1) != last[2:]:
                        stale.append((relative, number, match.group(1)))
    assert stale == [], (
        "prose names a corpus range ending at %r, but the last record is %s: %r"
        % (stale[0][2] if stale else "", last, stale))
    # Anti-vacuity: if the matcher stops matching, the assertion above passes
    # over an empty list and this test becomes furniture. These two files carry
    # the reading order and their references are the ones that go stale.
    for required in ("README.md", "CLAUDE.md"):
        assert seen.get(required), (
            "no ADR range found in %s -- the matcher has stopped matching, or "
            "the reading order moved" % required)


def test_every_narrows_line_is_matched_by_the_strict_pattern():
    """`NARROWS` drives the convention tests, so a line it does not match is a
    narrowing that is never checked.

    The count assertion in the narrows test is a floor, not a coupling: a
    malformed `Narrows:` line -- wrong quotes, a missing comma -- drops out of
    `findall` silently and the floor still holds.
    """
    for path in ADRS:
        text = _read(path)
        assert len(NARROWS_LOOSE.findall(text)) == len(NARROWS.findall(text)), (
            "%s has a `**Narrows:**` line the strict pattern does not match, so "
            "its narrowing is skipped by every check in this file" % path)


def _narrows_clause(narrower, target):
    for found, clause in NARROWS.findall(_read(_by_number(narrower))):
        if found == target:
            return clause
    raise AssertionError("ADR-%s no longer narrows ADR-%s" % (narrower, target))


def test_each_narrows_exemption_still_has_the_reason_it_was_granted_for():
    """An exemption is a claim about WHY a clause did not move, and ADR-0023
    says it has to be argued rather than inherited.

    Left as a bare list, the exemptions are the one way to make any future
    failure of the narrows assertion disappear: add the pair, and the record it
    covers is never checked again. So the set is pinned -- adding or removing an
    entry fails here until someone writes the reason -- and each reason's
    structural precondition is asserted, so an exemption whose ground has gone
    stops being granted.
    """
    assert NARROWS_EXEMPT == {("0016", "0007"), ("0012", "0004")}, (
        "the narrows exemption list changed. Each entry is a claim that has to "
        "be argued in ADR-0023 and checked below, not added silently")

    # ("0016", "0007") is exempt because the clause is Context-resident, and
    # `## Context` is as-of-then by TEMPLATE.md's contract. Move it out of
    # Context and the ground for the exemption is gone.
    context = _read(_by_number("0007")).split("\n## Context", 1)[1].split("\n## Decision", 1)[0]
    assert _words(_narrows_clause("0016", "0007")) in _words(context), (
        "ADR-0007's narrowed clause is no longer in `## Context`, so the reason "
        "its exemption was granted has gone -- it should move like the others")

    # ("0012", "0004") is exempt because ADR-0012 keeps the clause in full,
    # narrowing only an inference that was never part of it. If ADR-0012 stops
    # saying so, the clause is superseded after all and has to move.
    assert "kept in full" in _read(_by_number("0012")), (
        "ADR-0012 no longer says it keeps ADR-0004's clause in full, so that "
        "exemption has lost its ground")


@pytest.mark.parametrize("path", ADRS, ids=_number)
def test_the_superseded_section_sits_below_the_record_and_above_any_closing(path):
    text = _read(path)
    if SUPERSEDED not in text:
        return
    assert text.index("## Site config touched") < text.index(SUPERSEDED.strip()), (
        "`## Superseded wording` precedes `## Site config touched` in %s" % path)
    if CLOSING in text:
        assert text.index(SUPERSEDED.strip()) < text.index(CLOSING), (
            "`## Superseded wording` follows the closing paragraph in %s" % path)


@pytest.mark.parametrize("path", ADRS, ids=_number)
def test_the_closing_paragraph_is_the_records_last_words(path):
    """TEMPLATE.md puts it last and it is quoted verbatim as a citation target,
    so a section added below it would displace the thing being cited."""
    text = _read(path)
    if CLOSING not in text:
        return
    assert "\n## " not in text.split(CLOSING, 1)[1], (
        "a section follows the closing paragraph in %s" % path)
