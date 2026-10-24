import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from hallucination_check import check_narration
from hindi_text import latin_leaks, normalize_for_speech as n


def test_digits_become_words():
    assert n("इस महीने 17 लाख") == "इस महीने सत्रह लाख"
    assert n("मार्जिन 5.5%") == "मार्जिन पाँच दशमलव पाँच प्रतिशत"
    assert n("5 percent") == "पाँच प्रतिशत"
    assert n("30 June 2026 तक") == "तीस जून दो हज़ार छब्बीस तक"
    assert n("१२ ग्राहक") == "बारह ग्राहक"


def test_currency_symbols():
    assert n("₹45,230 का sale") == "पैंतालीस हज़ार दो सौ तीस रुपये का सेल"
    assert n("Rs. 2.5 crore") == "दो दशमलव पाँच करोड़ रुपये"
    assert n("₹ 5 लाख") == "पाँच लाख रुपये"


def test_negative_and_range():
    assert n("मुनाफ़ा -20.87% गिरा") == "मुनाफ़ा ऋण बीस दशमलव आठ सात प्रतिशत गिरा"
    assert n("10-12 दिन") == "दस-बारह दिन"


def test_product_codes_are_left_alone_and_reported():
    out = n("Product BL972 का रेवेन्यू")
    assert "BL972" in out
    assert "BL972" in latin_leaks(out)


def test_clean_devanagari_has_no_leaks():
    assert latin_leaks(n("इस महीने आपकी revenue 17 लाख रही")) == []


def test_grounding_understands_number_words():
    grounded = [55_000_000.0]
    ok = check_narration("इस महीने आपकी बिक्री पाँच दशमलव पाँच करोड़ रुपये रही", grounded)
    assert ok["grounded"], ok
    assert 55_000_000.0 in ok["checked"]
    bad = check_narration("इस महीने आपकी बिक्री पचपन लाख रुपये रही", grounded)  # the classic 10x slip
    assert not bad["grounded"], bad
    assert bad["unmatched"] == [5_500_000.0]


def test_grounding_digit_forms_still_work():
    assert check_narration("17 लाख रुपये", [1_700_000.0])["grounded"]
    assert not check_narration("50 crore", [10_450_000.0])["grounded"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
