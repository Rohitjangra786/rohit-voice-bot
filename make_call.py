"""
make_call.py
------------
Triggers an OUTBOUND call — Rohit phones a prospective student.

Usage:
    python make_call.py --to +9198XXXXXXXX --name "Priya" --course MBA

What happens:
    1. Twilio places a call from your Twilio number to the student.
    2. When the student picks up, Twilio fetches TwiML from PUBLIC_HOST/twiml.
    3. That TwiML opens a media stream to server.py, which runs Rohit.

Before running:
    * server.py must be live and reachable at PUBLIC_HOST (use ngrok in dev).
    * Fill in TWILIO_* and PUBLIC_HOST in .env.
"""

import argparse
import os

from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()


def make_call(to_number: str, name: str, course: str):
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_PHONE_NUMBER"]
    public_host = os.environ["PUBLIC_HOST"].rstrip("/")

    client = Client(account_sid, auth_token)

    # Candidate name + course are passed to TwiML as query params, which the
    # /twiml endpoint can forward as <Parameter> tags into the media stream.
    twiml_url = (
        f"{public_host}/twiml"
        f"?candidateName={name}&course={course}"
    )

    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=twiml_url,
        method="POST",
    )
    print(f"Call placed. SID: {call.sid}")
    print(f"Calling {name} ({to_number}) about the {course} course.")
    return call.sid


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Have Rohit call a student.")
    parser.add_argument("--to", required=True, help="Student phone, e.g. +9198...")
    parser.add_argument("--name", default="", help="Student's name")
    parser.add_argument("--course", default="BBA",
                        help="Course of interest: BBA / BCA / MBA / MCA / BJMC")
    args = parser.parse_args()
    make_call(args.to, args.name, args.course)
