"""HindiSpeechNormalizer: a pipecat processor that sits between the LLM and
TTS and enforces the Devanagari-only speech rule on whatever the model actually
streamed -- see hindi_text.py for what it rewrites and why.

The LLM streams token-sized LLMTextFrame chunks, so a number can arrive split
across frames ("1", "7", ".5"). Text is therefore held back to the last
whitespace boundary before it is normalized, and flushed in full when the
response ends or is interrupted. The held-back tail is at most one word, so
the extra latency is a single token.

Because the assistant context aggregator records what was *spoken* (the
TTSTextFrames downstream of this processor), the normalized Devanagari text is
also what lands in the conversation history -- every later turn then sees
Devanagari-only assistant messages, which is itself the strongest anchor
against the model drifting back to Roman script on long answers.

Latin-script words that survive normalization (entity names, product codes)
are logged per response so drift is visible rather than silent.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from hindi_text import (
    could_be_marker_prefix,
    infer_expected_answer,
    latin_leaks,
    normalize_for_speech,
    split_leading_marker,
)

# A held-back tail only needs to stay held while it could still become (part
# of) a digit string or a Latin word; a tail that is pure Devanagari/punctuation
# is safe to emit right away, which keeps sentence boundaries flowing to TTS.
_NEEDS_LOOKAHEAD_RE = re.compile(r"[A-Za-z0-9₹%.,\-]")


class HindiSpeechNormalizer(FrameProcessor):
    def __init__(
        self,
        session_id: str,
        on_expected_answer: Callable[[str], None] | None = None,
        **kwargs,
    ):
        """on_expected_answer: called at the end of every LLM response with the
        answer length the model declared via its <<ANS:...>> marker ("short" /
        "normal" / "long"; "normal" when the reply had no marker), so the turn-end
        wait can adapt to the caller's next answer -- see adaptive_turn.py."""
        super().__init__(**kwargs)
        self._session_id = session_id
        self._on_expected_answer = on_expected_answer
        self._pending = ""
        self._response_text = ""
        self._last_text_frame: LLMTextFrame | None = None
        # The marker, if any, is the very first thing in a reply, so the first
        # chunks are held back until it is either parsed or ruled out.
        self._head = ""
        self._marker_resolved = False
        self._marker_kind: str | None = None

    def _reset_response_state(self) -> None:
        self._head = ""
        self._marker_resolved = False
        self._marker_kind = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMTextFrame):
            self._last_text_frame = frame
            text = frame.text
            if not self._marker_resolved:
                self._head += text
                if could_be_marker_prefix(self._head):
                    return  # might still be the marker; hold
                self._marker_kind, text = split_leading_marker(self._head)
                self._head = ""
                self._marker_resolved = True
                if not text:
                    return
            self._pending += text
            ready, self._pending = self._split_ready(self._pending)
            if not ready:
                return  # still mid-word; wait for the next chunk
            normalized = normalize_for_speech(ready)
            self._response_text += normalized
            frame.text = normalized
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            if self._head:  # never resolved into a marker (e.g. a lone "<<A") -- it is just text
                self._pending += self._head
            await self._flush(direction)
            # The model's own marker wins; when it forgot one, read the
            # closing question off the reply itself.
            expected = self._marker_kind or infer_expected_answer(self._response_text)
            self._report_leaks()
            if self._on_expected_answer is not None:
                self._on_expected_answer(expected)
            self._reset_response_state()
        elif isinstance(frame, InterruptionFrame):
            self._pending = ""
            self._response_text = ""
            self._last_text_frame = None
            self._reset_response_state()

        await self.push_frame(frame, direction)

    @staticmethod
    def _split_ready(text: str) -> tuple[str, str]:
        """(emit-now, hold-back). Holds back the trailing partial word only
        when it could still turn into a number or a Latin word."""
        cut = max(text.rfind(" "), text.rfind("\n"))
        head, tail = text[: cut + 1], text[cut + 1:]
        if tail and not _NEEDS_LOOKAHEAD_RE.search(tail):
            return text, ""
        return head, tail

    async def _flush(self, direction: FrameDirection) -> None:
        if not self._pending:
            return
        normalized = normalize_for_speech(self._pending)
        self._pending = ""
        self._response_text += normalized
        frame = LLMTextFrame(text=normalized)
        if self._last_text_frame is not None:
            frame.skip_tts = self._last_text_frame.skip_tts
            frame.append_to_context = self._last_text_frame.append_to_context
        await self.push_frame(frame, direction)

    def _report_leaks(self) -> None:
        leaks = latin_leaks(self._response_text)
        if leaks:
            logger.warning(
                f"session_id={self._session_id}: {len(leaks)} Latin-script word(s) reached TTS "
                f"despite the Devanagari-only rule: {leaks[:12]}"
            )
        self._response_text = ""
        self._last_text_frame = None
