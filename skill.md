---
name: wearable-ai-assistant
description: Context and specs for the wrist-worn AI voice assistant project — an ESP32-based wearable that records audio on a button press, sends it to a FastAPI backend over WiFi, and plays back an AI-generated spoken reply (general conversation + basic navigation). Use this skill whenever the user refers to "the wearable AI project," "the ESP32 assistant," "my wrist AI," or asks to continue work on the backend, firmware, hardware wiring, or API integration for this specific project, so all prior architecture decisions carry forward instead of being re-derived from scratch.
---

# Wearable AI Voice Assistant — Project Skill

A wrist-worn device (ESP32) that lets the user press a button, ask a question out loud,
and hear a spoken AI reply — general conversation plus basic navigation/directions.

## Hardware

- **Board**: ESP32-WROOM-32 DevKit (standard 30-pin "DOIT DEVKIT V1" style). **No PSRAM** —
  only base 520KB SRAM, so audio must be streamed/uploaded in chunks rather than buffered
  in full for long clips.
- **Mic**: I2S digital mic (e.g. INMP441) — planned, not yet wired.
- **Speaker output**: I2S amp (e.g. MAX98357A) — planned, not yet wired.
- **Input**: a push button, `INPUT_PULLUP`, on a free GPIO (e.g. G4 or G5).
- **Pin notes**:
  - Good free GPIOs for I2S: G25, G26, G27, G14, G12 (left side) or G18, G19, G21, G22
    (right side).
  - Avoid G0, G2, G12, G15 as I2S data pins — they affect boot mode.
  - G34, G35, G32, G33 are input-only — fine for the button, not for I2S output.
  - TXD/RXD (right side) are USB serial — used only for flashing/debugging, **not** part
    of the live pipeline (that's WiFi, since the ESP32 has built-in WiFi).

## Architecture

```
[Button press] -> ESP32 records audio (I2S mic)
               -> ESP32 uploads WAV over WiFi to backend (POST /api/voice)
               -> Backend: Speech-to-Text -> LLM reasoning -> Text-to-Speech
               -> Backend returns mp3 reply
               -> ESP32 plays it back (I2S amp)
```

The backend holds all API keys — never put keys on the ESP32 firmware itself.

## Backend stack (FastAPI, Python)

All free-tier / no-cost services, chosen specifically to avoid any paid API:

| Piece | Service | Key needed? |
|---|---|---|
| STT | Groq API, model `whisper-large-v3-turbo` | Yes — `GROQ_API_KEY` |
| LLM | Groq API, model `llama-3.3-70b-versatile` | Same key as STT |
| TTS | gTTS (Google Translate TTS wrapper, Python lib) | No |
| Geocoding | OpenStreetMap Nominatim | No (custom `User-Agent` header required, ~1 req/sec limit) |
| Directions | OSRM public demo server | No (best-effort public server, don't hammer it) |

Only **one** API key is needed total (Groq), since it covers both STT and LLM.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Sanity check |
| POST | `/api/stt` | Test STT alone (audio in, text out) |
| POST | `/api/chat` | Test LLM alone (text in, text out) |
| POST | `/api/tts` | Test TTS alone (text in, mp3 out) |
| GET | `/api/geocode` | Test geocoding (`?place=`) |
| POST | `/api/directions` | Test routing (`{origin, destination}`) |
| POST | `/api/voice` | **Main pipeline** — audio in, mp3 reply out. This is the one the ESP32 calls. |

`/api/voice` returns `X-Heard-Text` and `X-Reply-Text` response headers for debugging what
was transcribed and replied without needing to decode the audio.

### LLM system prompt (persona)
> "You are a helpful voice assistant worn on someone's wrist. Keep answers short,
> spoken-style, and conversational — 1-3 sentences unless asked for detail. No markdown,
> no bullet points, no emojis, since this will be read aloud."

### Navigation intent detection (v1, simple)
Keyword regex on the transcript: `\b(navigate|directions|how (do|to) i get to|route to|
distance to)\b`. If matched, currently returns a placeholder pointing at
`/api/directions` rather than doing real slot extraction — proper LLM tool-calling for
this is a known future improvement, not yet built.

## Full detailed build spec
A complete, standalone build prompt with exact request/response formats for every
external API call (Groq STT/LLM request bodies, Nominatim/OSRM query formats, gTTS usage,
full endpoint table, pipeline logic, and acceptance test) was already written out in this
project — reuse that level of detail rather than re-deriving it if asked to (re)build the
backend.

## Status / next steps
- [x] Hardware identified (ESP32-WROOM-32 DevKit, no PSRAM)
- [x] Backend architecture and free API stack decided
- [x] Full FastAPI endpoint spec written
- [ ] Backend actually implemented and tested end-to-end via curl
- [ ] I2S mic + amp wired to the ESP32
- [ ] ESP32 firmware: button -> record -> upload to `/api/voice` -> play mp3 reply
- [ ] Real navigation intent/slot extraction (replace v1 keyword regex)
- [ ] Conversation memory across requests (currently stateless)

## Working style notes for this project
- User wants to validate the backend fully (via curl) before writing any ESP32 firmware —
  don't jump ahead to hardware/firmware code until the backend loop is confirmed working.
- User prefers to receive detailed prompts/specs they can act on themselves, not always
  full generated code — check whether they're asking for a build vs. a spec before
  producing files.