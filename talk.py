#!/usr/bin/env python3
"""Talk to the assistant from this laptop - the ESP32's job, done by your mic and speakers.

Runs as a continuous conversation: it listens, replies, then listens again. It
detects when you stop speaking instead of recording for a fixed time, and it
remembers the conversation so follow-up questions work.

    ./.venv/bin/python talk.py                       # conversation loop, Ctrl+C to stop
    ./.venv/bin/python talk.py --once                # single question, then exit
    ./.venv/bin/python talk.py -t "hello"            # type instead of speaking
    ./.venv/bin/python talk.py --url https://voice-assistant-backend-a5qi.onrender.com

Requires the backend to be running. Uses arecord/pw-play, already on this system.
"""

import argparse
import array
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import wave
from pathlib import Path

import requests

DEFAULT_URL = "http://localhost:8000"
SAMPLE_RATE = 16000
CHUNK_MS = 100
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MS // 1000  # 16-bit mono

CALIBRATE_CHUNKS = 8        # ~0.8s of room tone to measure the noise floor
SILENCE_MULTIPLIER = 2.5    # speech must be this much louder than the room
MIN_THRESHOLD = 250         # floor, for very quiet rooms
START_TIMEOUT_S = 12        # give up if nobody speaks
SILENCE_HOLD_S = 1.2        # end the turn after this much quiet
MAX_TURN_S = 20             # hard cap on one utterance


def die(message: str) -> None:
    print(f"\n  {message}", file=sys.stderr)
    sys.exit(1)


def rms(chunk: bytes) -> float:
    """Loudness of one chunk. audioop was removed in Python 3.13, so do it by hand."""
    if len(chunk) < 2:
        return 0.0
    samples = array.array("h", chunk[: len(chunk) // 2 * 2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def record_until_silence(path: Path) -> bool:
    """Record one utterance, stopping when the speaker goes quiet.

    Returns False if nobody spoke before the timeout.
    """
    if not shutil.which("arecord"):
        die("arecord not found. Install alsa-utils, or use -t to type instead of speaking.")

    proc = subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    frames = bytearray()
    speaking = False
    silent_chunks = 0
    elapsed_chunks = 0
    noise_floor = 0.0

    silence_limit = int(SILENCE_HOLD_S * 1000 / CHUNK_MS)
    start_limit = int(START_TIMEOUT_S * 1000 / CHUNK_MS)
    max_chunks = int(MAX_TURN_S * 1000 / CHUNK_MS)

    try:
        while True:
            chunk = proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            elapsed_chunks += 1
            level = rms(chunk)

            # First few chunks measure the room rather than the speaker.
            if elapsed_chunks <= CALIBRATE_CHUNKS:
                noise_floor = max(noise_floor, level)
                continue
            if elapsed_chunks == CALIBRATE_CHUNKS + 1:
                threshold = max(noise_floor * SILENCE_MULTIPLIER, MIN_THRESHOLD)
                print("  🎤 listening…", flush=True)

            if not speaking:
                if level > threshold:
                    speaking = True
                    frames += chunk
                elif elapsed_chunks - CALIBRATE_CHUNKS > start_limit:
                    return False
                continue

            frames += chunk
            silent_chunks = silent_chunks + 1 if level <= threshold else 0
            if silent_chunks >= silence_limit or elapsed_chunks >= max_chunks:
                break
    finally:
        proc.terminate()
        proc.wait(timeout=2)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(frames))
    return True


def play(path: Path) -> None:
    """Play the reply through the speakers."""
    for player in (["pw-play"], ["mpg123", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(player[0]):
            subprocess.run([*player, str(path)], capture_output=True)
            return
    print(f"  No audio player found. Reply saved at {path}")


def save_reply(response, work: Path) -> Path:
    """Groq TTS returns wav, gTTS returns mp3 - name the file for what arrived."""
    suffix = ".wav" if "wav" in response.headers.get("content-type", "") else ".mp3"
    path = work / f"reply{suffix}"
    path.write_bytes(response.content)
    return path


def ask_text(args, headers: str, session: str, work: Path) -> None:
    print(f"\n  You: {args.text}")
    response = requests.post(f"{args.url}/api/chat", headers=headers, timeout=90,
                             json={"message": args.text, "session_id": session})
    if response.status_code != 200:
        die(f"Chat failed ({response.status_code}): {response.text}")
    reply = response.json()["reply"]
    print(f"  Assistant: {reply}\n")

    speech = requests.post(f"{args.url}/api/tts", headers=headers, timeout=90,
                           json={"text": reply})
    if speech.status_code != 200:
        die(f"TTS failed ({speech.status_code}): {speech.text}")
    play(save_reply(speech, work))


def ask_voice(args, headers: dict, session: str, work: Path) -> bool:
    """One spoken turn. Returns False if nothing was said."""
    wav = work / "input.wav"
    if not record_until_silence(wav):
        return False

    print("  … thinking", flush=True)
    fields = {"session_id": session}
    if args.lat is not None and args.lon is not None:
        fields |= {"lat": str(args.lat), "lon": str(args.lon)}

    with open(wav, "rb") as fh:
        response = requests.post(
            f"{args.url}/api/voice",
            files={"file": ("input.wav", fh, "audio/wav")},
            data=fields,
            headers=headers,
            timeout=180,
        )
    if response.status_code == 400:
        print("  (didn't catch that)")
        return True
    if response.status_code != 200:
        die(f"Pipeline failed ({response.status_code}): {response.text}")

    print(f"  You said:  {response.headers.get('X-Heard-Text', '?')}")
    print(f"  Assistant: {response.headers.get('X-Reply-Text', '?')}\n")
    play(save_reply(response, work))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice client for the wearable assistant backend")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"backend base URL (default {DEFAULT_URL})")
    parser.add_argument("-t", "--text", help="send this text instead of recording from the mic")
    parser.add_argument("--once", action="store_true", help="one exchange, then exit")
    parser.add_argument("--lat", type=float, help="pretend the GPS has this fix")
    parser.add_argument("--lon", type=float, help="pretend the GPS has this fix")
    parser.add_argument("--key", default=os.getenv("API_KEY"),
                        help="X-API-Key value, if the backend has API_SECRET set")
    args = parser.parse_args()

    headers = {"X-API-Key": args.key} if args.key else {}
    session = uuid.uuid4().hex  # ties this run's turns into one conversation

    # A cloud instance may be asleep, so give the first request room to cold-start.
    try:
        health = requests.get(f"{args.url}/health", headers=headers, timeout=90).json()
    except requests.RequestException:
        die(f"Backend not reachable at {args.url}\n"
            f"  Start it with: ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000")
    if not health.get("groq_key_set"):
        die("Backend is up but GROQ_API_KEY is not set - check your .env file.")

    work = Path(tempfile.mkdtemp(prefix="talk_"))
    try:
        if args.text:
            ask_text(args, headers, session, work)
            return

        print(f"\n  Connected to {args.url}")
        print("  Speak when you see 'listening' - it replies when you stop talking.")
        print("  Ctrl+C to end the conversation.\n")

        # Silence never ends the conversation - only Ctrl+C does. This mirrors the
        # ESP32, where the button starts a conversation and the button ends it.
        while True:
            spoke = ask_voice(args, headers, session, work)
            if args.once:
                return
            if not spoke:
                print("  (still there - keep talking, or Ctrl+C to stop)")
    except KeyboardInterrupt:
        print("\n  Conversation ended.")
    finally:
        try:
            requests.post(f"{args.url}/api/reset", headers=headers, timeout=15,
                          json={"session_id": session})
        except requests.RequestException:
            pass  # best-effort cleanup; the session expires on its own anyway
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
