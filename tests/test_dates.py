from datetime import date

import pytest

from munim.scribe.dates_hi import describe, parse_when

MON = date(2026, 11, 2)       # a Monday


@pytest.mark.parametrize("text,expect", [
    ("aaj", date(2026, 11, 2)),
    ("kal tak de denge", date(2026, 11, 3)),
    ("parson tak", date(2026, 11, 4)),
    ("shukravar tak", date(2026, 11, 6)),
    ("somvar tak", date(2026, 11, 9)),                # same weekday -> next week
    ("शुक्रवार तक", date(2026, 11, 6)),
    ("Friday tak", date(2026, 11, 6)),
    ("teen din mein", date(2026, 11, 5)),
    ("3 din baad", date(2026, 11, 5)),
    ("agle hafte", date(2026, 11, 9)),
    ("mahine ke end tak", date(2026, 11, 30)),
    ("15 tarikh tak", date(2026, 11, 15)),
    ("1 tarikh ko", date(2026, 12, 1)),              # already past this month
    ("10 november", date(2026, 11, 10)),
    ("1 november", date(2027, 11, 1)),               # already past this year
    ("agle mahine", date(2026, 12, 1)),
])
def test_parse(text, expect):
    w = parse_when(text, MON)
    assert w is not None and w.day == expect, (text, w)


def test_ambiguity_noted_for_kal_parson():
    assert parse_when("kal", MON).ambiguous and parse_when("parson", MON).ambiguous
    assert parse_when("shukravar", MON).ambiguous is None


@pytest.mark.parametrize("text", ["salary aate hi", "jab ho jayega", "pata nahi", "31 tarikh tak"][:3])
def test_unknown_returns_none(text):
    assert parse_when(text, MON) is None


def test_31_skips_short_month():
    w = parse_when("31 tarikh tak", date(2026, 11, 2))      # Nov has 30 days -> Dec 31
    assert w.day == date(2026, 12, 31)


def test_describe():
    assert describe(date(2026, 11, 6)) == "Shukravar 6 Nov"
