"""
server.py
---------
The web server that connects Twilio's phone network to Rohit.

Two endpoints:
  POST /twiml      -> Twilio hits this when a call connects. We return TwiML
                      that tells Twilio to stream the call audio to our
                      websocket.
  WS   /ws         -> Twilio streams the live call audio here. Each new
                      connection spins up one run_bot() session.

Run it:   uvicorn server:app --host 0.0.0.0 --port 8000
Expose it during testing with ngrok:   ngrok http 8000
"""

import json

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
from loguru import logger

from bot import run_bot

app = FastAPI(title="Rohit Admissions Bot")

# Stores per-call info (candidate name, course) keyed by Twilio CallSid,
# so the websocket handler can pick it up. In production use Redis/DB.
PENDING_CALLS: dict[str, dict] = {}


@app.get("/")
async def health():
    return {"status": "ok", "bot": "Rohit", "campus": "IITM Janakpuri"}


@app.post("/twiml")
async def twiml(request: Request):
    """
    Twilio calls this when a phone call connects. We reply with TwiML that
    opens a media stream to our /ws websocket.

    The PUBLIC_HOST env value (your ngrok or server domain) is read by
    make_call.py and baked into the stream URL there, so this endpoint just
    needs to return a stream pointing at the same host that called it.
    """
    host = request.url.hostname
    form = await request.form()
    call_sid = form.get("CallSid", "")
    logger.info(f"/twiml hit for call {call_sid}")

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/ws">
      <Parameter name="callSid" value="{call_sid}" />
    </Stream>
  </Connect>
</Response>"""
    return HTMLResponse(content=twiml_response, media_type="application/xml")


@app.websocket("/ws")
async def media_stream(websocket: WebSocket):
    """Twilio connects here and streams the live call audio."""
    await websocket.accept()
    logger.info("Twilio media stream connected")

    # Twilio sends two JSON messages before audio: 'connected' then 'start'.
    # The 'start' message carries the streamSid, callSid, and custom params.
    await websocket.receive_text()                       # 'connected'
    start_msg = json.loads(await websocket.receive_text())  # 'start'

    start = start_msg.get("start", {})
    stream_sid = start.get("streamSid", "")
    call_sid = start.get("callSid", "")
    custom = start.get("customParameters", {}) or {}

    # Candidate details: passed as Twilio custom parameters (see make_call.py),
    # or looked up from PENDING_CALLS by CallSid.
    info = PENDING_CALLS.pop(call_sid, {})
    candidate_name = custom.get("candidateName") or info.get("name", "")
    course = custom.get("course") or info.get("course", "BBA")

    logger.info(f"Stream start | call={call_sid} stream={stream_sid}")

    await run_bot(
        websocket_client=websocket,
        stream_sid=stream_sid,
        call_sid=call_sid,
        candidate_name=candidate_name,
        course=course,
    )
