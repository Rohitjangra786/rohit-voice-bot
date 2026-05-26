"""
test_local.py
-------------
Talk to Rohit through your laptop mic + speakers — no Twilio, no phone.

Same STT -> LLM -> TTS pipeline as bot.py, but the audio in/out is the
local audio device (PyAudio) instead of a Twilio media-stream websocket.

Requires the same API keys as the phone bot, MINUS Twilio:
  - ANTHROPIC_API_KEY
  - DEEPGRAM_API_KEY
  - CARTESIA_API_KEY
  - CARTESIA_VOICE_ID

Run it:
    python test_local.py --name "Priya" --course MBA

Stop with Ctrl+C. The bot will start speaking first (the greeting), then
listen for your reply. Speak naturally — interruptions are allowed.
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame, LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from prompts import INSTITUTE_FACTS, SYSTEM_PROMPT

load_dotenv()


END_CALL = FunctionSchema(
    name="end_call",
    description="End the conversation after saying goodbye.",
    properties={"reason": {"type": "string", "description": "Short reason."}},
    required=["reason"],
)

TRANSFER_CALL = FunctionSchema(
    name="transfer_to_counsellor",
    description="Hand the conversation over to a human admission in-charge.",
    properties={"reason": {"type": "string", "description": "Why transferring."}},
    required=["reason"],
)


def build_system_prompt(candidate_name: str, course: str) -> str:
    return SYSTEM_PROMPT.format(
        candidate_name=candidate_name or "the candidate",
        course=course or "BBA",
        institute_facts=INSTITUTE_FACTS,
    )


async def run_local(candidate_name: str, course: str):
    logger.info(
        f"Local test | candidate={candidate_name!r} course={course!r}. "
        "Speak into your mic. Ctrl+C to stop."
    )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        )
    )

    # VAD is now a pipeline stage (Pipecat 1.x), not a transport param.
    # 0.55s stop_secs gives Delhi Hinglish callers room for code-switch pauses.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.55))
    )

    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(
            model="nova-3",
            language="multi",
            smart_format=True,
            keyterm=[
                "IITM Janakpuri", "Janakpuri", "BBA", "BCA", "MBA", "MCA",
                "BJMC", "BAJMC", "CET", "CUET", "CAT", "CMAT", "NIMCET",
                "IPU", "CGPA", "twelfth",
            ],
        ),
    )

    llm = GroqLLMService(
        api_key=os.environ["GROQ_API_KEY"],
        model=os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        voice_id=os.environ.get("CARTESIA_VOICE_ID") or "",
    )

    messages = [{
        "role": "system",
        "content": build_system_prompt(candidate_name, course),
    }]
    tools = ToolsSchema(standard_tools=[END_CALL, TRANSFER_CALL])
    context = LLMContext(messages, tools)
    aggregators = LLMContextAggregatorPair(context)

    task_holder: dict = {}

    async def handle_end_call(params):
        logger.info(f"end_call: {params.arguments.get('reason')}")
        await params.result_callback({"status": "ending"})
        if "task" in task_holder:
            await task_holder["task"].queue_frame(EndFrame())

    async def handle_transfer(params):
        reason = params.arguments.get("reason")
        logger.info(f"transfer (local-test, no Twilio): {reason}")
        await params.result_callback({
            "status": "would_transfer",
            "note": "Local test mode — tell the caller a human will follow up.",
        })

    llm.register_function("end_call", handle_end_call)
    llm.register_function("transfer_to_counsellor", handle_transfer)

    pipeline = Pipeline([
        transport.input(),
        vad,
        stt,
        aggregators.user(),
        llm,
        tts,
        transport.output(),
        aggregators.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True),
    )
    task_holder["task"] = task

    @task.event_handler("on_pipeline_started")
    async def _greet(_task, _frame):
        # Nudge the LLM to speak first by appending a system note and
        # queueing a context frame — same pattern as bot.py's
        # on_client_connected, adapted to the 1.x context-frame API.
        messages.append({
            "role": "system",
            "content": "The call just connected. Greet the caller now, "
                       "following the GREETING step.",
        })
        await task.queue_frame(LLMContextFrame(context=context))

    await PipelineRunner(handle_sigint=True).run(task)
    logger.info("Local test finished.")


def main():
    parser = argparse.ArgumentParser(
        description="Talk to Rohit locally through your mic + speakers."
    )
    parser.add_argument("--name", default="", help="Candidate name")
    parser.add_argument(
        "--course", default="BBA",
        help="Course of interest: BBA / BCA / MBA / MCA / BJMC",
    )
    args = parser.parse_args()

    for key in ("GROQ_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY"):
        if not os.environ.get(key):
            raise SystemExit(f"Missing {key} — fill it in .env first.")
    if not os.environ.get("CARTESIA_VOICE_ID"):
        raise SystemExit(
            "Missing CARTESIA_VOICE_ID — pick a voice at "
            "https://play.cartesia.ai/voices and paste its ID into .env."
        )

    asyncio.run(run_local(args.name, args.course))


if __name__ == "__main__":
    main()
