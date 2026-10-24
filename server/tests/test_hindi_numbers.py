import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from hindi_numbers import extract_spoken_numbers as ex, number_to_hindi_words as w


def test_roundtrip_every_value_up_to_a_crore_sample():
    for n in list(range(0, 1000)) + [1234, 12500, 99999, 100000, 1250000, 10450000, 55000000, 123456789]:
        assert ex(w(n)) == [float(n)], (n, w(n), ex(w(n)))


def test_decimals_roundtrip():
    for text, val in [("5.5", 5.5), ("17.35", 17.35), ("0.5", 0.5), ("12.05", 12.05)]:
        assert ex(w(text)) == [val], (text, w(text), ex(w(text)))


def test_scaled_decimal_phrase():
    assert ex("पाँच दशमलव पाँच करोड़ रुपये") == [55_000_000.0]
    assert ex("सत्रह दशमलव तीन पाँच लाख रुपये") == [1_735_000.0]


def test_spelling_variants():
    assert ex("पांच करोड") == [50_000_000.0]
    assert ex("बारह हजार") == [12000.0]
    assert ex("बाइस") == [22.0]


def test_fraction_words():
    assert ex("साढ़े पाँच करोड़") == [55_000_000.0]
    assert ex("सवा लाख") == [125_000.0]
    assert ex("डेढ़ लाख") == [150_000.0]
    assert ex("ढाई हज़ार") == [2500.0]
    assert ex("पौने दो लाख") == [175_000.0]


def test_compound_and_separate_numbers():
    assert ex("दस करोड़ पैंतालीस लाख") == [104_500_000.0]
    assert ex("तीन चार") == [3.0, 4.0]
    assert ex("पाँच, सात और नौ") == [5.0, 7.0, 9.0]


def test_ignores_bare_scale_and_plain_text():
    assert ex("17 लाख रुपये") == []
    assert ex("आपकी बिक्री अच्छी रही") == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
