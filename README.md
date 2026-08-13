# Wearable Voice Assistant — Backend

FastAPI backend for a wrist-worn voice assistant. An ESP32 records audio, uploads it here,
and gets back a spoken reply:

```
ESP32 ──wav──▶ /api/voice ──▶ Groq Whisper (STT) ──▶ Groq Llama 3.3 (LLM) ──▶ gTTS ──mp3──▶ ESP32
```

Plus standalone endpoints for testing each stage, and geocoding/routing via free
OpenStreetMap services.

## Setup

1. **Get a free Groq API key** at [console.groq.com](https://console.groq.com) → *API Keys*.
   The free tier covers both the Whisper transcription and Llama chat calls.

2. **Create the env file:**

   ```bash
   cp .env.example .env
   # then edit .env and paste your key into GROQ_API_KEY
   ```

3. **Install dependencies** (a virtualenv is recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Run the server.** Bind to `0.0.0.0` so the ESP32 can reach it over your WiFi —
   `localhost` would only be reachable from this machine:

   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

   Find the LAN address to point the ESP32 at with `hostname -I` (e.g. `http://192.168.1.42:8000`).

Interactive API docs are at `http://localhost:8000/docs`.

## Talk to it from this laptop

`talk.py` stands in for the ESP32: it listens on your mic, sends the audio through the
pipeline, plays the spoken reply through your speakers, and then listens again. It's a
continuous conversation — it detects when you stop speaking rather than recording for a
fixed time, and only **Ctrl+C** ends it.

```bash
./.venv/bin/python talk.py                     # conversation loop
./.venv/bin/python talk.py --once              # a single question, then exit
./.venv/bin/python talk.py -t "hi there"       # type instead of speaking, still speaks back
./.venv/bin/python talk.py --lat 48.8584 --lon 2.2945   # pretend the GPS has a fix
./.venv/bin/python talk.py --url https://your-app.onrender.com
```

Every run gets its own `session_id`, so follow-ups work: *"What is the capital of Japan?"*
→ *"And what about Italy?"* resolves correctly. The session is cleared when you exit.

## Run the test suite

`test_e2e.sh` exercises every endpoint, generating its own spoken test audio with gTTS:

```bash
./test_e2e.sh                               # tests localhost:8000
BASE=http://192.168.1.42:8000 ./test_e2e.sh
```

## Deploying it (so the ESP32 doesn't need your laptop)

The repo is deployment-ready: [render.yaml](render.yaml) for Render blueprints, a
[Procfile](Procfile) for anything else, and `$PORT` honoured automatically.

**Use Render's free tier.** Railway removed theirs — you get a one-time $5 credit, then
it's paid.

1. Push to GitHub. `.env` is gitignored — **never commit your key**.
2. Render → *New* → *Blueprint* → select the repo. It reads `render.yaml`.
3. Paste `GROQ_API_KEY` when prompted (it's marked `sync: false`, so it's never in the repo).
4. Deploy, then check `https://your-app.onrender.com/health`.

### Three things that will bite you

**Free instances sleep after 15 minutes idle** and take ~50s to wake — unusable for a
button-press wearable. Point a free uptime pinger (UptimeRobot, cron-job.org) at `/health`
every 10 minutes. `/health` is deliberately unauthenticated so pingers can reach it. Free
tier gives 750 instance-hours/month; one always-awake service uses ~730, so it fits — but
only one.

**gTTS is unreliable from cloud hosts.** It scrapes an unofficial Google endpoint that
rate-limits datacenter IPs. That's why TTS defaults to `auto`: it tries **Groq TTS** first
and falls back to gTTS. Groq TTS returns 16-bit mono 24 kHz WAV, voices are
`autumn, diana, hannah, austin, daniel, troy` (set `GROQ_TTS_VOICE`), and it needs a
one-time terms acceptance at
[console.groq.com](https://console.groq.com/playground?model=canopylabs%2Forpheus-v1-english).
Check which provider is live:

```bash
curl https://your-app.onrender.com/health
# {"tts_provider":"auto","tts_last_used":"groq"}   <- what you want after accepting
# {"tts_provider":"auto","tts_last_used":"gtts"}   <- fallback still covering
```

Groq TTS also returns **WAV**, which the ESP32 can feed straight to the MAX98357A with no
mp3 decoder. Once it's live, set `TTS_PROVIDER=groq` to stop falling back silently.

**A public URL is a public bill.** Anyone who finds it can spend your Groq quota. Set
`API_SECRET` to a long random string in Render's dashboard and send it from the ESP32:

```bash
curl -H "X-API-Key: your-secret" https://your-app.onrender.com/api/chat ...
```

### Testing the deployment

Both tools work against the live URL:

```bash
BASE=https://your-app.onrender.com API_KEY=your-secret ./test_e2e.sh
./.venv/bin/python talk.py --url https://your-app.onrender.com --key your-secret
```

### On the ESP32 side

Render forces HTTPS, so use `WiFiClientSecure` — `setInsecure()` is simplest, or embed the
root CA. Set the HTTP timeout to 30s+ so a cold start doesn't abort the request.

## The ESP32 firmware

[esp32/esp32.ino](esp32/esp32.ino) is the device side: press the button to start a
conversation, it listens and replies continuously, press again to stop.

### Wiring

| INMP441 (mic) | ESP32 | | MAX98357A (amp) | ESP32 |
|---|---|---|---|---|
| VDD | 3V3 | | VIN | VIN (5V) |
| GND | GND | | GND | GND |
| L/R | GND | | DIN | GPIO 13 |
| WS | GPIO 25 | | BCLK | GPIO 27 |
| SCK | GPIO 26 | | LRC | GPIO 14 |
| SD | GPIO 34 | | speaker | screw terminals |

| Neo-6M (GPS) | ESP32 | | Button | ESP32 |
|---|---|---|---|---|
| VCC | VIN (5V) | | one leg | GPIO 4 |
| GND | GND | | other leg | GND |
| TX | GPIO 16 | | | |
| RX | GPIO 17 | | | |

Put the **220 µF** capacitor across the amplifier's VIN/GND (striped leg to GND — it's
polarised) and a **0.1 µF** ceramic across VIN/GND of both the mic and the amp, as close
to the modules as you can. Without them, loud audio browns out the ESP32 and it reboots
mid-sentence. Power the board from the **5V 2A adapter**, not a laptop USB port.

### Flashing

1. *File → Preferences → Additional board manager URLs*:
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. *Boards Manager* → install **esp32 by Espressif Systems**
3. *Tools → Board* → **ESP32 Dev Module**, *Partition Scheme* → **Huge APP**
4. Edit `WIFI_SSID`, `WIFI_PASSWORD` and `BACKEND_HOST` at the top of the sketch
5. Upload, then open *Serial Monitor* at **115200 baud**

No extra libraries are needed — GPS NMEA parsing is built in.

## Hardware contract

The device is an ESP32 with an INMP441 I2S mic, a MAX98357A I2S amp, a Neo-6M GPS module,
and a tactile button.

**Audio the ESP32 must send:** 16-bit PCM, mono, 16 kHz WAV — what Whisper expects natively,
so nothing is resampled server-side. Uploads under 1 KB (`MIN_AUDIO_BYTES` in `main.py`) are
rejected with a `400` *before* any Groq call is made, so a dead mic never costs an API request.

**GPS:** `/api/voice` accepts optional `lat` and `lon` form fields. Send them whenever the
Neo-6M has a fix and omit them when it doesn't — the backend handles both.

**Debugging firmware from the server console.** Every request logs the audio format it
actually received, so I2S misconfiguration is visible without opening the file:

```
[voice] received 32044 bytes, 1.0s, 16000 Hz, 1 ch, 16-bit | gps: 48.8584, 2.2945   <- correct
[voice] received 176444 bytes, 1.0s, 44100 Hz, 2 ch, 16-bit | gps: no fix           <- wrong I2S config
[voice] heard: 'Where am I right now?'
[voice] branch: reverse-geocode
[voice] reply: "You're near Avenue Gustave Eiffel, ..."
```

## Notes

- Temp audio (uploads and generated mp3s) lives in `/tmp/voice_assistant`, created on startup
  and cleaned up after each request.
- Conversation memory is opt-in: send a `session_id` and the last 6 exchanges are replayed
  to the LLM; omit it and the request is stateless as before. Sessions live in memory and
  expire after 15 minutes idle (`SESSION_TTL_SECONDS`), or immediately via `/api/reset`.
- Geocoding uses Nominatim, which rate-limits to ~1 request/second and requires the custom
  `User-Agent` set in `main.py`. If you add a contact address there, use a real one —
  Nominatim returns 403 for any User-Agent containing a placeholder like `example.com`.
- Routing uses the public OSRM demo server, which is best-effort and intermittently
  unreachable. When it is down, `/api/directions` returns a clean `502` with the reason
  rather than failing silently.
- Every outbound call maps failures to proper status codes: upstream error codes pass
  through, timeouts become `504`, unreachable hosts become `502`.
- Endpoints are sync `def`, not `async def`, on purpose — they make blocking `requests`
  calls, so FastAPI runs them in a threadpool instead of stalling the event loop. This is
  what lets one cloud instance serve overlapping requests.
- Reply audio is WAV from Groq TTS or mp3 from gTTS; the `Content-Type` tells you which,
  so don't hardcode `audio/mpeg` in the firmware.

## Endpoints

| Method | Path | Input | Output |
|---|---|---|---|
| GET | `/health` | — | `{"status", "groq_key_set", "auth_required", "tts_provider", "tts_last_used"}` |
| POST | `/api/stt` | multipart `file` | `{"text"}` |
| POST | `/api/chat` | JSON `{"message"}`, optional `session_id` | `{"reply"}` |
| POST | `/api/reset` | JSON `{"session_id"}` | `{"cleared"}` |
| POST | `/api/tts` | JSON `{"text"}` | `audio/mpeg` |
| GET | `/api/geocode` | query `place` | `{"lat", "lon", "display_name"}` |
| GET | `/api/reverse-geocode` | query `lat`, `lon` | `{"display_name", "lat", "lon"}` |
| POST | `/api/directions` | JSON `{"origin", "destination"}` | `{"distance_km", "duration_min", "origin", "destination"}` |
| POST | `/api/voice` | multipart `file`, optional `lat`/`lon`/`session_id` | audio + `X-Heard-Text` / `X-Reply-Text` headers |

`/api/voice` picks one of four branches from the transcript, logging which one it took:

| Transcript matches | Branch | LLM called? |
|---|---|---|
| "where am I", "my location", "current location", "where are we" + GPS fix | reverse-geocode → "You're near …" | no |
| same, but no `lat`/`lon` sent | "I don't have a GPS fix yet" | no |
| "navigate", "directions", "route to", "distance to" | placeholder reply (slot extraction is future work) | no |
| anything else | normal conversation | yes |

## Testing every endpoint with curl

**Health check**

```bash
curl http://localhost:8000/health
# {"status":"ok","groq_key_set":true}
```

**Speech-to-text** (any wav file with speech in it)

```bash
curl -X POST http://localhost:8000/api/stt -F "file=@test.wav"
# {"text":"What's the weather like today?"}
```

**Chat (LLM only)**

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "How tall is the Eiffel Tower?"}'
# {"reply":"The Eiffel Tower is about 330 meters tall, including its antenna."}
```

**Text-to-speech**

```bash
curl -X POST http://localhost:8000/api/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello from your wrist assistant."}' \
  --output speech.mp3
```

**Geocoding**

```bash
curl "http://localhost:8000/api/geocode?place=Bangalore"
# {"lat":12.9767936,"lon":77.590082,"display_name":"Bengaluru, Bangalore North, Bengaluru Urban, Karnataka, India"}
```

**Reverse geocoding** (what the Neo-6M's coordinates turn into)

```bash
curl "http://localhost:8000/api/reverse-geocode?lat=48.8584&lon=2.2945"
# {"display_name":"Avenue Gustave Eiffel, ... 75007, France","lat":48.8583982,"lon":2.2944933}
```

**Directions**

```bash
curl -X POST http://localhost:8000/api/directions \
  -H "Content-Type: application/json" \
  -d '{"origin": "Bangalore", "destination": "Mysore"}'
# {"distance_km":141.48,"duration_min":116.7,"origin":"Bengaluru, ...","destination":"Mysuru, ..."}
```

**Full voice pipeline** (what the ESP32 calls)

```bash
curl -X POST http://localhost:8000/api/voice \
  -F "file=@test.wav" \
  --output reply.mp3 \
  -D headers.txt

cat headers.txt   # shows X-Heard-Text and X-Reply-Text
mpg123 reply.mp3  # or any audio player
```

**Full pipeline with a GPS fix** — ask "where am I" and get a real address back:

```bash
curl -X POST http://localhost:8000/api/voice \
  -F "file=@where_am_i.wav" \
  -F "lat=48.8584" -F "lon=2.2945" \
  --output reply.mp3 -D headers.txt
# X-Reply-Text: You're near Avenue Gustave Eiffel, ... 75007, France.
```

Drop the `lat`/`lon` fields and the same audio gets "I don't have a GPS fix yet" instead.

No `test.wav` handy? Record one:

```bash
arecord -f cd -d 5 -r 16000 -c 1 test.wav   # 5 seconds, 16 kHz mono
```
