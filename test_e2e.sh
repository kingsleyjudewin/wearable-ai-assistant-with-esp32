#!/usr/bin/env bash
# End-to-end test for every backend endpoint.
#   ./test_e2e.sh                      # tests http://localhost:8000
#   BASE=http://192.168.1.42:8000 ./test_e2e.sh
#
# Generates its own spoken-question audio with gTTS, so no test.wav is needed.

set -uo pipefail

BASE="${BASE:-http://localhost:8000}"
PY="./.venv/bin/python"

# Set API_KEY when testing a deployment that has API_SECRET configured.
AUTH=()
[ -n "${API_KEY:-}" ] && AUTH=(-H "X-API-Key: $API_KEY")
c() { curl "${AUTH[@]}" "$@"; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

ok()   { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
head_() { echo; echo "=== $1 ==="; }

# --- 1. health ------------------------------------------------------------ #
head_ "GET /health"
body=$(c -s -m 10 "$BASE/health")
echo "  $body"
case "$body" in
  *'"status":"ok"'*) ok "server up" ;;
  *) bad "server not responding at $BASE"; echo; echo "Start it with: ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000"; exit 1 ;;
esac
case "$body" in
  *'"groq_key_set":true'*) ok "GROQ_API_KEY loaded" ;;
  *) bad "GROQ_API_KEY missing - put it in .env"; exit 1 ;;
esac

# --- 2. chat (LLM) -------------------------------------------------------- #
head_ "POST /api/chat"
body=$(c -s -m 40 -X POST "$BASE/api/chat" \
  -H "Content-Type: application/json" \
  -d '{"message": "In one sentence, what is the capital of France?"}')
echo "  $body"
case "$body" in
  *'"reply"'*[Pp]aris*) ok "LLM answered correctly" ;;
  *'"reply"'*) ok "LLM replied (content not asserted)" ;;
  *) bad "no reply field" ;;
esac

# --- 3. tts --------------------------------------------------------------- #
head_ "POST /api/tts"
code=$(c -s -m 30 -X POST "$BASE/api/tts" \
  -H "Content-Type: application/json" \
  -d '{"text": "Text to speech is working."}' \
  --output "$WORK/tts.mp3" -w "%{http_code}")
size=$(stat -c%s "$WORK/tts.mp3" 2>/dev/null || echo 0)
echo "  http=$code bytes=$size"
if [ "$code" = "200" ] && [ "$size" -gt 1000 ]; then ok "mp3 returned"; else bad "bad tts response"; fi

# --- 4. geocode ----------------------------------------------------------- #
head_ "GET /api/geocode"
body=$(c -s -m 25 "$BASE/api/geocode?place=Bangalore")
echo "  $body"
case "$body" in
  *'"lat"'*'"lon"'*) ok "geocoded" ;;
  *) bad "geocode failed" ;;
esac

sleep 1  # Nominatim allows ~1 request/second

# --- 4b. reverse geocode -------------------------------------------------- #
head_ "GET /api/reverse-geocode  (Eiffel Tower coords)"
body=$(c -s -m 25 "$BASE/api/reverse-geocode?lat=48.8584&lon=2.2945")
echo "  $body"
case "$body" in
  *[Ee]iffel*|*Paris*) ok "resolved to a real Paris address" ;;
  *'"display_name"'*) ok "resolved (address differs from expected)" ;;
  *) bad "reverse geocode failed" ;;
esac

sleep 1

# --- 5. directions -------------------------------------------------------- #
head_ "POST /api/directions"
body=$(c -s -m 45 -X POST "$BASE/api/directions" \
  -H "Content-Type: application/json" \
  -d '{"origin": "Bangalore", "destination": "Mysore"}')
echo "  $body"
case "$body" in
  *'"distance_km"'*'"duration_min"'*) ok "route computed" ;;
  *) bad "directions failed" ;;
esac

# --- build a spoken question for the audio tests -------------------------- #
head_ "generating spoken test audio (gTTS)"
$PY -c "
from gtts import gTTS
gTTS(text='What is the capital of France?', lang='en').save('$WORK/question.mp3')
gTTS(text='Where am I right now?', lang='en').save('$WORK/where_am_i.mp3')
" 2>/dev/null && ok "spoken questions generated" || { bad "could not generate test audio"; exit 1; }

# --- 6. stt --------------------------------------------------------------- #
head_ "POST /api/stt"
body=$(c -s -m 60 -X POST "$BASE/api/stt" -F "file=@$WORK/question.mp3")
echo "  $body"
case "$body" in
  *[Cc]apital*[Ff]rance*) ok "transcribed accurately" ;;
  *'"text"'*) ok "transcribed (text differs from expected)" ;;
  *) bad "stt failed" ;;
esac

# --- 7. voice (full pipeline) --------------------------------------------- #
head_ "POST /api/voice  [MAIN PIPELINE]"
code=$(c -s -m 90 -X POST "$BASE/api/voice" \
  -F "file=@$WORK/question.mp3" \
  --output "$WORK/reply.mp3" -D "$WORK/headers.txt" -w "%{http_code}")
size=$(stat -c%s "$WORK/reply.mp3" 2>/dev/null || echo 0)
heard=$(grep -i '^x-heard-text:' "$WORK/headers.txt" | cut -d: -f2- | tr -d '\r')
reply=$(grep -i '^x-reply-text:' "$WORK/headers.txt" | cut -d: -f2- | tr -d '\r')
echo "  http=$code bytes=$size"
echo "  heard:$heard"
echo "  reply:$reply"
[ "$code" = "200" ] && ok "200 OK" || bad "http $code"
[ "$size" -gt 1000 ] && ok "playable mp3 returned" || bad "mp3 too small"
[ -n "$heard" ] && ok "X-Heard-Text header set" || bad "X-Heard-Text missing"
[ -n "$reply" ] && ok "X-Reply-Text header set" || bad "X-Reply-Text missing"

# --- 7b. voice + GPS fix (reverse-geocode branch) ------------------------- #
head_ "POST /api/voice + lat/lon  [GPS BRANCH]"
code=$(c -s -m 90 -X POST "$BASE/api/voice" \
  -F "file=@$WORK/where_am_i.mp3" -F "lat=48.8584" -F "lon=2.2945" \
  --output "$WORK/gps_reply.mp3" -D "$WORK/gps_headers.txt" -w "%{http_code}")
reply=$(grep -i '^x-reply-text:' "$WORK/gps_headers.txt" | cut -d: -f2- | tr -d '\r')
echo "  http=$code"
echo "  reply:$reply"
case "$reply" in
  *"You're near"*|*"You\\'re near"*) ok "answered from GPS coordinates" ;;
  *) bad "did not take the reverse-geocode branch" ;;
esac

# --- 7c. voice without GPS fix -------------------------------------------- #
head_ "POST /api/voice, no lat/lon  [NO-FIX BRANCH]"
code=$(c -s -m 90 -X POST "$BASE/api/voice" \
  -F "file=@$WORK/where_am_i.mp3" \
  --output "$WORK/nofix_reply.mp3" -D "$WORK/nofix_headers.txt" -w "%{http_code}")
reply=$(grep -i '^x-reply-text:' "$WORK/nofix_headers.txt" | cut -d: -f2- | tr -d '\r')
echo "  http=$code"
echo "  reply:$reply"
[ "$code" = "200" ] && ok "still returns audio, no error" || bad "http $code"
case "$reply" in
  *GPS*) ok "explains there is no GPS fix" ;;
  *) bad "expected a no-GPS-fix reply" ;;
esac

# --- 8. error handling ---------------------------------------------------- #
head_ "error handling"
body=$(c -s -m 30 -X POST "$BASE/api/voice" -F "file=@/dev/null;filename=empty.wav" -w "\n%{http_code}")
code=$(echo "$body" | tail -1)
echo "  0-byte upload -> http $code"
echo "  $(echo "$body" | head -1)"
[ "$code" = "400" ] && ok "rejects 0-byte upload" || bad "expected 400, got $code"
case "$body" in
  *"no audio received"*) ok "rejected before any Groq call" ;;
  *) bad "rejection message unclear" ;;
esac

printf 'x%.0s' $(seq 1 200) > "$WORK/tiny.wav"
code=$(c -s -m 30 -X POST "$BASE/api/voice" -F "file=@$WORK/tiny.wav" -o /dev/null -w "%{http_code}")
echo "  200-byte upload -> http $code"
[ "$code" = "400" ] && ok "rejects undersized recording" || bad "expected 400, got $code"

code=$(c -s -m 25 "$BASE/api/geocode?place=zzzzqqqxxnotarealplace" -o /dev/null -w "%{http_code}")
echo "  bogus place -> http $code"
[ "$code" = "404" ] && ok "404 for unknown place" || bad "expected 404, got $code"

# --- temp file cleanup ---------------------------------------------------- #
head_ "temp file cleanup"
left=$(ls -1 /tmp/voice_assistant 2>/dev/null | wc -l)
echo "  files left in /tmp/voice_assistant: $left"
[ "$left" -eq 0 ] && ok "no temp files leaked" || bad "$left temp files left behind"

# --- summary -------------------------------------------------------------- #
echo
echo "=========================================="
echo "  PASSED: $PASS    FAILED: $FAIL"
echo "=========================================="
[ "$FAIL" -eq 0 ]
