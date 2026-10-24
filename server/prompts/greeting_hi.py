# greeting_hi.py
"""The opening line, spoken verbatim at the start of every call.

Previously bot.py asked the LLM to improvise the greeting on every connection:
a full LLM round trip (the ~7k-token system prompt + tool schema, then
generation) before the caller heard anything, for a line that never needs to
vary. It is now a constant handed straight to TTS as a TTSSpeakFrame, so the
first sound is gated only by TTS itself. TTSSpeakFrame text is appended to the
LLM context as an assistant message, so the model still knows it already
greeted and asked its opening question.

Pure Devanagari, no digits -- same rule as everything the agent speaks (see
system_prompt_hi.py). Override per deployment with the GREETING_TEXT env var.
"""
import os

DEFAULT_GREETING_TEXT = (
    "नमस्ते! मैं आशा हूँ, आपके बिज़नेस के नंबर समझने में आपकी मदद करती हूँ। "
    "बताइए, आज आप अपने बिज़नेस के बारे में क्या जानना चाहेंगे?"
)

GREETING_TEXT = os.getenv("GREETING_TEXT") or DEFAULT_GREETING_TEXT
