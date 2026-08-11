"""FastAPI backend for a wrist-worn voice assistant.

Pipeline: ESP32 uploads recorded audio -> Groq Whisper (STT) -> Groq Llama (LLM)
-> gTTS (TTS) -> mp3 reply streamed back to the device.
"""

import logging
import os
import re
import uuid
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from gtts import gTTS
from pydantic import BaseModel
from starlette.background import BackgroundTask

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
logger = logging.getLogger("assistant")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Optional shared secret. Once the backend is on a public URL, anyone who finds it
# can burn your Groq quota; set API_SECRET in the host's dashboard and have the
# ESP32 send it as an X-API-Key header. Unset (the local default) = no auth.
API_SECRET = os.getenv("API_SECRET")

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TTS_URL = "https://api.groq.com/openai/v1/audio/speech"
STT_MODEL = "whisper-large-v3-turbo"
CHAT_MODEL = "llama-3.3-70b-versatile"

# "auto" tries Groq first and falls back to gTTS; "groq" or "gtts" pin one provider.
#
# Prefer Groq once deployed: gTTS scrapes an unofficial Google endpoint that
# rate-limits datacenter IPs, so it works from a laptop but is unreliable from a
# cloud host. Groq TTS also returns WAV, which the ESP32 can feed straight to the
# MAX98357A without an mp3 decoder.
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "auto").lower()
GROQ_TTS_MODEL = os.getenv("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
# Orpheus voices: autumn, diana, hannah, austin, daniel, troy.
GROQ_TTS_VOICE = os.getenv("GROQ_TTS_VOICE", "hannah")
# WAV comes back as 16-bit mono 24 kHz - the ESP32 can push it straight to the
# MAX98357A over I2S with no mp3 decoder in between.
GROQ_TTS_FORMAT = os.getenv("GROQ_TTS_FORMAT", "wav")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
# Nominatim's usage policy requires a self-identifying User-Agent and allows
# roughly one request per second. Don't hammer it. Add a real contact address
# below if you deploy this - but never a placeholder domain, Nominatim 403s any
# User-Agent containing example.com.
USER_AGENT = "wrist-voice-assistant/1.0 (ESP32 wearable project)"

TEMP_DIR = Path("/tmp/voice_assistant")

STT_TIMEOUT = 60
LLM_TIMEOUT = 60
TTS_TIMEOUT = 60
GEO_TIMEOUT = 15

AUDIO_MEDIA_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac"}

# Which TTS provider actually served the last reply - reported by /health so you
# can tell from the outside whether Groq TTS is live or gTTS is covering for it.
_tts_state = {"active": None}

# The INMP441 sends 16-bit mono PCM WAV at 16 kHz - one second of speech is ~32 KB
# plus a 44-byte header, so anything under 1 KB is a truncated or dead recording.
# Raise this once real device uploads show their typical size.
MIN_AUDIO_BYTES = 1024

SYSTEM_PROMPT = (
    "You are a helpful voice assistant worn on someone's wrist. Keep answers short, "
    "spoken-style, and conversational - 1-3 sentences unless asked for detail. "
    "No markdown, no bullet points, no emojis, since this will be read aloud."
)

NAV_INTENT_RE = re.compile(
    r"\b(navigate|directions|how (do|to) i get to|route to|distance to)\b",
    re.IGNORECASE,
)

LOCATION_INTENT_RE = re.compile(
    r"\b(where am i|my location|current location|where are we)\b",
    re.IGNORECASE,
)

NAV_PLACEHOLDER_REPLY = (
    "I can look up directions, but I need both a starting point and a destination. "
    "Send them to the directions endpoint and I'll give you the distance and travel time."
)

NO_GPS_FIX_REPLY = "I don't have a GPS fix yet. Try again in a moment."


@asynccontextmanager
async def lifespan(app: FastAPI):
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Wearable Voice Assistant Backend", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    message: str


class TTSRequest(BaseModel):
    text: str


class DirectionsRequest(BaseModel):
    origin: str
    destination: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _require_key() -> str:
    if not GROQ_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Config failed: GROQ_API_KEY is not set (see .env.example)",
        )
    return GROQ_API_KEY


def _upstream_error(service: str, response: requests.Response) -> HTTPException:
    """Surface the upstream status code and body instead of failing silently."""
    return HTTPException(
        status_code=response.status_code,
        detail=f"{service} failed: {response.text}",
    )


def _request(method: str, url: str, service: str, **kwargs) -> requests.Response:
    """Outbound HTTP with every failure mode mapped to a clean JSON error.

    Network-level failures (timeouts, DNS, refused connections) would otherwise
    escape as a bare 500 with an HTML body, which the ESP32 can't parse.
    """
    try:
        response = requests.request(method, url, **kwargs)
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail=f"{service} timed out: {exc}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"{service} unreachable: {exc}") from exc

    if response.status_code != 200:
        raise _upstream_error(service, response)
    return response


def _header_safe(value: str, limit: int = 400) -> str:
    """HTTP headers must be latin-1 encodable; transcripts may not be."""
    trimmed = value.replace("\n", " ").replace("\r", " ").strip()[:limit]
    return trimmed.encode("ascii", "backslashreplace").decode("ascii")


def _temp_path(suffix: str) -> Path:
    return TEMP_DIR / f"{uuid.uuid4().hex}{suffix}"


def _audio_summary(path: Path, size: int) -> str:
    """Describe an upload for the console.

    Surfaces the format the device actually sent, so wrong sample rate, wrong
    channel count or truncated recordings are obvious from the log alone.
    """
    try:
        with wave.open(str(path)) as wav:
            rate = wav.getframerate()
            seconds = wav.getnframes() / rate if rate else 0.0
            return (
                f"{size} bytes, {seconds:.1f}s, {rate} Hz, "
                f"{wav.getnchannels()} ch, {wav.getsampwidth() * 8}-bit"
            )
    except (wave.Error, EOFError):
        return f"{size} bytes, not a WAV container"


def save_upload(upload: UploadFile, suffix: str = ".wav") -> tuple[Path, int]:
    """Persist an upload, rejecting unusable audio before it costs an API call."""
    data = upload.file.read()
    size = len(data)

    if size == 0:
        logger.warning("upload rejected: no audio received (0 bytes)")
        raise HTTPException(status_code=400, detail="Upload rejected: no audio received (0 bytes)")

    if size < MIN_AUDIO_BYTES:
        logger.warning("upload rejected: %d bytes is below the %d byte minimum", size, MIN_AUDIO_BYTES)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Upload rejected: audio too small to be a real recording "
                f"({size} bytes, minimum {MIN_AUDIO_BYTES})"
            ),
        )

    path = _temp_path(suffix)
    path.write_bytes(data)
    return path, size


def transcribe_audio(audio_path: Path) -> str:
    """Groq Whisper speech-to-text."""
    key = _require_key()
    with open(audio_path, "rb") as fh:
        response = _request(
            "POST",
            GROQ_STT_URL,
            "STT",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (audio_path.name, fh, "audio/wav")},
            data={"model": STT_MODEL},
            timeout=STT_TIMEOUT,
        )
    return response.json().get("text", "").strip()


def chat_with_llm(user_text: str) -> str:
    """Groq chat completion, OpenAI-compatible payload."""
    key = _require_key()
    response = _request(
        "POST",
        GROQ_CHAT_URL,
        "LLM",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.6,
            "max_tokens": 200,
        },
        timeout=LLM_TIMEOUT,
    )
    return response.json()["choices"][0]["message"]["content"].strip()


def _groq_tts(text: str, out_path: Path) -> None:
    """Groq text-to-speech. Same API key as STT/LLM, and survives datacenter IPs."""
    key = _require_key()
    response = _request(
        "POST",
        GROQ_TTS_URL,
        "TTS",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": GROQ_TTS_MODEL,
            "input": text,
            "voice": GROQ_TTS_VOICE,
            "response_format": GROQ_TTS_FORMAT,
        },
        timeout=TTS_TIMEOUT,
    )
    out_path.write_bytes(response.content)


def _gtts_tts(text: str, out_path: Path) -> None:
    """gTTS fallback. Free and keyless, but unreliable from cloud hosts."""
    try:
        gTTS(text=text, lang="en").save(str(out_path))
    except Exception as exc:  # gTTS wraps network failures in its own errors
        raise HTTPException(status_code=502, detail=f"TTS failed (gtts): {exc}") from exc


def synthesize_speech(text: str) -> tuple[Path, str]:
    """Speak `text`. Returns (path, media_type) - the format depends on the provider."""
    if TTS_PROVIDER in ("auto", "groq"):
        out_path = _temp_path(f".{GROQ_TTS_FORMAT}")
        try:
            _groq_tts(text, out_path)
            _tts_state["active"] = "groq"
            return out_path, AUDIO_MEDIA_TYPES.get(out_path.suffix, "application/octet-stream")
        except HTTPException as exc:
            out_path.unlink(missing_ok=True)
            if TTS_PROVIDER == "groq":  # pinned, so surface the failure
                raise
            logger.warning("[tts] Groq unavailable, falling back to gTTS: %s", str(exc.detail)[:200])

    out_path = _temp_path(".mp3")
    _gtts_tts(text, out_path)
    _tts_state["active"] = "gtts"
    return out_path, "audio/mpeg"


def geocode_place(place: str) -> dict:
    """OpenStreetMap Nominatim forward geocoding."""
    response = _request(
        "GET",
        NOMINATIM_URL,
        "Geocoding",
        params={"q": place, "format": "json", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=GEO_TIMEOUT,
    )
    results = response.json()
    if not results:
        raise HTTPException(status_code=404, detail=f"Geocoding failed: no location found for '{place}'")

    top = results[0]
    return {
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "display_name": top["display_name"],
    }


def reverse_geocode_coords(lat: float, lon: float) -> dict:
    """Nominatim reverse geocoding - turns a Neo-6M GPS fix into a street address."""
    response = _request(
        "GET",
        NOMINATIM_REVERSE_URL,
        "Reverse geocode",
        params={"lat": lat, "lon": lon, "format": "json"},
        headers={"User-Agent": USER_AGENT},
        timeout=GEO_TIMEOUT,
    )
    result = response.json()
    # Nominatim answers open ocean / unmapped coordinates with {"error": "..."}.
    if not result or "error" in result or "display_name" not in result:
        raise HTTPException(
            status_code=404,
            detail=f"Reverse geocode failed: no address found at {lat}, {lon}",
        )

    return {
        "display_name": result["display_name"],
        "lat": float(result["lat"]),
        "lon": float(result["lon"]),
    }


def get_directions(origin: str, destination: str) -> dict:
    """Geocode both endpoints, then route between them via the OSRM demo server."""
    start = geocode_place(origin)
    end = geocode_place(destination)

    coords = f"{start['lon']},{start['lat']};{end['lon']},{end['lat']}"
    response = _request(
        "GET",
        f"{OSRM_URL}/{coords}",
        "Routing",
        params={"overview": "false"},
        timeout=GEO_TIMEOUT,
    )
    routes = response.json().get("routes") or []
    if not routes:
        raise HTTPException(
            status_code=404,
            detail=f"Routing failed: no route between '{origin}' and '{destination}'",
        )

    route = routes[0]
    return {
        "distance_km": round(route["distance"] / 1000, 2),
        "duration_min": round(route["duration"] / 60, 1),
        "origin": start["display_name"],
        "destination": end["display_name"],
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


def require_api_key(x_api_key: str | None = Header(None)):
    """No-op unless API_SECRET is set, so local development is unaffected."""
    if API_SECRET and x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Auth failed: missing or invalid X-API-Key header")


# /health is deliberately unauthenticated: uptime pingers need to reach it to keep
# free-tier instances from sleeping.
@app.get("/health")
def health():
    return {
        "status": "ok",
        "groq_key_set": bool(GROQ_API_KEY),
        "auth_required": bool(API_SECRET),
        "tts_provider": TTS_PROVIDER,
        "tts_last_used": _tts_state["active"],
    }


# Endpoints below are sync `def` on purpose: they make blocking `requests` calls, so
# FastAPI runs them in a threadpool. As `async def` they would block the event loop
# and serialise every concurrent request behind the slowest one.
@app.post("/api/stt", dependencies=[Depends(require_api_key)])
def stt(file: UploadFile = File(...)):
    audio_path, size = save_upload(file)
    logger.info("[stt] received %s", _audio_summary(audio_path, size))
    try:
        text = transcribe_audio(audio_path)
    finally:
        audio_path.unlink(missing_ok=True)
    logger.info("[stt] transcript: %r", text)
    return {"text": text}


@app.post("/api/chat", dependencies=[Depends(require_api_key)])
def chat(request: ChatRequest):
    return {"reply": chat_with_llm(request.message)}


@app.post("/api/tts", dependencies=[Depends(require_api_key)])
def tts(request: TTSRequest):
    speech_path, media_type = synthesize_speech(request.text)
    return FileResponse(
        speech_path,
        media_type=media_type,
        filename=f"speech{speech_path.suffix}",
        background=BackgroundTask(speech_path.unlink, missing_ok=True),
    )


@app.get("/api/geocode", dependencies=[Depends(require_api_key)])
def geocode(place: str):
    return geocode_place(place)


@app.get("/api/reverse-geocode", dependencies=[Depends(require_api_key)])
def reverse_geocode(lat: float, lon: float):
    """Neo-6M GPS coordinates -> street address."""
    return reverse_geocode_coords(lat, lon)


@app.post("/api/directions", dependencies=[Depends(require_api_key)])
def directions(request: DirectionsRequest):
    return get_directions(request.origin, request.destination)


@app.post("/api/voice", dependencies=[Depends(require_api_key)])
def voice(
    file: UploadFile = File(...),
    lat: float | None = Form(None),
    lon: float | None = Form(None),
):
    """Main pipeline the ESP32 calls: audio in, audio out.

    `lat`/`lon` come from the Neo-6M when it has a fix, and are absent otherwise.
    """
    audio_path, size = save_upload(file)
    has_fix = lat is not None and lon is not None
    logger.info(
        "[voice] received %s | gps: %s",
        _audio_summary(audio_path, size),
        f"{lat}, {lon}" if has_fix else "no fix",
    )

    try:
        heard_text = transcribe_audio(audio_path)
    finally:
        audio_path.unlink(missing_ok=True)

    if not heard_text:
        logger.warning("[voice] STT returned nothing - silence or unusable audio")
        raise HTTPException(status_code=400, detail="STT failed: no speech detected in the audio")

    logger.info("[voice] heard: %r", heard_text)

    # Location questions are checked first: they are answerable straight from the
    # GPS fix, so they never need to reach the LLM.
    if LOCATION_INTENT_RE.search(heard_text):
        if has_fix:
            branch = "reverse-geocode"
            place = reverse_geocode_coords(lat, lon)
            reply_text = f"You're near {place['display_name']}."
        else:
            branch = "no-gps-fix"
            reply_text = NO_GPS_FIX_REPLY
    # v1 navigation handling is a keyword placeholder; real intent + slot
    # extraction (parsing origin/destination out of speech) comes later.
    elif NAV_INTENT_RE.search(heard_text):
        branch = "navigate-placeholder"
        reply_text = NAV_PLACEHOLDER_REPLY
    else:
        branch = "llm-chat"
        reply_text = chat_with_llm(heard_text)

    logger.info("[voice] branch: %s", branch)
    logger.info("[voice] reply: %r", reply_text)

    speech_path, media_type = synthesize_speech(reply_text)
    logger.info("[voice] spoke via %s (%s)", _tts_state["active"], media_type)

    return FileResponse(
        speech_path,
        media_type=media_type,
        filename=f"reply{speech_path.suffix}",
        headers={
            "X-Heard-Text": _header_safe(heard_text),
            "X-Reply-Text": _header_safe(reply_text),
        },
        background=BackgroundTask(speech_path.unlink, missing_ok=True),
    )


if __name__ == "__main__":
    # Hosts like Render assign the port at runtime; honour $PORT when present.
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
