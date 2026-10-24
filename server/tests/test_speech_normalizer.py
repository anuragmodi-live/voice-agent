import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipecat.frames.frames import LLMFullResponseEndFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection

from speech_normalizer import HindiSpeechNormalizer


class _Capture(HindiSpeechNormalizer):
    """Runs the real process_frame logic with the pipeline plumbing stubbed out."""

    def __init__(self):
        super().__init__("test")
        self.out: list = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.out.append(frame)


async def _feed(chunks: list[str]) -> str:
    proc = _Capture()
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
