# Rohit — IITM Janak Puri Admissions Voice Bot

**Author:** Rohit Jangra

Rohit is a real-time voice agent that runs the IITM Janak Puri RLTV
admissions script — greeting, eligibility check, entrance-exam check,
course information, objection handling, and transfer to a human admission
in-charge.

It's a streaming **STT → LLM → TTS** pipeline built on Pipecat, with three
ways to talk to it:

| Mode | Entry point | What you need |
|---|---|---|
| **Mic + speakers (laptop)** | `python test_local.py` | Just your computer mic and speakers |
| **Browser (mobile or laptop)** | `python server_webrtc.py -t webrtc` | A browser + a tunnel (cloudflared/ngrok) for HTTPS so mobile mic works |
| **Real phone call (Twilio)** | `uvicorn server:app` + `python make_call.py` | Twilio account + public HTTPS host |

---

## What a user needs to run this

A real demo needs **four free accounts** (no credit card required for any of them):

1. **Groq** — https://console.groq.com → API Keys → "Create API Key"
   The LLM brain. Free tier with no payment info. Llama 3.3 70B, ~300ms TTFT.
2. **Deepgram** — https://console.deepgram.com → API Keys
   Speech-to-text. New accounts get $200 free credit. Plenty for testing.
3. **Cartesia** — https://play.cartesia.ai → top-right avatar → API Keys
   Text-to-speech. Free tier covers casual testing. Also pick a voice ID
   from the Voices page (filter language = "English (India)") and copy it.
4. **(Optional) Twilio** — only if you want real outbound phone calls.
   Free trial credit on signup.

Copy `.env.example` → `.env` and paste the keys in. Then:

```bash
python -m venv venv
venv\Scripts\activate          # Windows  (or `source venv/bin/activate` on Mac/Linux)
pip install -r requirements.txt
python test_local.py           # talk to Rohit through your laptop mic
```

To test from your **phone**, run `python server_webrtc.py -t webrtc --host 0.0.0.0 --port 7860`, then start a free HTTPS tunnel in another terminal:

```bash
cloudflared tunnel --url http://localhost:7860
```

Open the printed `https://*.trycloudflare.com` URL on your phone, tap **Connect**, and talk.

---

## About latency (please read)

You asked for **1ms** latency. That isn't physically possible — the network
round-trip alone is 20–100ms, before any speech processing happens.

What you actually want is **voice-to-voice latency under ~800ms**. Human
conversation has a natural turn-taking gap of about 200–300ms; replying
*faster* than that feels like interrupting. This stack targets **~400–700ms**,
which sounds natural and human. Speed is not what makes a bot feel robotic —
awkward pauses, bad end-of-speech detection, and stiff scripted answers are.
Rohit is built to avoid all three.

---

## What's in here

| File              | Purpose                                                  |
|-------------------|----------------------------------------------------------|
| `prompts.py`      | Rohit's persona + the full RLTV call script              |
| `bot.py`          | The voice pipeline + end-call / human-transfer functions |
| `server.py`       | FastAPI server: Twilio webhook + media-stream websocket  |
| `make_call.py`    | Triggers an outbound call to a student                   |
| `requirements.txt`| Dependencies                                             |
| `.env.example`    | Template for your API keys                               |

---

## The stack

- **Telephony:** Twilio (handles the actual phone call)
- **Speech-to-text:** Deepgram (streaming, Indian-English model)
- **Brain (LLM):** Claude Haiku — a fast model keeps latency low
- **Text-to-speech:** Cartesia (low-latency, natural voice)
- **Orchestration:** Pipecat (pipeline, VAD, interruption handling)

All five are swappable. For India-based phone numbers you may prefer
**Plivo or Exotel** over Twilio (better local coverage and DLT compliance) —
the structure stays the same, only the telephony layer changes.

---

## Setup

**1. Install dependencies** (Python 3.11+ recommended)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**2. Get API keys** and fill in `.env`

```bash
cp .env.example .env
```

You need accounts at: Anthropic, Deepgram, Cartesia, and Twilio. Each
`.env.example` line has the signup URL. In Cartesia, pick a warm male
Indian-English voice and paste its voice ID into `CARTESIA_VOICE_ID`.

**3. Fill in the real institute data**

Open `prompts.py` and replace every `>>> FILL THIS IN <<<` — fees per
course, placement companies/packages, internship partners. Rohit is
instructed **not to invent** these; if you leave them blank, Rohit will
offer to connect the caller to a human instead of guessing.

**4. Start the server**

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

**5. Expose it** (development only — Twilio needs a public URL)

```bash
ngrok http 8000
```

Copy the `https://...ngrok...` URL into `PUBLIC_HOST` in `.env`.

**6. Make a test call**

```bash
python make_call.py --to +9198XXXXXXXX --name "Priya" --course MBA
```

Rohit will phone that number and run the script. Course options:
`BBA`, `BCA`, `MBA`, `MCA`, `BJMC` — Rohit asks the correct entrance-exam
question for each (e.g. CAT/CMAT/CET for MBA, NIMCET/CET/CUET for MCA).

---

## Tuning latency

If Rohit feels slow, in roughly this priority order:

1. **Host close to your callers.** Run the server in an India region.
   A US-hosted server adds 200ms+ for Indian calls.
2. **Keep the LLM fast.** Haiku is already fast; don't switch to a large
   model. Keep `max_tokens` low — the prompt already forces short replies.
3. **Tune the VAD.** `stop_secs` in `bot.py` is the silence gap before
   Rohit speaks. 0.4s is a good start. Lower = snappier but more likely to
   cut callers off; higher = more relaxed but slower.
4. **Use streaming TTS** (Cartesia already streams). Rohit starts speaking
   before the full sentence is generated.

`enable_metrics=True` in `bot.py` logs per-stage latency so you can see
exactly where time goes.

---

## Hindi / Hinglish

Many students near Janak Puri will mix Hindi and English. To support that:

- In `bot.py`, change Deepgram `language` to `"hi"` or `"multi"` and test
  with real callers.
- Pick a Cartesia voice that handles Hindi well.
- The prompt already lets Rohit reply in Hinglish when the caller does.

Test this with actual callers — code-mixed speech is where STT quality
varies most between providers.

---

## One important note before going live

Rohit already introduces himself by name at the start of every call, which
is good practice. Outbound automated calling is regulated — in India that
includes TRAI / DLT rules for the phone number and consent norms for
contacting people. Make sure the numbers you call are people who opted in,
and keep Rohit identifying itself clearly. Aim for "natural and pleasant,"
not "secretly indistinguishable from a human" — that keeps you on safe
ground legally and ethically.

---

## Going to production

- Replace `ngrok` with a real hosted domain (Render, Railway, Fly.io, AWS).
- Swap the in-memory `PENDING_CALLS` dict in `server.py` for Redis or a DB.
- Add call logging / transcripts so the admissions team can review calls.
- Add retry logic and a do-not-call list.
- Load-test before running campaigns — each concurrent call is one pipeline.
