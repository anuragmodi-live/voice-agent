#
# Copyright (c) 2024-2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""biz-performance-voice-agent - Pipecat Voice Agent (Phase 2)

Cascade pipeline: Speech-to-Text -> LLM -> Text-to-Speech. Same shape as
kyc-voice-agent's bot.py -- Sarvam STT/TTS in Hindi/Hinglish, llm_factory.py's
provider switch, SmallWebRTC transport -- with the ONE
ask_calculation_engine tool wired in instead of KYC's document tools, and
VoiceTurnObserver in place of InstrumentationObserver (transcript capture +
grounding/judge triggering instead of TTFB/TTFA/interruption metrics).

Run via server.py's /api/offer route (uvicorn server:app), not directly.
"""

import os
import time

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame, LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments, WebSocketRunnerArguments
from pipecat.runner.utils import create_transport, parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

import pipeline_registry
import telephony
from adaptive_turn import AdaptiveSpeechTimeoutUserTurnStopStrategy
from llm_factory import build_llm
from prompts.greeting_hi import GREETING_TEXT
from prompts.system_prompt_hi import SYSTEM_PROMPT
from session_store import get_session, update_session
from speech_normalizer import HindiSpeechNormalizer
from tools.ask_calculation_engine import build_pipecat_tool
from voice_observer import VoiceTurnObserver

load_dotenv(override=True)

# Same generous default as kyc-voice-agent -- a voice call has real thinking
# pauses; too jumpy a timeout interrupts normal silence, not just genuine
# audio problems.
USER_IDLE_TIMEOUT_SECS = float(os.getenv("USER_IDLE_TIMEOUT_SECS") or 12.0)
MAX_CONSECUTIVE_IDLE_CHECKS = 2

# How long to wait for the caller to resume before handing their transcribed
# speech to the LLM is adaptive -- see adaptive_turn.py (short for yes/no-style
# answers, long for dictated digits/names, tunable via env vars).

# How recent a /session/{id}/typing ping has to be for silence to still
# count as "user is composing a question in the text box", not "call went
# quiet". Comfortably above the client's ~3s ping throttle so a couple of
# missed/delayed pings don't false-trigger a check-in mid-sentence.
TYPING_GRACE_SECS = 8.0

NO_AUDIO_CLOSING_LINE = (
    "Mujhe lagta hai aapki awaaz mujhtak nahi pahunch rahi. Kripya apna microphone "
    "check karke thodi der baad dobara call karein. Dhanyawad."
)

# Pipecat/Silero's defaults (confidence=0.7, start_secs=0.2, min_volume=0.6)
# only need 200ms of sufficiently loud, speech-shaped audio to decide "the
# user started talking" -- a sharp, high-pitched background sound (a bird
# chirp, a door, a notification) can have spectral characteristics close
# enough to human speech to cross that bar, wrongly barging in on the bot
# mid-sentence. Raised here to require both a higher-confidence match and a
# louder signal, trading a little responsiveness to genuinely soft speech
# for real resistance to short, sharp non-speech noise.
VAD_PARAMS = VADParams(confidence=0.85, min_volume=0.7)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info("Starting bot")

    # Speech-to-Text -- `os.getenv(key, default)` only falls back to `default`
    # when the key is ABSENT, not present-but-empty (as with SARVAM_MODEL=
    # and nothing after the `=`) -- `or` catches that too.
    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY"),
        settings=SarvamSTTService.Settings(
            model=os.getenv("SARVAM_MODEL") or "saaras:v3",
            language=Language.HI_IN,
        ),
    )

    # Text-to-Speech. "ritu" on bulbul:v3, chosen from a side-by-side listening
    # test of the female voices (priya was the earlier default, inherited from
    # kyc-voice-agent). Pace 0.85, slightly under the natural-speed default,
    # reads as calmer/friendlier than 1.0, not sluggish. enable_preprocessing
    # normalizes numbers/currency before synthesis.
    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        settings=SarvamTTSService.Settings(
            model=os.getenv("SARVAM_TTS_MODEL") or "bulbul:v3",
            voice=os.getenv("SARVAM_VOICE_ID") or "ritu",
            pace=float(os.getenv("SARVAM_TTS_PACE") or 0.85),
            enable_preprocessing=True,
            language=Language.HI_IN,
        ),
    )

    llm = build_llm()

    # A phone call (Twilio Media Streams over a websocket, see telephony.py and
    # server.py's /twilio/ws) differs from the web client only at the edges:
    # no RTVI "client ready" signal (so the greeting is triggered by the
    # transport connecting instead), no typing pings, no reconnect/resume.
    telephony_call = isinstance(runner_args, WebSocketRunnerArguments)
    call_data = getattr(runner_args, "call_data", None)
    session_id = (
        runner_args.session_id
        or (call_data.call_id if (telephony_call and call_data and call_data.call_id) else None)
        or "dev-session"
    )
    ask_calculation_engine = build_pipecat_tool(session_id)

    # A reconnect within RESUME_WINDOW_SECS of a disconnect (see server.py's
    # /start-session) reuses the same session_id and has this session's
    # saved_llm_messages populated (set in on_client_disconnected below) --
    # restore that conversation instead of starting over with just the
    # system prompt, so a brief drop doesn't erase what was already said.
    resumed_messages = get_session(session_id).saved_llm_messages
    is_resume = bool(resumed_messages)

    context = LLMContext(
        messages=resumed_messages if is_resume else [{"role": "system", "content": SYSTEM_PROMPT}],
        tools=ToolsSchema(standard_tools=[ask_calculation_engine]),
    )
    # Turn-end wait that follows what the agent last asked: updated by
    # HindiSpeechNormalizer below from the model's <<ANS:...>> marker.
    turn_stop_strategy = AdaptiveSpeechTimeoutUserTurnStopStrategy(session_id=session_id)

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VAD_PARAMS),
            user_idle_timeout=USER_IDLE_TIMEOUT_SECS,
            # Pipecat's default end-of-turn detector is a local ONNX model
            # (LocalSmartTurnAnalyzerV3) run in-process on every frame -- real
            # CPU competition against STT/LLM/TTS/audio-codec work on Render's
            # free tier, which was causing audio to stutter roughly every few
            # seconds. Silero VAD (already configured above) is lightweight
            # and this swaps end-of-turn detection to a plain VAD-plus-timer
            # strategy instead of a second local model.
            user_turn_strategies=UserTurnStrategies(stop=[turn_stop_strategy]),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            # Enforces the Devanagari-only rule on whatever the LLM actually
            # streamed (digits -> number words, loanwords -> Devanagari,
            # Latin leaks logged) before it reaches TTS -- see hindi_text.py.
            HindiSpeechNormalizer(session_id, on_expected_answer=turn_stop_strategy.set_expected_answer),
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    voice_turn_observer = VoiceTurnObserver(session_id)
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True, **_audio_rates(telephony_call)),
        observers=[voice_turn_observer],
    )

    pipeline_registry.register_task(session_id, worker, context)

    # Same idempotency guard as kyc-voice-agent: RTVIProcessor's on_client_ready
    # has no guard of its own and can genuinely fire more than once for one
    # connection (e.g. a brief WebRTC/data-channel retry) -- without this,
    # each extra firing re-queues the greeting.
    conversation_started = False

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        nonlocal conversation_started
        if conversation_started:
            logger.info(f"session_id={session_id}: on_client_ready fired again -- ignoring")
            return
        conversation_started = True

        if is_resume:
            # Conversation history is already restored into `context` --
            # don't re-greet as if this were a brand new call, just become
            # ready to listen again.
            logger.info(
                f"session_id={session_id}: resumed session, {len(resumed_messages)} prior "
                f"message(s) restored -- skipping greeting"
            )
            return

        # A fixed line straight to TTS -- no LLM round trip (see
        # prompts/greeting_hi.py). TTSSpeakFrame text is appended to the
        # context as an assistant message, so the model knows it already
        # greeted and asked its opening question.
        await worker.queue_frames([TTSSpeakFrame(GREETING_TEXT)])

    idle_check_count = 0

    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy=None):
        nonlocal idle_check_count
        idle_check_count = 0
        # The user only starts a new turn once the bot's ENTIRE reply to the
        # previous one is done, however many speak/stop cycles that reply
        # took (filler + tool call + real answer, etc.) -- see
        # voice_observer.py's docstring for why this is the right boundary
        # and BotStoppedSpeakingFrame alone is not.
        voice_turn_observer.flush()

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        nonlocal idle_check_count

        session = get_session(session_id)
        typing_at = session.last_typing_at
        if typing_at is not None and (time.time() - typing_at) < TYPING_GRACE_SECS:
            # User is composing a question in the text box, not silent --
            # don't burn the idle-check budget or talk over them. Reset so
            # they get the full budget once they actually go quiet.
            idle_check_count = 0
            logger.debug(f"session_id={session_id}: idle timeout fired but user is typing -- skipping check-in")
            return

        if session.pending_log_ids:
            # A tool call for this turn has already started (tracked in our
            # own session state by ask_calculation_engine.py's _track(),
            # cleared only once grounding.finalize_turn() runs after the
            # bot's ENTIRE reply is narrated) but hasn't finished yet --
            # confirmed happening live: the bot said its filler line ("let
            # me calculate"), went quiet while the tool call was still
            # running, and the idle timer fired anyway mid-calculation.
            # Pipecat's own UserIdleController is supposed to suppress this
            # via FunctionCallsStartedFrame (see user_idle_controller.py),
            # but its own source comments document a real race window where
            # BotStoppedSpeakingFrame (the filler line ending) can arrive
            # before that frame does. This is an independent second line of
            # defense using our own server-side state instead of relying
            # solely on Pipecat's internal frame ordering.
            idle_check_count = 0
            logger.debug(
                f"session_id={session_id}: idle timeout fired but a tool call is still in flight "
                f"(pending_log_ids={session.pending_log_ids}) -- skipping check-in"
            )
            return

        idle_check_count += 1
        logger.info(
            f"session_id={session_id}: user idle for {USER_IDLE_TIMEOUT_SECS:.0f}s "
            f"(check-in {idle_check_count}/{MAX_CONSECUTIVE_IDLE_CHECKS})"
        )

        if idle_check_count > MAX_CONSECUTIVE_IDLE_CHECKS:
            await worker.queue_frames(
                [TTSSpeakFrame(NO_AUDIO_CLOSING_LINE), EndFrame(reason="user_idle_no_response")]
            )
            return

        context.add_message(
            {
                "role": "developer",
                "content": (
                    "The user hasn't said anything since your last turn. Check in "
                    "with them in ONE short, warm line -- ask if they're still there "
                    "or if there's a mic problem. Don't repeat your introduction, "
                    "just this one check-in."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        update_session(session_id, connected_at=time.time(), disconnected_at=None)
        if telephony_call:
            # No RTVI client on a phone line, so greet as soon as the call's
            # media stream is up (the web path does this from on_client_ready).
            await worker.queue_frames([TTSSpeakFrame(GREETING_TEXT)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        # Flush any trailing bot speech that never got a following user turn
        # to trigger the flush above (e.g. the call ended right after the
        # bot's answer, or mid-reply). force=True: the call is over, so
        # capture whatever's buffered even if a tool call never resolved --
        # there's no more chance to wait for the real answer.
        voice_turn_observer.flush(force=True)
        # Snapshot the conversation so far -- a reconnect within
        # RESUME_WINDOW_SECS restores it (see the top of run_bot()) instead
        # of starting over. list(...) copies it now, since `context` itself
        # is about to be torn down along with the rest of this pipeline.
        update_session(session_id, disconnected_at=time.time(), saved_llm_messages=list(context.messages))
        pipeline_registry.unregister_task(session_id)
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


def _audio_rates(telephony_call: bool) -> dict:
    """Pipeline sample rates for a phone call. Twilio's line is always 8 kHz
    mu-law; pipecat's TwilioFrameSerializer resamples between that and the
    pipeline rate, so the rate here only decides what STT/TTS run at.
    TELEPHONY_SAMPLE_RATE=16000 feeds STT upsampled 16 kHz audio (some speech
    models cope better with it than with 8 kHz) -- no new detail is added, the
    line is still narrowband. Web calls keep pipecat's defaults."""
    if not telephony_call:
        return {}
    rate = int(os.getenv("TELEPHONY_SAMPLE_RATE") or 8000)
    return {"audio_in_sample_rate": rate, "audio_out_sample_rate": rate}


async def _telephony_transport(runner_args: WebSocketRunnerArguments) -> BaseTransport:
    """Transport for a phone call's Media Streams websocket (Twilio or
    SignalWire). Built here rather than by pipecat's create_transport because
    that helper hard-wires Twilio's own REST host for the serializer's
    auto hang-up; SignalWire needs its space URL (telephony.serializer_base_url).
    SignalWire's stream handshake is the same shape as Twilio's, so
    pipecat detects both as "twilio"."""
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    runner_args.transport_type = transport_type
    runner_args.call_data = call_data
    if transport_type != "twilio":
        raise ValueError(f"unsupported telephony stream handshake: {transport_type!r}")
    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
        account_sid=telephony.account_sid(),
        auth_token=telephony.api_token(),
        base_url=telephony.serializer_base_url(),
    )
    params = FastAPIWebsocketParams(
        audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer,
    )
    return FastAPIWebsocketTransport(websocket=runner_args.websocket, params=params)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point: server.py's /api/offer (WebRTC) and /twilio/ws
    (phone calls via Twilio or SignalWire Media Streams) both land here."""
    transport_params = {
        "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    try:
        if isinstance(runner_args, WebSocketRunnerArguments):
            transport = await _telephony_transport(runner_args)
        else:
            transport = await create_transport(runner_args, transport_params)
    except Exception as e:  # noqa: BLE001
        if not isinstance(runner_args, WebSocketRunnerArguments):
            raise
        # e.g. the provider's account id / API token missing (the serializer
        # needs them to hang up the call), or an unrecognised handshake.
        logger.error(f"Could not set up the telephony transport: {type(e).__name__}: {e}")
        await runner_args.websocket.close(code=1011)
        return
    if isinstance(runner_args, WebSocketRunnerArguments):
        # create_transport has now read Twilio's handshake into call_data. The
        # stream must carry the token only our signed /twilio/voice webhook
        # mints for this exact call -- otherwise anyone who finds the
        # websocket URL could open a (billable) agent session.
        call_data = runner_args.call_data
        token = (call_data.body or {}).get("auth") if call_data else None
        if not telephony.stream_token_valid(call_data.call_id if call_data else None, token):
            logger.warning("Rejecting telephony stream with a missing/invalid token")
            await runner_args.websocket.close(code=4401)
            return
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
