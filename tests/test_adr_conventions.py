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


def test_there_are_twenty_one_adrs_numbered_without_gaps():
    numbers = [_number(p) for p in ADRS]
    assert numbers == ["%04d" % i for i in range(1, len(numbers) + 1)]
    assert len(numbers) == 21


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


def test_every_narrows_line_quotes_text_its_target_still_contains():
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
            body = target_text.split("\n## Context", 1)[1]
            # The quote must be a substring of the narrowed record's BODY, not
            # merely of its Status note about being narrowed. Compare words,
            # not markup: the body may wrap the clause across lines and carry
            # emphasis or code spans the quote does not.
            assert _words(clause) in _words(body), (
                "ADR-%s narrows ADR-%s on %r, which ADR-%s's body no longer contains"
                % (_number(path), target, clause, target))
            assert "narrowed by ADR-%s" % _number(path) in target_text, (
                "ADR-%s lacks the reciprocal Status note for ADR-%s" % (target, _number(path)))
    assert seen >= 4, "expected at least the four known Narrows lines"
