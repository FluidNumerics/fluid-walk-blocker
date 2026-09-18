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
    """The `## Superseded wording` body, or None. The closing paragraph sits
    below that section (ADR-0005 has both), so it is cut off here rather than
    counted as part of it."""
    if SUPERSEDED not in text:
        return None
    return text.split(SUPERSEDED, 1)[1].split(CLOSING, 1)[0]


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


def test_there_are_twenty_three_adrs_numbered_without_gaps():
    numbers = [_number(p) for p in ADRS]
    assert numbers == ["%04d" % i for i in range(1, len(numbers) + 1)]
    assert len(numbers) == 23


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
    assert seen >= 8, "expected at least the eight known Narrows lines"


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
