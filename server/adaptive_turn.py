"""Turn-end wait that adapts to what the agent just asked.

How long to wait after the caller pauses before treating their turn as done is
a trade-off that differs by question. A yes/no or one-word answer ("do you want
the full list?", a city, an age) is over the moment it is said -- waiting only
adds latency, so the agent should barge in almost immediately. A dictated
email, a card/Aadhaar/PAN/phone number or a spelled-out name is the opposite:
people pause between groups of digits, and cutting in mid-dictation drops half
of it, so the agent has to wait much longer.

The LLM knows which kind of answer it is asking for, so it declares it with a
<<ANS:SHORT|NORMAL|LONG>> marker at the start of the reply (see
system_prompt_hi.py); speech_normalizer.py strips the marker before TTS and
reports it here. The wait applies to the caller's NEXT turn. A reply with no
marker (no question asked) is treated as NORMAL.

Why NORMAL is only 0.2s: pipecat's default wait is 0.6s, which is pure added
latency on every turn. Sarvam's streaming STT returns only final per-segment
transcripts, and only after its own VAD has already seen a pause, so the
transcript of a finished segment is the earliest the LLM can see anything.
If the caller keeps talking (or corrects themselves mid-sentence), VAD starts a
new turn, pipecat interrupts the in-flight LLM/TTS output, and the later
segment is appended to the context -- the system prompt tells the model to
answer the LATEST intent. Raise NORMAL if natural mid-sentence pauses trigger
too many throwaway LLM calls (each one re-sends the full prompt).
"""
from __future__ import annotations

import os

from loguru import logger
from pipecat.frames.frames import Frame, STTMetadataFrame
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)

SHORT = "short"
NORMAL = "normal"
LONG = "long"

# (env var, default seconds) -- read at call time, not import time, because
# bot.py loads .env after its imports.
_WAIT_SETTINGS = {
    SHORT: ("TURN_WAIT_SHORT_SECS", 0.05),
    NORMAL: ("USER_SPEECH_TIMEOUT_SECS", 0.2),
    LONG: ("TURN_WAIT_LONG_SECS", 2.0),
}


def wait_secs(kind: str) -> float:
    """Seconds to keep waiting for the caller to resume, once their latest
    speech segment has been transcribed."""
    env_name, default = _WAIT_SETTINGS.get(kind, _WAIT_SETTINGS[NORMAL])
    return float(os.getenv(env_name) or default)


class AdaptiveSpeechTimeoutUserTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """SpeechTimeoutUserTurnStopStrategy whose user_speech_timeout can be
    changed between turns. The parent reads the value each time it arms its
    timer, so updating it takes effect from the caller's next pause."""

    def __init__(self, *, session_id: str = "", **kwargs):
        super().__init__(user_speech_timeout=wait_secs(NORMAL), **kwargs)
        self._session_id = session_id
        self._expected_answer = NORMAL

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        result = await super().process_frame(frame)
        if isinstance(frame, STTMetadataFrame):
            # The parent also waits out an "STT latency safety net" (the STT's
            # p99 time-to-final-transcript, minus the VAD stop window) after
            # every pause, unless the transcript is marked finalized. Sarvam's
            # transcripts are never marked finalized, so that timer ran on every
            # turn: ~0.97s (1.17s p99 - 0.2s), longer than the user_speech_timeout
            # this class adapts -- the adaptive wait never took effect, and each
            # reply paid ~1s of dead air. It guards nothing here: a turn cannot end
            # before a transcript has arrived (wait_for_transcript=True), Sarvam
            # returns one transcript per flushed segment, and speech that arrives
            # after a turn has ended simply starts a new turn (interrupting the
            # in-flight reply).
            self._stt_timeout = 0.0
        return result

    @property
    def expected_answer(self) -> str:
        return self._expected_answer

    def set_expected_answer(self, kind: str) -> None:
        kind = kind if kind in _WAIT_SETTINGS else NORMAL
        if kind != self._expected_answer:
            logger.info(
                f"session_id={self._session_id}: expecting a {kind.upper()} answer -- "
                f"turn-end wait {wait_secs(kind)}s"
            )
        self._expected_answer = kind
        self._user_speech_timeout = wait_secs(kind)
