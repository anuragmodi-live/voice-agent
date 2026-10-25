import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from adaptive_turn import AdaptiveSpeechTimeoutUserTurnStopStrategy, wait_secs
from hindi_text import could_be_marker_prefix, infer_expected_answer, split_leading_marker, strip_answer_marker


def test_wait_ordering_short_lt_normal_lt_long():
    assert wait_secs("short") < wait_secs("normal") < wait_secs("long")
    assert wait_secs("garbage") == wait_secs("normal")


def test_strategy_timeout_follows_expected_answer():
    s = AdaptiveSpeechTimeoutUserTurnStopStrategy(session_id="t")
    assert s._user_speech_timeout == wait_secs("normal")
    s.set_expected_answer("long")
    assert s._user_speech_timeout == wait_secs("long") and s.expected_answer == "long"
    s.set_expected_answer("short")
    assert s._user_speech_timeout == wait_secs("short")
    s.set_expected_answer("nonsense")
    assert s._user_speech_timeout == wait_secs("normal") and s.expected_answer == "normal"


def test_env_overrides(monkeypatch=None):
    import os
    os.environ["TURN_WAIT_LONG_SECS"] = "3.5"
    try:
        assert wait_secs("long") == 3.5
    finally:
        del os.environ["TURN_WAIT_LONG_SECS"]


def test_marker_parsing():
    assert strip_answer_marker("<<ANS:SHORT>> क्या आप हाँ कहेंगे?") == ("क्या आप हाँ कहेंगे?", "short")
    assert strip_answer_marker("कोई मार्कर नहीं") == ("कोई मार्कर नहीं", None)
    assert strip_answer_marker("<< ans : long >>अपना ईमेल बताइए") == ("अपना ईमेल बताइए", "long")
    assert split_leading_marker("<<ANS:LONG>> अपना ") == ("long", "अपना ")
    assert split_leading_marker("अपना") == (None, "अपना")


def test_prefix_detection():
    for head in ["", " ", "<", "<<", "<<AN", "<<ANS:", "<<ANS:SHO", "<<ANS:SHORT>"]:
        assert could_be_marker_prefix(head), head
    for head in ["नमस्ते", "<<नमस्ते", "इस महीने", "<<ANS:SHORT>> क्या"[:0] + "x"]:
        assert not could_be_marker_prefix(head), head


def test_inference_when_marker_is_missing():
    assert infer_expected_answer("ये टॉप पाँच हैं। क्या आप पूरी लिस्ट सुनना चाहेंगे?") == "short"
    assert infer_expected_answer("बकाया बत्तीस लाख रुपये है। क्या आप यह भी जानना चाहेंगे?") == "short"
    assert infer_expected_answer("ठीक है। कृपया अपना ईमेल बताइए?") == "long"
    assert infer_expected_answer("आपका आधार नंबर क्या है?") == "long"
    assert infer_expected_answer("कृपया अपना पूरा नाम बताइए?") == "long"
    # open / wh-questions and plain statements must NOT be cut off
    assert infer_expected_answer("नमस्ते! आप अपने बिज़नेस के बारे में क्या जानना चाहेंगे?") == "normal"
    assert infer_expected_answer("क्या आप बता सकते हैं कि कौन सा महीना?") == "normal"
    assert infer_expected_answer("क्या आप जानना चाहेंगे कि कितने दिन से अटका है?") == "normal"
    assert infer_expected_answer("इस महीने सत्रह लाख रुपये की बिक्री रही।") == "normal"
    assert infer_expected_answer("") == "normal"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
