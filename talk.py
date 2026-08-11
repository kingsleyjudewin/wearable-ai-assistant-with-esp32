#!/usr/bin/env python3
"""Talk to the assistant from this laptop - the ESP32's job, done by your mic and speakers.

    ./.venv/bin/python talk.py              # press Enter, speak 5s, hear the reply
    ./.venv/bin/python talk.py -d 8         # record for 8 seconds instead
    ./.venv/bin/python talk.py -t "hello"   # skip the mic, type the question
    ./.venv/bin/python talk.py --url http://192.168.1.42:8000

Requires the backend to be running. Uses arecord/pw-play, already on this system.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

DEFAULT_URL = "http://localhost:8000"
RECORD_SECONDS = 5
SAMPLE_RATE = 16000


def die(message: str) -> None:
    print(f"\n  {message}", file=sys.stderr)
    sys.exit(1)


def record(path: Path, seconds: int) -> None:
    """Capture mono 16 kHz wav from the default mic - the format the ESP32 sends."""
    if not shutil.which("arecord"):
        die("arecord not found. Install alsa-utils, or use -t to type instead of speaking.")

    input(f"  Press Enter, then speak for {seconds} seconds...")
    print("  ● recording", flush=True)
    result = subprocess.run(
        ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1",
         "-d", str(seconds), str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not path.exists() or path.stat().st_size < 1000:
        die(f"Recording failed: {result.stderr.strip() or 'no audio captured'}\n"
            f"  Check your mic is not muted, then try again.")
    print("  ✓ recorded")


def play(path: Path) -> None:
    """Play the mp3 reply through the speakers."""
    for player in (["pw-play"], ["mpg123", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(player[0]):
            print("  🔊 playing reply", flush=True)
            subprocess.run([*player, str(path)], capture_output=True)
            return
    print(f"  No audio player found. Reply saved at {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice client for the wearable assistant backend")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"backend base URL (default {DEFAULT_URL})")
    parser.add_argument("-d", "--duration", type=int, default=RECORD_SECONDS, help="seconds to record")
    parser.add_argument("-t", "--text", help="send this text instead of recording from the mic")
    parser.add_argument("--keep", action="store_true", help="keep the audio files instead of deleting them")
    parser.add_argument("--key", default=os.getenv("API_KEY"),
                        help="X-API-Key value, if the backend has API_SECRET set")
    args = parser.parse_args()

    # A cloud instance may be asleep, so give the first request room to cold-start.
    headers = {"X-API-Key": args.key} if args.key else {}

    try:
        health = requests.get(f"{args.url}/health", timeout=60).json()
    except requests.RequestException:
        die(f"Backend not reachable at {args.url}\n"
            f"  Start it with: ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000")
    if not health.get("groq_key_set"):
        die("Backend is up but GROQ_API_KEY is not set - check your .env file.")

    work = Path(tempfile.mkdtemp(prefix="talk_"))

    try:
        if args.text:
            # Text path: /api/chat for the answer, /api/tts to speak it.
            print(f"\n  You: {args.text}")
            response = requests.post(f"{args.url}/api/chat", json={"message": args.text},
                                     headers=headers, timeout=90)
            if response.status_code != 200:
                die(f"Chat failed ({response.status_code}): {response.text}")
            reply = response.json()["reply"]
            print(f"  Assistant: {reply}\n")

            speech = requests.post(f"{args.url}/api/tts", json={"text": reply},
                                   headers=headers, timeout=90)
            if speech.status_code != 200:
                die(f"TTS failed ({speech.status_code}): {speech.text}")
            audio = speech
        else:
            # Voice path: the full pipeline, exactly what the ESP32 will call.
            wav = work / "input.wav"
            record(wav, args.duration)

            print("  … thinking", flush=True)
            with open(wav, "rb") as fh:
                response = requests.post(
                    f"{args.url}/api/voice",
                    files={"file": ("input.wav", fh, "audio/wav")},
                    headers=headers,
                    timeout=180,
                )
            if response.status_code != 200:
                die(f"Pipeline failed ({response.status_code}): {response.text}")

            print(f"\n  You said: {response.headers.get('X-Heard-Text', '?')}")
            print(f"  Assistant: {response.headers.get('X-Reply-Text', '?')}\n")
            audio = response

        # Groq TTS returns wav, gTTS returns mp3 - name the file for what arrived.
        suffix = ".wav" if "wav" in audio.headers.get("content-type", "") else ".mp3"
        reply_path = work / f"reply{suffix}"
        reply_path.write_bytes(audio.content)

        play(reply_path)
        if args.keep:
            print(f"  Audio kept in {work}")
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
