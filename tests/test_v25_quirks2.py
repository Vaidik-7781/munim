import pytest
from munim.scribe.indic_numbers import parse_amount
from munim.scribe.names import Customer, resolve

CASES = [
    ("\u092f\u093e\u0926\u0935 \u091c\u0940 \u0915\u094b \u092a\u094c\u0928\u0947 \u20b9200 \u0909\u0927\u093e\u0930 \u0932\u093f\u0916 2", 17500),
    ("\u0936\u0930\u094d\u092e\u093e \u091c\u0940 \u0915\u094b \u092a\u094c\u0928\u0947 \u20b92,00,000 \u0909\u0927\u093e\u0930 \u0932\u093f\u0916 2", 17500000),
    ("\u092e\u093f\u0936\u094d\u0930\u093e \u091c\u0940 03:30 \u20b9100 \u0909\u0927\u093e\u0930 \u0932\u093f\u0916 2", 35000),
    ("\u0938\u0935\u093e \u20b9200", 22500),
    ("\u0938\u093e\u0922\u093c\u0947 \u20b9300", 35000),
    ("\u20b9200 \u0909\u0927\u093e\u0930", 20000),            # a real Rs200 alone is untouched
    ("\u20b92,00,000 \u0909\u0927\u093e\u0930", 20000000),
    ("\u092a\u094c\u0928\u0947 \u20b9150", 15000),            # not a round multiple -> untouched
    ("\u092e\u093f\u0936\u094d\u0930\u093e \u091c\u0940 \u0915\u094b 350\u0930\u0941\u092a\u092f\u0947 \u0909\u0927\u093e\u0930", 35000),  # digits glued to Devanagari
]


@pytest.mark.parametrize("text,paise", CASES)
def test_v25_fraction_round(text, paise):
    assert parse_amount(text) == paise


@pytest.mark.parametrize("name", ["\u0938\u093f\u0902\u0939", "\u0938\u093f\u0902\u0917", "Singh"])
def test_singh_devanagari_resolves(name):
    r = resolve(name + " \u091c\u0940", [Customer(1, "Sharma"), Customer(2, "Singh"), Customer(3, "Sinha")])
    assert r.status == "match" and r.customer.name == "Singh"


def test_sinha_still_distinct():
    r = resolve("\u0938\u093f\u0928\u094d\u0939\u093e \u091c\u0940", [Customer(2, "Singh"), Customer(3, "Sinha")])
    assert r.customer is None or r.customer.name == "Sinha"
