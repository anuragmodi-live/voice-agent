"""Deterministic last-line-of-defence for the Devanagari-only speech rule.

The system prompt tells the LLM to write every spoken word -- numbers and
English loanwords included -- in Devanagari, because Sarvam's TTS mispronounces
Latin-script words and digit strings embedded in Hindi sentences. A prompt is
a request, not a guarantee (long answers in particular drift back to Roman
script), so normalize_for_speech() is applied to the LLM's text on its way to
TTS (see speech_normalizer.py) and to the /ask text path:

- digits, ₹/Rs amounts and % -> Hindi words (exact, via hindi_numbers.py)
- a table of common business loanwords -> their Devanagari spelling
- everything else in Latin script is left alone but REPORTED by latin_leaks(),
  so the caller can log it and nudge the model back to Devanagari. There is
  deliberately no generic Latin->Devanagari transliterator: a rule-based one
  would mangle names and be worse than the TTS's own handling.

Pure Python, no pipecat import, so the HTTP dev path can use it too.
"""
from __future__ import annotations

import re

from hindi_numbers import number_to_hindi_words

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_NUM = r"\d[\d,]*(?:\.\d+)?"
_NEG = r"(?<![\w.])(?P<neg>-)?"
_SCALE_WORD = r"(?P<u>lakhs?|lacs?|crores?|thousand|लाख|करोड़|हज़ार|हजार)"
# ₹ 5,000 / Rs. 2.5 crore / INR 5000 / रु. 5000 -> "<words> [<unit>] रुपये"
_CURRENCY_RE = re.compile(
    rf"{_NEG}(?:₹|\bRs\.?|\bINR\b|रु\.?)\s*(?P<n>{_NUM})(?:\s*{_SCALE_WORD}\b)?", re.IGNORECASE
)
_PERCENT_RE = re.compile(rf"{_NEG}(?P<n>{_NUM})\s*(?:%|\bpercent\b|\bper cent\b)", re.IGNORECASE)
# A number not glued to letters (so product codes like BL972 are left alone).
_NUMBER_RE = re.compile(rf"{_NEG}(?P<n>{_NUM})(?![\w])")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’]*")

_LOANWORDS = {
    # business terms
    "gross margin": "ग्रॉस मार्जिन", "net margin": "नेट मार्जिन", "margin": "मार्जिन",
    "gross profit": "ग्रॉस प्रॉफ़िट", "net profit": "नेट प्रॉफ़िट", "profit": "प्रॉफ़िट",
    "revenue": "रेवेन्यू", "sales": "सेल्स", "sale": "सेल", "loss": "लॉस",
    "cash flow": "कैश फ़्लो", "cash": "कैश", "cogs": "कॉस्ट ऑफ़ गुड्स सोल्ड",
    "customers": "कस्टमर्स", "customer": "कस्टमर", "suppliers": "सप्लायर्स", "supplier": "सप्लायर",
    "invoices": "इनवॉइसेस", "invoice": "इनवॉइस", "inventory": "इन्वेंटरी", "stock": "स्टॉक",
    "products": "प्रोडक्ट्स", "product": "प्रोडक्ट", "receivables": "रिसीवेबल्स",
    "payables": "पेएबल्स", "purchase": "परचेज़", "orders": "ऑर्डर्स", "order": "ऑर्डर",
    "overdue": "ओवरड्यू", "discount": "डिस्काउंट", "trend": "ट्रेंड", "quarter": "क्वार्टर",
    "bank": "बैंक", "gst": "जीएसटी", "emi": "ईएमआई", "ai": "एआई", "top": "टॉप",
    "laptop": "लैपटॉप", "smartphone": "स्मार्टफ़ोन", "phone": "फ़ोन", "headphones": "हेडफ़ोन्स",
    "ok": "ओके", "okay": "ओके",
    # units spoken in the same breath as a figure
    "lakh": "लाख", "lakhs": "लाख", "lac": "लाख", "crore": "करोड़", "crores": "करोड़",
    "rupees": "रुपये", "rupee": "रुपया", "rs": "रुपये",
    # months
    "january": "जनवरी", "february": "फ़रवरी", "march": "मार्च", "april": "अप्रैल", "may": "मई",
    "june": "जून", "july": "जुलाई", "august": "अगस्त", "september": "सितंबर",
    "october": "अक्टूबर", "november": "नवंबर", "december": "दिसंबर",
}
_LOANWORD_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(k) for k in sorted(_LOANWORDS, key=len, reverse=True))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _spoken(n: str, negative: bool = False) -> str:
    words = number_to_hindi_words(n)
    return ("ऋण " if negative else "") + words


def _currency(m: re.Match) -> str:
    unit = m.group("u")
    unit_word = f" {_LOANWORDS[unit.lower()]}" if unit and unit.lower() in _LOANWORDS else (f" {unit}" if unit else "")
    return f"{_spoken(m.group('n'), bool(m.group('neg')))}{unit_word} रुपये"


def normalize_for_speech(text: str) -> str:
    """Rewrites digits/symbols/known loanwords into Devanagari words."""
    text = text.translate(_DEVANAGARI_DIGITS)
    text = _CURRENCY_RE.sub(_currency, text)
    text = _PERCENT_RE.sub(lambda m: f"{_spoken(m.group('n'), bool(m.group('neg')))} प्रतिशत", text)
    text = _NUMBER_RE.sub(lambda m: _spoken(m.group("n"), bool(m.group("neg"))), text)
    text = _LOANWORD_RE.sub(lambda m: _LOANWORDS[m.group().lower()], text)
    return text


# The model prefixes a reply with <<ANS:SHORT|NORMAL|LONG>> to declare how long
# the caller's answer to its question will be (see adaptive_turn.py). It is a
# control token, never speech, so it is stripped before TTS and before logging.
_ANSWER_MARKER_RE = re.compile(r"\s*<<\s*ANS\s*:\s*(SHORT|NORMAL|LONG)\s*>>\s*", re.IGNORECASE)
_MARKER_OPENING = "<<ANS:"


def strip_answer_marker(text: str) -> tuple[str, str | None]:
    """(text without any answer marker, the last marker's kind in lowercase or None)."""
    kinds = [m.group(1).lower() for m in _ANSWER_MARKER_RE.finditer(text)]
    return (_ANSWER_MARKER_RE.sub(" ", text).strip() if kinds else text), (kinds[-1] if kinds else None)


# Fallback for when the model forgets the marker (it often does: in a live
# check it emitted none, even on an obvious yes/no question). Reads the reply's
# own closing question, conservatively -- an open question wrongly judged SHORT
# would cut the caller off, so anything unclear stays NORMAL.
_LONG_ANSWER_RE = re.compile(
    r"ईमेल|ई-मेल|मेल आईडी|कार्ड|आधार|पैन\b|फ़ोन नंबर|फोन नंबर|मोबाइल नंबर|पूरा नाम|स्पेलिंग|नंबर बताइ"
)
_WH_WORD_RE = re.compile(r"कौन|कितन|कब\b|कहाँ|कहां|कैसे|क्यों|किस|कैसा|कैसी")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[।?!.])\s+")


def infer_expected_answer(text: str) -> str:
    """"long" / "short" / "normal" from the closing question of a spoken reply."""
    text = text.strip()
    if not text.endswith("?"):
        return "normal"
    question = _SENTENCE_SPLIT_RE.split(text)[-1]
    if _LONG_ANSWER_RE.search(question):
        return "long"
    # Yes/no question: opens with "क्या" and contains no other question word
    # anywhere. Even "क्या आप जानना चाहेंगे कि कितने दिन...?" stays NORMAL: it
    # is ambiguous whether that is an offer or a request, and NORMAL is the
    # safe side of that doubt.
    if question.startswith("क्या") and not _WH_WORD_RE.search(question):
        return "short"
    return "normal"


def split_leading_marker(head: str) -> tuple[str | None, str]:
    """For the first streamed text of a reply: (marker kind or None, the text
    after the marker). Unlike strip_answer_marker this keeps the remaining
    text's whitespace intact, since more chunks are about to be appended to it."""
    m = _ANSWER_MARKER_RE.match(head)
    if not m:
        return None, head
    return m.group(1).lower(), head[m.end():]


def could_be_marker_prefix(head: str) -> bool:
    """True while `head` (the first streamed chunks of a reply) might still turn
    into an answer marker, so the caller should keep holding it back."""
    squashed = re.sub(r"\s+", "", head).upper()
    if not squashed:
        return True
    if _MARKER_OPENING.startswith(squashed):
        return True
    return squashed.startswith(_MARKER_OPENING) and ">>" not in squashed and len(squashed) <= 20


def latin_leaks(text: str) -> list[str]:
    """Latin-script words still present (after normalize_for_speech) --
    entity names, product codes, anything not in the loanword table."""
    return _LATIN_WORD_RE.findall(text)
