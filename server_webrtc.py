"""
server_webrtc.py
----------------
Talk to Rohit from a web browser (laptop or phone) via WebRTC. No Twilio.

How it works:
  * Pipecat's built-in runner spins up a FastAPI app on port 7860 with a
    prebuilt browser UI ("Connect" button -> WebRTC -> the Rohit pipeline).
  * One pipeline runs per browser session.
  * Same STT (Deepgram) / LLM (Groq) / TTS (Cartesia) as test_local.py.

Run it:
    python server_webrtc.py -t webrtc --host 0.0.0.0 --port 7860

Then expose it to your phone with an HTTPS tunnel (mobile browsers require
HTTPS for mic access). Example with ngrok:
    ngrok http 7860

Open the https://...ngrok... URL on your phone, click Connect, and talk.
"""

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
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.groq.llm import GroqLLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import (
    IceServer,
    SmallWebRTCConnection,
)

from prompts import INSTITUTE_FACTS, SYSTEM_PROMPT

load_dotenv()


# ---------------------------------------------------------------------------
# ICE / TURN configuration
# ---------------------------------------------------------------------------
# When hosted on a provider that blocks inbound UDP (Render free tier,
# many corporate networks), the browser cannot reach the server directly
# over UDP. A TURN relay sits on the public internet over TCP/TLS, and
# both endpoints talk to it. We default to OpenRelay's free public TURN
# (https://www.metered.ca/tools/openrelay/), override via env vars if you
# want your own (twilio / xirsys / coturn behind a VPS).
def _ice_servers() -> list[IceServer]:
    turn_urls = os.environ.get(
        "TURN_URLS",
        "turn:openrelay.metered.ca:80?transport=tcp,"
        "turn:openrelay.metered.ca:443?transport=tcp,"
        "turns:openrelay.metered.ca:443",
    ).split(",")
    return [
        IceServer(urls="stun:stun.l.google.com:19302"),
        IceServer(
            urls=[u.strip() for u in turn_urls if u.strip()],
            username=os.environ.get("TURN_USERNAME", "openrelayproject"),
            credential=os.environ.get("TURN_CREDENTIAL", "openrelayproject"),
        ),
    ]


_orig_swrtc_init = SmallWebRTCConnection.__init__


def _patched_swrtc_init(self, *args, ice_servers=None, **kwargs):
    """Inject TURN servers when Pipecat's runner doesn't pass any."""
    if not ice_servers:
        ice_servers = _ice_servers()
    _orig_swrtc_init(self, *args, ice_servers=ice_servers, **kwargs)


SmallWebRTCConnection.__init__ = _patched_swrtc_init


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


async def bot(runner_args: RunnerArguments):
    """Pipecat runner entry point — called per browser connection."""
    # Browser callers don't carry name/course. Use generic defaults; the
    # script itself asks the caller for these details conversationally.
    candidate_name = os.environ.get("DEFAULT_CANDIDATE_NAME", "")
    course = os.environ.get("DEFAULT_COURSE", "BBA")

    logger.info(
        f"Browser session opened | candidate={candidate_name!r} course={course!r}"
    )

    transport = await create_transport(
        runner_args,
        {
            "webrtc": lambda: TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
            ),
        },
    )

    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.3))
    )

    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(
            model="nova-3",
            language="multi",
            smart_format=True,
            # Deepgram's own smart endpointer fires when it's confident the
            # speaker is done — much more accurate than a fixed VAD timer for
            # Hinglish code-switching. 300ms keeps turns snappy.
            endpointing=300,
            # And a 1s utterance-end as a backstop so long mid-sentence
            # pauses don't get chopped into multiple turns.
            utterance_end_ms=1000,
            keyterm=[
                "IITM Janak Puri", "Janak Puri", "Janakpuri",
                "BBA", "BCA", "MBA", "MCA", "BJMC", "BAJMC",
                "CET", "CUET", "CAT", "CMAT", "NIMCET",
                "IPU", "CGPA", "twelfth", "admission", "fees",
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
        # sonic-2: Cartesia's higher-quality conversational model.
        model="sonic-2",
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
        logger.info(f"transfer (browser session): {reason}")
        await params.result_callback({
            "status": "would_transfer",
            "note": "Browser test mode — tell the caller a human will follow up.",
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

    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True))
    task_holder["task"] = task

    @task.event_handler("on_pipeline_started")
    async def _greet(_task, _frame):
        messages.append({
            "role": "system",
            "content": "The call just connected. Greet the caller now, "
                       "following the GREETING step.",
        })
        await task.queue_frame(LLMContextFrame(context=context))

    await PipelineRunner(handle_sigint=False).run(task)
    logger.info("Browser session finished.")


if __name__ == "__main__":
    for key in ("GROQ_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY"):
        if not os.environ.get(key):
            raise SystemExit(f"Missing {key} — fill it in .env first.")
    if not os.environ.get("CARTESIA_VOICE_ID"):
        raise SystemExit("Missing CARTESIA_VOICE_ID in .env.")

    from pipecat.runner.run import main
    main()
