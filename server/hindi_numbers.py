"""Hindi number words, both directions -- pure Python, no pipecat dependency.

Why this exists: the voice agent is required to speak in pure Devanagari
(numbers included), because Sarvam's TTS mispronounces Latin-script words and
digit strings inside Hindi sentences. That has two consequences this module
serves:

1. words_from_number()/number_to_hindi_words() -- the deterministic converter
   used (a) by the engine to build the "<field>_inr_words" phrase the LLM
   reads verbatim, and (b) by hindi_text.normalize_for_speech() as a safety
   net that rewrites any digit the LLM still emits before it reaches TTS.
   num2words has no Hindi support, so the 0-99 table is spelled out here.

2. extract_spoken_numbers() -- the reverse parse (Devanagari words -> value),
   so hallucination_check.py can still verify that a narrated figure like
   "पाँच दशमलव पाँच करोड़" matches the engine's 55000000. Without it, moving
   the narration from digits to words would silently blind the grounding check.

Matching is spelling-tolerant: nukta is dropped and chandrabindu folded into
anusvara on BOTH the table and the input (पाँच == पांच, हज़ार == हजार,
करोड़ == करोड), and the common alternate spellings (छह/छः, बाईस/बाइस ...) are
listed explicitly.
"""
from __future__ import annotations

import re
import unicodedata

_UNITS_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस "
    "ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस "
    "इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस "
    "इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस "
    "इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास "
    "इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर "
    "इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी "
    "इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे "
    "इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()
assert len(_UNITS_0_99) == 100

# Spellings an LLM plausibly produces instead of the canonical word above.
_UNIT_VARIANTS = {
    "छः": 6, "छै": 6, "पन्द्रह": 15, "अट्ठारह": 18, "बाइस": 22, "तेइस": 23,
    "सत्ताइस": 27, "अठाईस": 28, "अठाइस": 28, "तैतीस": 33, "चौतीस": 34, "पैतीस": 35,
    "सैतीस": 37, "तैतालीस": 43, "चौआलीस": 44, "पैतालीस": 45, "सैतालीस": 47,
    "तिरेपन": 53, "चौअन": 54, "छप्पन": 56, "अठावन": 58, "चौसठ": 64, "पैसठ": 65,
    "सरसठ": 67, "अरसठ": 68, "सतहत्तर": 77, "अठहत्तर": 78, "उन्यासी": 79, "इक्यासी": 81,
    "चौरासी": 84, "छियासी": 86, "सतासी": 87, "अट्ठासी": 88, "नब्बे": 90, "निन्यानबे": 99,
}

# Scale words (value), spoken Indian-style: सौ / हज़ार / लाख / करोड़ / अरब.
_SCALES = {"सौ": 100, "हज़ार": 1_000, "लाख": 100_000, "करोड़": 10_000_000, "अरब": 1_000_000_000}
_SCALE_VARIANTS = {"सैकड़ा": 100, "लाख़": 100_000}

# Fractional prefixes that bind to the NEXT number word: साढ़े पाँच = 5.5.
_FRACTION_PREFIX = {"साढ़े": 0.5, "सवा": 0.25, "पौने": -0.25}
_STANDALONE_FRACTION = {"डेढ़": 1.5, "ढाई": 2.5, "पौन": 0.75}
_DECIMAL_WORDS = {"दशमलव", "प्वाइंट", "पॉइंट", "प्वॉइंट"}


def _norm(word: str) -> str:
    """Spelling-tolerant key: drop nukta, fold chandrabindu into anusvara."""
    decomposed = unicodedata.normalize("NFD", word)
    decomposed = decomposed.replace("़", "").replace("ँ", "ं")
    return unicodedata.normalize("NFC", decomposed)


_WORD_TO_UNIT: dict[str, int] = {_norm(w): i for i, w in enumerate(_UNITS_0_99)}
_WORD_TO_UNIT.update({_norm(w): v for w, v in _UNIT_VARIANTS.items()})
_WORD_TO_SCALE: dict[str, int] = {_norm(w): v for w, v in {**_SCALES, **_SCALE_VARIANTS}.items()}
_WORD_TO_FRAC_PREFIX = {_norm(w): v for w, v in _FRACTION_PREFIX.items()}
_WORD_TO_FRAC_STANDALONE = {_norm(w): v for w, v in _STANDALONE_FRACTION.items()}
_DECIMAL_KEYS = {_norm(w) for w in _DECIMAL_WORDS}


# ---------------------------------------------------------------- number -> words

def _int_to_words(n: int) -> str:
    if n < 100:
        return _UNITS_0_99[n]
    parts: list[str] = []
    crore, rest = divmod(n, 10_000_000)
    if crore:
        parts.append(f"{_int_to_words(crore)} करोड़")
    lakh, rest = divmod(rest, 100_000)
    if lakh:
        parts.append(f"{_UNITS_0_99[lakh]} लाख")
    thousand, rest = divmod(rest, 1_000)
    if thousand:
        parts.append(f"{_UNITS_0_99[thousand]} हज़ार")
    hundred, rest = divmod(rest, 100)
    if hundred:
        parts.append(f"{_UNITS_0_99[hundred]} सौ")
    if rest:
        parts.append(_UNITS_0_99[rest])
    return " ".join(parts)


def number_to_hindi_words(value: float | int | str) -> str:
    """12 -> "बारह"; 5.5 -> "पाँच दशमलव पाँच"; 1250000 -> "बारह लाख पचास हज़ार";
    -3 -> "ऋण तीन". Fractional digits are read one by one, as spoken Hindi does.
    Accepts a numeric string (so "0.50" keeps its trailing zero as written)."""
    text = value if isinstance(value, str) else repr(float(value)) if isinstance(value, float) else str(value)
    text = text.replace(",", "").strip()
    negative = text.startswith("-")
    text = text.lstrip("-+")
    if "e" in text.lower():  # repr() of a very large/small float -- fall back to fixed notation
        text = f"{float(text):.6f}".rstrip("0").rstrip(".")
    whole, _, frac = text.partition(".")
    words = _int_to_words(int(whole or "0"))
    if frac and frac.strip("0") != "":
        words += " दशमलव " + " ".join(_UNITS_0_99[int(d)] for d in frac)
    return ("ऋण " if negative else "") + words


# ---------------------------------------------------------------- words -> number

_TOKEN_RE = re.compile(r"[ऀ-ॿ]+")


def _evaluate_run(tokens: list[tuple[str, str]]) -> list[float]:
    """Turns one maximal run of number-ish tokens into the value(s) it spells.
    tokens: [(kind, key)] where kind is unit|scale|decimal|prefix|fraction."""
    values: list[float] = []
    total = 0
    current = 0
    have_unit = False  # a unit/fraction word has contributed to `current`
    prev_kind = None
    frac_digits = ""
    in_fraction = False
    prefix: float | None = None
    saw_number_word = False

    def finish():
        nonlocal total, current, have_unit, frac_digits, in_fraction, prefix, saw_number_word
        if saw_number_word:
            value = total + current
            if frac_digits:
                value += float("0." + frac_digits)
            values.append(round(float(value), 6))
        total = current = 0
        have_unit = False
        frac_digits = ""
        in_fraction = False
        prefix = None
        saw_number_word = False

    for kind, key in tokens:
        if kind == "unit":
            n = _WORD_TO_UNIT[key]
            if in_fraction:
                frac_digits += str(n)  # digits one by one, or a two-digit word whole
                prev_kind = "unit"
                saw_number_word = True
                continue
            if prev_kind == "unit" and have_unit:
                # two bare unit words in a row ("तीन चार") are two numbers, not one
                finish()
            current += n
            if prefix is not None:
                current += prefix  # साढ़े/सवा/पौने bind to this unit: साढ़े पाँच = 5.5
                prefix = None
            have_unit = True
            saw_number_word = True
            prev_kind = "unit"
        elif kind == "scale":
            mult = _WORD_TO_SCALE[key]
            if prefix is not None and not current:
                current = 1 + prefix  # "सवा लाख" = 1.25 लाख
                prefix = None
            if in_fraction:
                # "पाँच दशमलव पाँच करोड़" -- the scale applies to the whole decimal number
                base = current + float("0." + frac_digits) if frac_digits else current
                total += base * mult
                current = 0
                frac_digits = ""
                in_fraction = False
            elif mult == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * mult
                current = 0
            have_unit = False
            saw_number_word = True
            prev_kind = "scale"
        elif kind == "decimal":
            in_fraction = True
            prev_kind = "decimal"
        elif kind == "prefix":
            prefix = _WORD_TO_FRAC_PREFIX[key]
            prev_kind = "prefix"
        elif kind == "fraction":
            if have_unit or total:
                finish()
            current += _WORD_TO_FRAC_STANDALONE[key]
            have_unit = True
            saw_number_word = True
            prev_kind = "unit"
    finish()
    return values


def extract_spoken_numbers(text: str) -> list[float]:
    """Every number spelled in Devanagari words inside `text`, e.g.
    "पाँच दशमलव पाँच करोड़ रुपये" -> [55000000.0]. A run made only of scale
    words ("लाख" after digits that the digit path already handled) yields
    nothing, so mixed forms are never double counted."""
    tokens: list[tuple[str, str]] = []
    values: list[float] = []

    def flush():
        nonlocal tokens
        if any(kind in ("unit", "fraction", "prefix") for kind, _ in tokens):
            values.extend(_evaluate_run(tokens))
        tokens = []

    last_end = 0
    for m in _TOKEN_RE.finditer(text):
        # Any non-space separator (digit, Latin letter, punctuation other than a
        # comma/space) between two words ends the run.
        gap = text[last_end:m.start()]
        if tokens and re.search(r"[^\s,]", gap):
            flush()
        last_end = m.end()
        key = _norm(m.group())
        if key in _DECIMAL_KEYS:
            tokens.append(("decimal", key))
        elif key in _WORD_TO_FRAC_PREFIX:
            tokens.append(("prefix", key))
        elif key in _WORD_TO_FRAC_STANDALONE:
            tokens.append(("fraction", key))
        elif key in _WORD_TO_UNIT:
            tokens.append(("unit", key))
        elif key in _WORD_TO_SCALE:
            tokens.append(("scale", key))
        else:
            flush()
    flush()
    return values
