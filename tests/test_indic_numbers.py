import random

import pytest

from munim.scribe.indic_numbers import (
    WORD_VALUE,
    format_inr,
    paise_to_words,
    parse_amount,
    parse_amounts,
    rupees_to_words,
)

RS = 100  # paise per rupee

CASES = [
    # idioms from the benchmark truth set
    ("dedh sau", 150), ("dhai sau", 250), ("sava sau", 125), ("paune do sau", 175),
    ("saadhe teen sau", 350), ("dedh hazaar", 1500), ("dhai hazaar", 2500),
    ("sava lakh", 125000), ("do lakh pachaas hazaar", 250000),
    ("pachattar hazaar chaar sau chaalis", 75440),
    # plain
    ("ek sau", 100), ("sau", 100), ("do sau", 200), ("paanch sau", 500), ("pandrah sau", 1500),
    ("ek hazaar", 1000), ("ek hazaar paanch sau", 1500), ("das hazaar", 10000),
    ("ek lakh", 100000), ("ek crore", 10_000_000), ("pachaas", 50), ("das", 10),
    ("bees", 20), ("ninyaanave", 99), ("ek sau ek", 101), ("do hazaar do", 2002),
    ("teen sau chaalis hazaar", 340000), ("ek sau pachaas hazaar", 150000),
    ("saadhe teen hazaar", 3500), ("paune do lakh", 175000), ("sava hazaar", 1250),
    ("dhai lakh", 250000), ("do hazaar dedh sau", 2150),
    # spelling variants
    ("pachas", 50), ("panchas", 50), ("hazar", 1000), ("do hajar", 2000), ("lac", 100000),
    ("char sau", 400), ("chaar sau", 400), ("chhe sau", 600), ("pachchees", 25),
    ("sadhe teen sau", 350), ("sawa sau", 125), ("dhaai sau", 250), ("derh sau", 150),
    # digits and symbols
    ("150", 150), ("₹150", 150), ("Rs 150", 150), ("150 rupaye", 150), ("rs.150", 150),
    ("1,50,000", 150000), ("2k", 2000), ("1.5k", 1500), ("5 sau", 500), ("2 lakh", 200000),
    ("2.5 hazaar", 2500), ("१५०", 150),
    # Devanagari
    ("डेढ़ सौ", 150), ("ढाई सौ", 250), ("सवा सौ", 125), ("साढ़े तीन सौ", 350),
    ("पौने दो सौ", 175), ("पचहत्तर हज़ार चार सौ चालीस", 75440), ("पचास", 50),
    ("दो लाख पचास हज़ार", 250000), ("एक हज़ार", 1000), ("डेढ़ हजार", 1500),
    # in-sentence
    ("Sharma ji ko 150 udhaar likh do", 150),
    ("Gupta ji ke naam dedh sau rupaye", 150),
    ("sharma ji ne paanch sau diye", 500),
]


@pytest.mark.parametrize("text,rupees", CASES)
def test_parse_amount(text, rupees):
    assert parse_amount(text) == rupees * RS


def test_paise_suffix():
    spans = parse_amounts("pachaas paise")
    assert spans[0].paise == 50


def test_multi_amount_returns_two_spans():
    spans = [s for s in parse_amounts("Sharma ji dedh sau aur dhai hazaar") if s.valid]
    assert [s.paise for s in spans] == [150 * RS, 2500 * RS]
    assert sum(s.paise for s in spans) == 2650 * RS


def test_quantity_not_amount():
    spans = parse_amounts("do kilo cheeni 150 udhaar")
    assert spans[0].is_quantity and not spans[1].is_quantity
    assert parse_amount("do kilo cheeni 150 udhaar") == 150 * RS


def test_time_not_amount():
    assert parse_amount("paanch baje aana") is None


@pytest.mark.parametrize("text", ["ek do", "dedh", "saadhe sau", "sava teen", "dedh dhai sau"])
def test_malformed_is_invalid(text):
    spans = parse_amounts(text)
    assert spans and not any(s.valid for s in spans)


def test_no_number():
    assert parse_amounts("namaste kaise ho") == []


def test_word_table_covers_all_0_to_99():
    assert set(WORD_VALUE.values()) == set(range(100))


def test_roundtrip_roman_and_deva():
    rng = random.Random(7)
    values = list(range(1, 2001)) + [rng.randint(1, 99_999_999) for _ in range(3000)]
    values += [10 ** k for k in range(1, 8)] + [75440, 250000, 125000, 99_99_999]
    for n in values:
        for script in ("roman", "deva"):
            words = rupees_to_words(n, script)
            assert parse_amount(words) == n * RS, (n, script, words)


def test_format_inr():
    assert format_inr(25_000_000) == "₹2,50,000"
    assert format_inr(15_000) == "₹150"
    assert format_inr(15_050) == "₹150.50"
    assert format_inr(7_544_000) == "₹75,440"
    assert format_inr(12_500_000) == "₹1,25,000"
    assert format_inr(0) == "₹0"
    assert format_inr(100_000_000) == "₹10,00,000"


def test_spoken_words():
    assert rupees_to_words(75440, "roman") == "pachhattar hazaar chaar sau chaalees"
    assert paise_to_words(15000, "roman") == "ek sau pachaas rupaye"
    assert "रुपये" in paise_to_words(15000)


def test_das_surname_documented_risk():
    # 'Das' alone parses as 10 — extractor must mask known customer names first.
    assert parse_amount("Das") == 10 * RS
