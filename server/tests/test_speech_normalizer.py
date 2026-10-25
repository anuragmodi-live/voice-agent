import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection

from speech_normalizer import HindiSpeechNormalizer


class _Capture(HindiSpeechNormalizer):
    """Runs the real process_frame logic with the pipeline plumbing stubbed out."""

    def __init__(self, on_expected_answer=None):
        super().__init__("test", on_expected_answer=on_expected_answer)
        self.out: list = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.out.append(frame)


async def _feed(chunks: list[str], expected: list | None = None) -> str:
    proc = _Capture(on_expected_answer=expected.append if expected is not None else None)
    # FrameProcessor.process_frame's own bookkeeping needs a started pipeline;
    # this test only exercises the normalizer's own logic, so skip the base call.
    from pipecat.processors.frame_processor import FrameProcessor

    async def noop(self, frame, direction):
        return None

    FrameProcessor.process_frame, original = noop, FrameProcessor.process_frame
    try:
        for c in chunks:
            await proc.process_frame(LLMTextFrame(text=c), FrameDirection.DOWNSTREAM)
        await proc.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    finally:
        FrameProcessor.process_frame = original
    return "".join(f.text for f in proc.out if isinstance(f, LLMTextFrame))


def test_number_split_across_token_chunks():
    spoken = asyncio.run(_feed(["इस महीने ", "1", "7", " लाख", " रुपये", " की", " बिक्री", " रही।"]))
    assert spoken == "इस महीने सत्रह लाख रुपये की बिक्री रही।"


def test_decimal_and_percent_split_across_chunks():
    spoken = asyncio.run(_feed(["मार्जिन ", "5", ".", "5", "% रहा"]))
    assert spoken == "मार्जिन पाँच दशमलव पाँच प्रतिशत रहा"


def test_trailing_number_flushed_at_end_of_response():
    spoken = asyncio.run(_feed(["कुल ", "42"]))
    assert spoken == "कुल बयालीस"


def test_marker_is_stripped_even_when_split_across_chunks():
    seen: list = []
    spoken = asyncio.run(_feed(["<<", "AN", "S:SH", "ORT>> क्या ", "आप ", "पूरी ", "लिस्ट ", "सुनेंगे?"], seen))
    assert spoken == "क्या आप पूरी लिस्ट सुनेंगे?"
    assert seen == ["short"]


def test_long_marker_and_whitespace_around_it():
    seen: list = []
    spoken = asyncio.run(_feed([" <<ANS:LONG>>", " कृपया अपना ", "ईमेल बताइए।"], seen))
    assert spoken.strip() == "कृपया अपना ईमेल बताइए।"
    assert seen == ["long"]


def test_reply_without_marker_reports_normal_and_is_untouched():
    seen: list = []
    spoken = asyncio.run(_feed(["इस महीने ", "सत्रह लाख", " रुपये।"], seen))
    assert spoken == "इस महीने सत्रह लाख रुपये।"
    assert seen == ["normal"]


def test_text_starting_with_angle_bracket_is_not_swallowed():
    seen: list = []
    spoken = asyncio.run(_feed(["<<नमस्ते>> जी"], seen))
    assert spoken == "<<नमस्ते>> जी"
    assert seen == ["normal"]


def test_unfinished_marker_at_end_of_response_is_released_not_lost():
    seen: list = []
    spoken = asyncio.run(_feed(["<<AN"], seen))
    assert spoken == "<<AN"
    assert seen == ["normal"]


def test_missing_marker_falls_back_to_inferring_from_the_closing_question():
    seen: list = []
    asyncio.run(_feed(["ये टॉप ", "पाँच हैं। ", "क्या आप ", "पूरी लिस्ट ", "सुनना चाहेंगे?"], seen))
    assert seen == ["short"]


def test_explicit_marker_beats_inference():
    seen: list = []
    asyncio.run(_feed(["<<ANS:LONG>> क्या आप पूरी लिस्ट सुनना चाहेंगे?"], seen))
    assert seen == ["long"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
