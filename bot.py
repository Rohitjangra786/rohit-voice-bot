"""
bot.py
------
The Rohit voice agent: a streaming STT -> LLM -> TTS pipeline, plus two
real actions Rohit can take — end the call, and transfer to a human
admission in-charge.

Latency strategy (this is what makes Rohit feel human):
  * Every stage streams — STT emits partials, the LLM streams tokens, and
    TTS starts speaking before the full sentence is generated.
  * Silero VAD detects end-of-speech so Rohit knows when the caller stopped.
  * Interruptions are allowed — if the caller talks, Rohit stops instantly.
  * The prompt forces SHORT replies, so there is little audio to synthesize.

Target voice-to-voice latency with this stack: ~400-700ms.
(1ms is not physically possible — see the README.)

API churn note: Pipecat's import paths and the function-calling schema
change between releases. This file targets pipecat-ai ~0.0.60+. If an
import fails, check your installed version against https://docs.pipecat.ai
"""

import os

from dotenv import load_dotenv
from loguru import logger
from twilio.rest import Client as TwilioClient

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from prompts import INSTITUTE_FACTS, SYSTEM_PROMPT

load_dotenv()


def build_system_prompt(candidate_name: str, course: str) -> str:
    """Fill the call-specific placeholders into the RLTV script."""
    return SYSTEM_PROMPT.format(
        candidate_name=candidate_name or "the candidate",
        course=course or "BBA",
        institute_facts=INSTITUTE_FACTS,
    )


# --- Tools Rohit can call -------------------------------------------------
# These are exposed to the LLM. The prompt tells Rohit when to use them.
END_CALL = FunctionSchema(
    name="end_call",
    description="End the phone call after saying goodbye to the caller.",
    properties={
        "reason": {
            "type": "string",
            "description": "Short reason, e.g. 'not interested' or 'finished'.",
        }
    },
    required=["reason"],
)

TRANSFER_CALL = FunctionSchema(
    name="transfer_to_counsellor",
    description=(
        "Transfer the live call to a human admission in-charge. Use when the "
        "caller wants detailed counselling, asks for a human, or wants to "
        "proceed with admission."
    ),
    properties={
        "reason": {
            "type": "string",
            "description": "Why the caller is being transferred.",
        }
    },
    required=["reason"],
)


async def run_bot(websocket_client, stream_sid: str, call_sid: str,
                  candidate_name: str = "", course: str = "BBA"):
    """
    Wire up and run the Rohit pipeline for a single phone call.

    Called once per call by server.py when Twilio opens its media websocket.
    """
    logger.info(f"Starting Rohit | call={call_sid} "
                f"candidate={candidate_name!r} course={course!r}")

    # --- Transport: audio in/out over the Twilio media-stream websocket ----
    transport = FastAPIWebsocketTransport(
        websocket=websocket_client,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            # Silero VAD = "did the caller stop talking?" detection.
            # 0.55s gives Delhi Hinglish callers room for mid-sentence
            # code-switch pauses ("matlab... fees kitni hai?") without Rohit
            # cutting them off. Drop to ~0.4s for pure English callers.
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.55)),
            serializer=TwilioFrameSerializer(stream_sid),
        ),
    )

    # --- Speech to text: Deepgram (streaming, tuned for Delhi callers) -----
    # nova-3 + language="multi" handles Hindi-English code-switching the way
    # Delhi callers actually speak ("haan ji, twelfth complete kar liya").
    # keyterm boosts recognition of institute-specific vocabulary that
    # nova-3 otherwise mistranscribes (e.g. "I-I-T-M" -> "item").
    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        live_options={
            "model": "nova-3",
            "language": "multi",
            "smart_format": True,
            "keyterm": [
                "IITM Janakpuri", "Janakpuri", "BBA", "BCA", "MBA", "MCA",
                "BJMC", "BAJMC", "CET", "CUET", "CAT", "CMAT", "NIMCET",
                "IPU", "CGPA", "twelfth",
            ],
        },
    )

    # --- The brain: Claude. Swap to OpenAI/Groq here if you prefer. --------
    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model="claude-haiku-4-5-20251001",  # fast model = lower latency
    )

    # --- Text to speech: Cartesia (low-latency, natural, streams) ----------
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        # Pick a warm male Indian-English voice in the Cartesia dashboard
        # and paste its voice ID here, or override via .env.
        voice_id=os.environ.get("CARTESIA_VOICE_ID", "REPLACE_WITH_VOICE_ID"),
    )

    # --- Conversation memory + tools --------------------------------------
    messages = [{
        "role": "system",
        "content": build_system_prompt(candidate_name, course),
    }]
    tools = ToolsSchema(standard_tools=[END_CALL, TRANSFER_CALL])
    context = LLMContext(messages, tools)
    context_aggregator = llm.create_context_aggregator(context)

    task_holder = {}  # lets the function handlers reach the running task

    # --- Function handler: end the call gracefully ------------------------
    async def handle_end_call(params):
        logger.info(f"end_call requested: {params.arguments.get('reason')}")
        await params.result_callback({"status": "ending"})
        # EndFrame flushes any audio still being spoken, then closes cleanly.
        if "task" in task_holder:
            await task_holder["task"].queue_frame(EndFrame())

    # --- Function handler: transfer to a human ----------------------------
    async def handle_transfer(params):
        reason = params.arguments.get("reason")
        logger.info(f"transfer requested: {reason}")
        human_number = os.environ.get("ADMISSION_INCHARGE_NUMBER")
        try:
            if human_number and call_sid:
                client = TwilioClient(
                    os.environ["TWILIO_ACCOUNT_SID"],
                    os.environ["TWILIO_AUTH_TOKEN"],
                )
                # Redirect the LIVE call to TwiML that dials the human.
                client.calls(call_sid).update(
                    twiml=f"<Response><Say>Connecting you now.</Say>"
                          f"<Dial>{human_number}</Dial></Response>"
                )
                await params.result_callback({"status": "transferring"})
            else:
                await params.result_callback({
                    "status": "no_number_configured",
                    "note": "Tell the caller someone will call them back.",
                })
        except Exception as e:  # noqa: BLE001
            logger.error(f"Transfer failed: {e}")
            await params.result_callback({"status": "transfer_failed"})

    llm.register_function("end_call", handle_end_call)
    llm.register_function("transfer_to_counsellor", handle_transfer)

    # --- The pipeline: audio in -> STT -> LLM -> TTS -> audio out ----------
    pipeline = Pipeline([
        transport.input(),
        stt,
        context_aggregator.user(),
        llm,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,      # caller can talk over Rohit
            enable_metrics=True,           # logs per-stage latency
        ),
    )
    task_holder["task"] = task

    # When the call connects, make Rohit speak first (it's an outbound call).
    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        messages.append({
            "role": "system",
            "content": "The call just connected. Greet the caller now, "
                       "following the GREETING step.",
        })
        await task.queue_frames(
            [context_aggregator.user().get_context_frame()]
        )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        logger.info(f"Caller hung up | call={call_sid}")
        await task.cancel()

    await PipelineRunner(handle_sigint=False).run(task)
    logger.info(f"Rohit finished | call={call_sid}")
