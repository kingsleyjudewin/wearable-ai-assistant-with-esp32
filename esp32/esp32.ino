/*
 * Wrist voice assistant - ESP32 firmware
 * ======================================
 *
 * Press the button to start a conversation. It listens, sends your speech to the
 * backend, plays the spoken reply, then listens again - continuously. Press the
 * button again to end the conversation.
 *
 * HARDWARE / WIRING
 * -----------------
 *   INMP441 microphone (I2S input)        MAX98357A amplifier (I2S output)
 *     VDD  -> 3V3                           VIN  -> VIN (5V)
 *     GND  -> GND                           GND  -> GND
 *     L/R  -> GND      (selects left)       DIN  -> GPIO 13
 *     WS   -> GPIO 25                       BCLK -> GPIO 27
 *     SCK  -> GPIO 26                       LRC  -> GPIO 14
 *     SD   -> GPIO 34  (input-only pin)     (leave GAIN and SD pins unconnected)
 *                                           speaker + / - -> the two screw terminals
 *
 *   Neo-6M GPS                            Tactile button (4-pin)
 *     VCC  -> VIN (5V)                      one leg  -> GPIO 4
 *     GND  -> GND                           other leg-> GND
 *     TX   -> GPIO 16   (ESP32 RX2)         (the other two legs are the same pair)
 *     RX   -> GPIO 17   (ESP32 TX2)
 *
 *   Capacitors (they stop the amp browning out the ESP32 on loud audio):
 *     220uF electrolytic across the MAX98357A VIN and GND - watch the polarity,
 *       the striped leg is negative and goes to GND.
 *     0.1uF ceramic across VIN/GND of the mic and the amp, as close as possible.
 *
 *   Power the board from the 5V 2A adapter, not a laptop USB port. WiFi transmit
 *   plus audio draws more than most laptop ports will give you.
 *
 * ARDUINO IDE SETUP
 * -----------------
 *   1. File > Preferences > Additional board manager URLs:
 *        https://espressif.github.io/arduino-esp32/package_esp32_index.json
 *   2. Tools > Board > Boards Manager > install "esp32 by Espressif Systems".
 *   3. Tools > Board > ESP32 Arduino > "ESP32 Dev Module".
 *   4. Tools > Partition Scheme > "Huge APP (3MB No OTA/1MB SPIFFS)".
 *   5. Fill in WIFI_SSID / WIFI_PASSWORD below, then Upload.
 *   6. Tools > Serial Monitor at 115200 baud to watch what it is doing.
 *
 * No extra libraries are needed - everything used here ships with the ESP32 core.
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <driver/i2s.h>

// --------------------------------------------------------------------------- //
// Configuration - edit these
// --------------------------------------------------------------------------- //

// Note the trailing space - it is genuinely part of the hotspot's name. WiFi
// association matches the SSID byte for byte, so dropping it fails to connect
// even though every UI displays the name without it.
const char *WIFI_SSID     = "Judewin ";
const char *WIFI_PASSWORD = "123456789";

const char *BACKEND_HOST = "voice-assistant-backend-a5qi.onrender.com";
const int   BACKEND_PORT = 443;

// Only needed if you set API_SECRET on the backend. Leave "" for no auth.
const char *API_KEY = "";

// --------------------------------------------------------------------------- //
// Pins
// --------------------------------------------------------------------------- //

#define MIC_WS    25
#define MIC_SCK   26
#define MIC_SD    34

#define AMP_LRC   14
#define AMP_BCLK  27
#define AMP_DIN   13

#define GPS_RX    16   // ESP32 receives on this pin, wire it to the GPS TX
#define GPS_TX    17

#define BUTTON    4

#define I2S_MIC   I2S_NUM_0
#define I2S_AMP   I2S_NUM_1

// --------------------------------------------------------------------------- //
// Audio settings
// --------------------------------------------------------------------------- //

#define SAMPLE_RATE        16000   // what the backend expects - do not change
// 3 s at 16 kHz 16-bit = 96 KB of heap. Raising this to 4 works on some boards but
// leaves little room for the TLS handshake; if uploads start failing with a memory
// error, come back here first.
#define MAX_RECORD_SECONDS 3
#define CHUNK_SAMPLES      1600    // 100 ms of audio per voice-activity check

// The INMP441 gives 24-bit samples inside 32-bit slots and is fairly quiet, so
// shift down and then apply gain. Raise MIC_GAIN if the transcript comes back
// empty, lower it if speech sounds clipped and distorted.
#define MIC_SHIFT  14
#define MIC_GAIN   4

// Playback volume, 0-100. The MAX98357A can push over 3 W but the speaker in this
// build is rated 0.5 W, so full-scale audio would distort and eventually cook it.
// Raise this if the reply is too quiet; drop it if the speaker buzzes or rattles.
#define PLAYBACK_VOLUME 60

// Voice activity detection, same approach as talk.py on the laptop.
#define CALIBRATE_CHUNKS   8     // ~0.8 s of room tone to learn the noise floor
#define SILENCE_MULTIPLIER 2.5f  // speech must be this much louder than the room
#define MIN_THRESHOLD      250.0f
#define SILENCE_HOLD_MS    1200  // end the turn after this much quiet
#define START_TIMEOUT_MS   12000 // how long to wait for someone to start talking

const size_t MAX_SAMPLES = (size_t)SAMPLE_RATE * MAX_RECORD_SECONDS;

// Allocated once from the heap in setup(), never freed - so it behaves like a
// static buffer without being one. A static array this size does not fit: the
// linker must place it in DRAM alongside the WiFi and TLS stacks, and 128 KB
// overflows that segment. The heap has room only because the TLS buffers are
// not claimed until a request is actually in flight.
static int16_t *audioBuffer = nullptr;
static size_t  recordedSamples = 0;

// --------------------------------------------------------------------------- //
// State
// --------------------------------------------------------------------------- //

bool   inConversation = false;
String sessionId      = "";

double gpsLat = 0, gpsLon = 0;
bool   gpsHasFix = false;

// --------------------------------------------------------------------------- //
// Button - debounced, edge triggered, and safe to poll inside tight loops
// --------------------------------------------------------------------------- //

bool          lastButtonLevel = HIGH;
unsigned long lastButtonChange = 0;

bool buttonPressed() {
  bool level = digitalRead(BUTTON);
  unsigned long now = millis();

  if (level != lastButtonLevel && now - lastButtonChange > 50) {
    lastButtonChange = now;
    lastButtonLevel  = level;
    if (level == LOW) return true;   // pressed (pin pulled to ground)
  }
  return false;
}

// --------------------------------------------------------------------------- //
// I2S
// --------------------------------------------------------------------------- //

void setupMicrophone() {
  i2s_config_t config = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate          = SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 8,
    .dma_buf_len          = 256,
    .use_apll             = false,
    .tx_desc_auto_clear   = false,
    .fixed_mclk           = 0
  };
  i2s_pin_config_t pins = {
    // Neither the INMP441 nor the MAX98357A needs a master clock. Leave this out
    // and it defaults to GPIO0, which is already driven - the driver then fails
    // with "mclk config failed" and no audio ever moves.
    .mck_io_num   = I2S_PIN_NO_CHANGE,
    .bck_io_num   = MIC_SCK,
    .ws_io_num    = MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num  = MIC_SD
  };
  i2s_driver_install(I2S_MIC, &config, 0, NULL);
  i2s_set_pin(I2S_MIC, &pins);
}

void setupAmplifier() {
  i2s_config_t config = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate          = 24000,   // corrected per reply from the WAV header
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 8,
    .dma_buf_len          = 256,
    .use_apll             = false,
    .tx_desc_auto_clear   = true,
    .fixed_mclk           = 0
  };
  i2s_pin_config_t pins = {
    .mck_io_num   = I2S_PIN_NO_CHANGE,
    .bck_io_num   = AMP_BCLK,
    .ws_io_num    = AMP_LRC,
    .data_out_num = AMP_DIN,
    .data_in_num  = I2S_PIN_NO_CHANGE
  };
  i2s_driver_install(I2S_AMP, &config, 0, NULL);
  i2s_set_pin(I2S_AMP, &pins);
  i2s_zero_dma_buffer(I2S_AMP);
}

// --------------------------------------------------------------------------- //
// Recording, with voice activity detection
// --------------------------------------------------------------------------- //

// Reads one chunk into the buffer and returns its loudness.
float readChunk(int16_t *dest, size_t samples, size_t *written) {
  static int32_t raw[CHUNK_SAMPLES];
  size_t bytesRead = 0;

  i2s_read(I2S_MIC, raw, samples * sizeof(int32_t), &bytesRead, portMAX_DELAY);
  size_t got = bytesRead / sizeof(int32_t);

  double sumSquares = 0;
  for (size_t i = 0; i < got; i++) {
    int32_t value = (raw[i] >> MIC_SHIFT) * MIC_GAIN;
    if (value >  32767) value =  32767;
    if (value < -32768) value = -32768;
    dest[i] = (int16_t)value;
    sumSquares += (double)value * value;
  }

  *written = got;
  return got ? sqrt(sumSquares / got) : 0.0f;
}

// Records one utterance. Returns:
//   1  captured speech
//   0  nobody spoke before the timeout
//  -1  the button was pressed, meaning end the conversation
int recordUtterance() {
  recordedSamples = 0;

  float noiseFloor = 0;
  float threshold  = MIN_THRESHOLD;
  bool  speaking   = false;
  int   chunkIndex = 0;
  unsigned long silenceStart = 0;
  unsigned long listenStart  = millis();

  static int16_t chunk[CHUNK_SAMPLES];

  while (true) {
    if (buttonPressed()) return -1;

    size_t got = 0;
    float level = readChunk(chunk, CHUNK_SAMPLES, &got);
    chunkIndex++;

    // The first chunks measure the room rather than the speaker.
    if (chunkIndex <= CALIBRATE_CHUNKS) {
      if (level > noiseFloor) noiseFloor = level;
      continue;
    }
    if (chunkIndex == CALIBRATE_CHUNKS + 1) {
      threshold = max(noiseFloor * SILENCE_MULTIPLIER, MIN_THRESHOLD);
      Serial.printf("listening (noise floor %.0f, threshold %.0f)\n", noiseFloor, threshold);
      listenStart = millis();
    }

    if (!speaking) {
      if (level > threshold) {
        speaking = true;
        Serial.println("hearing you...");
      } else if (millis() - listenStart > START_TIMEOUT_MS) {
        return 0;
      } else {
        continue;
      }
    }

    // Keep the audio, stopping if the buffer fills.
    size_t room = MAX_SAMPLES - recordedSamples;
    size_t take = got < room ? got : room;
    memcpy(&audioBuffer[recordedSamples], chunk, take * sizeof(int16_t));
    recordedSamples += take;

    if (level <= threshold) {
      if (silenceStart == 0) silenceStart = millis();
      if (millis() - silenceStart > SILENCE_HOLD_MS) break;
    } else {
      silenceStart = 0;
    }

    if (recordedSamples >= MAX_SAMPLES) {
      Serial.println("(hit the recording limit)");
      break;
    }
  }

  Serial.printf("recorded %.1f s\n", (float)recordedSamples / SAMPLE_RATE);
  return 1;
}

// --------------------------------------------------------------------------- //
// WAV header - the backend rejects uploads that are not real WAV files
// --------------------------------------------------------------------------- //

void buildWavHeader(uint8_t *header, size_t dataBytes) {
  uint32_t chunkSize = 36 + dataBytes;
  uint32_t byteRate  = SAMPLE_RATE * 2;

  memcpy(header,      "RIFF", 4);
  memcpy(header + 4,  &chunkSize, 4);
  memcpy(header + 8,  "WAVEfmt ", 8);
  uint32_t sub1 = 16;   memcpy(header + 16, &sub1, 4);
  uint16_t fmt  = 1;    memcpy(header + 20, &fmt, 2);    // PCM
  uint16_t ch   = 1;    memcpy(header + 22, &ch, 2);     // mono
  uint32_t rate = SAMPLE_RATE; memcpy(header + 24, &rate, 4);
  memcpy(header + 28, &byteRate, 4);
  uint16_t align = 2;   memcpy(header + 32, &align, 2);
  uint16_t bits  = 16;  memcpy(header + 34, &bits, 2);
  memcpy(header + 36, "data", 4);
  uint32_t dataLen = dataBytes; memcpy(header + 40, &dataLen, 4);
}

// --------------------------------------------------------------------------- //
// GPS - minimal NMEA parsing, no library required
// --------------------------------------------------------------------------- //

// NMEA gives ddmm.mmmm; degrees are the leading 2 or 3 digits, the rest is minutes.
double nmeaToDegrees(const String &value, int degreeDigits) {
  if (value.length() < degreeDigits + 2) return 0;
  double degrees = value.substring(0, degreeDigits).toDouble();
  double minutes = value.substring(degreeDigits).toDouble();
  return degrees + minutes / 60.0;
}

String nmeaField(const String &sentence, int index) {
  int start = 0, field = 0;
  while (field < index) {
    start = sentence.indexOf(',', start);
    if (start < 0) return "";
    start++;
    field++;
  }
  int end = sentence.indexOf(',', start);
  return end < 0 ? sentence.substring(start) : sentence.substring(start, end);
}

// Call often - it drains whatever the GPS has sent since last time.
void updateGps() {
  static String line = "";

  while (Serial2.available()) {
    char c = Serial2.read();
    if (c == '\n') {
      if (line.startsWith("$GPGGA") || line.startsWith("$GNGGA")) {
        String quality = nmeaField(line, 6);
        if (quality.length() && quality.toInt() > 0) {
          double lat = nmeaToDegrees(nmeaField(line, 2), 2);
          double lon = nmeaToDegrees(nmeaField(line, 4), 3);
          if (nmeaField(line, 3) == "S") lat = -lat;
          if (nmeaField(line, 5) == "W") lon = -lon;
          if (lat != 0 || lon != 0) {
            gpsLat = lat;
            gpsLon = lon;
            if (!gpsHasFix) Serial.printf("GPS fix: %.6f, %.6f\n", gpsLat, gpsLon);
            gpsHasFix = true;
          }
        }
      }
      line = "";
    } else if (c != '\r') {
      if (line.length() < 120) line += c;
    }
  }
}

// --------------------------------------------------------------------------- //
// Networking
// --------------------------------------------------------------------------- //

// Translate the numeric status into something you can act on, instead of an
// endless row of dots that never says what went wrong.
const char *wifiStatusName(int status) {
  switch (status) {
    case WL_NO_SSID_AVAIL: return "SSID not found - wrong name, or it is a 5 GHz network";
    case WL_CONNECT_FAILED: return "connect failed - usually a wrong password";
    case WL_CONNECTION_LOST: return "connection lost";
    case WL_DISCONNECTED: return "disconnected - still negotiating";
    case WL_IDLE_STATUS: return "idle";
    default: return "unknown";
  }
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true);
  delay(200);

  for (int attempt = 1; ; attempt++) {
    Serial.printf("connecting to %s (attempt %d)", WIFI_SSID, attempt);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
      delay(500);
      Serial.print(".");
    }
    if (WiFi.status() == WL_CONNECTED) break;

    int status = WiFi.status();
    Serial.printf("\n  failed: status=%d (%s)\n", status, wifiStatusName(status));

    // Show what the ESP32 itself can see - the laptop's view of the airwaves is
    // not the board's, and only the board's opinion matters here. The radio has
    // to be idle first, or the scan just returns a negative error code.
    WiFi.disconnect(true);
    delay(500);

    Serial.println("  networks visible to the ESP32:");
    int found = WiFi.scanNetworks();
    if (found < 0) {
      Serial.printf("    scan failed (%d)\n", found);
    } else if (found == 0) {
      Serial.println("    (none - check the antenna and the power supply)");
    } else {
      for (int i = 0; i < found && i < 12; i++) {
        Serial.printf("    %-28s ch%-3d %4d dBm %s\n",
                      WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i),
                      WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "secured");
      }
    }
    WiFi.scanDelete();
    delay(1000);
  }

  Serial.printf("\nconnected, IP %s, %d dBm\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

String makeSessionId() {
  char buffer[17];
  for (int i = 0; i < 16; i++) buffer[i] = "0123456789abcdef"[random(16)];
  buffer[16] = 0;
  return String(buffer);
}

// Tells the backend to forget this conversation.
void resetSession() {
  if (sessionId == "") return;

  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(15000);
  if (!client.connect(BACKEND_HOST, BACKEND_PORT)) return;

  String body = "{\"session_id\":\"" + sessionId + "\"}";
  client.printf("POST /api/reset HTTP/1.1\r\nHost: %s\r\n", BACKEND_HOST);
  if (strlen(API_KEY)) client.printf("X-API-Key: %s\r\n", API_KEY);
  client.printf("Content-Type: application/json\r\nContent-Length: %d\r\n"
                "Connection: close\r\n\r\n%s", body.length(), body.c_str());
  client.stop();
}

/*
 * Uploads the recording to /api/voice and plays the spoken reply as it arrives.
 *
 * The reply is streamed straight into the amplifier rather than buffered - a
 * ten second answer is far larger than the RAM on this board.
 */
bool sendAndPlay() {
  WiFiClientSecure client;
  client.setInsecure();          // skip certificate validation, fine for a prototype
  client.setTimeout(90000);      // a sleeping free-tier instance can take ~50 s to wake

  Serial.println("uploading...");
  if (!client.connect(BACKEND_HOST, BACKEND_PORT)) {
    Serial.println("could not reach the backend");
    return false;
  }

  const char *boundary = "----esp32wearable";
  size_t dataBytes = recordedSamples * sizeof(int16_t);

  String head = String("--") + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"file\"; filename=\"speech.wav\"\r\n"
    "Content-Type: audio/wav\r\n\r\n";

  String tail = String("\r\n--") + boundary + "\r\n"
    "Content-Disposition: form-data; name=\"session_id\"\r\n\r\n" + sessionId + "\r\n";

  if (gpsHasFix) {
    tail += String("--") + boundary + "\r\n"
      "Content-Disposition: form-data; name=\"lat\"\r\n\r\n" + String(gpsLat, 6) + "\r\n";
    tail += String("--") + boundary + "\r\n"
      "Content-Disposition: form-data; name=\"lon\"\r\n\r\n" + String(gpsLon, 6) + "\r\n";
  }
  tail += String("--") + boundary + "--\r\n";

  size_t contentLength = head.length() + 44 + dataBytes + tail.length();

  client.printf("POST /api/voice HTTP/1.1\r\nHost: %s\r\n", BACKEND_HOST);
  if (strlen(API_KEY)) client.printf("X-API-Key: %s\r\n", API_KEY);
  client.printf("Content-Type: multipart/form-data; boundary=%s\r\n"
                "Content-Length: %u\r\nConnection: close\r\n\r\n",
                boundary, (unsigned)contentLength);

  client.print(head);

  uint8_t wavHeader[44];
  buildWavHeader(wavHeader, dataBytes);
  client.write(wavHeader, 44);

  // Send the audio in slices; one huge write can overrun the TLS buffer.
  const size_t sliceBytes = 1024;
  uint8_t *raw = (uint8_t *)audioBuffer;
  for (size_t sent = 0; sent < dataBytes; sent += sliceBytes) {
    size_t n = min(sliceBytes, dataBytes - sent);
    if (client.write(raw + sent, n) != n) {
      Serial.println("upload stalled");
      client.stop();
      return false;
    }
  }
  client.print(tail);

  // ---- response ----
  Serial.println("waiting for the reply...");
  unsigned long deadline = millis() + 90000;
  while (client.available() == 0) {
    if (millis() > deadline) {
      Serial.println("backend timed out");
      client.stop();
      return false;
    }
    if (!client.connected()) {
      Serial.println("connection dropped");
      client.stop();
      return false;
    }
    delay(10);
  }

  String status = client.readStringUntil('\n');
  if (status.indexOf("200") < 0) {
    Serial.printf("backend said: %s\n", status.c_str());
    while (client.available()) Serial.write(client.read());
    client.stop();
    return false;
  }

  // Headers carry the transcript and the reply text - invaluable when debugging.
  long contentRemaining = -1;
  while (client.connected() || client.available()) {
    String line = client.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) break;

    String lower = line;
    lower.toLowerCase();
    if (lower.startsWith("content-length:")) {
      contentRemaining = line.substring(15).toInt();
    } else if (lower.startsWith("x-heard-text:")) {
      Serial.printf("you said : %s\n", line.substring(13).c_str());
    } else if (lower.startsWith("x-reply-text:")) {
      Serial.printf("assistant: %s\n", line.substring(13).c_str());
    }
  }

  // ---- play the WAV as it streams in ----
  // Read the 44 byte header first so the amplifier can match the sample rate.
  uint8_t header[44];
  size_t headerRead = 0;
  while (headerRead < 44 && (client.connected() || client.available())) {
    if (client.available()) headerRead += client.read(header + headerRead, 44 - headerRead);
    else delay(1);
  }

  uint32_t replyRate = 24000;
  if (memcmp(header, "RIFF", 4) == 0) {
    memcpy(&replyRate, header + 24, 4);
    if (replyRate < 8000 || replyRate > 48000) replyRate = 24000;
  }
  i2s_set_clk(I2S_AMP, replyRate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_STEREO);
  Serial.printf("playing reply at %u Hz\n", replyRate);

  if (contentRemaining > 44) contentRemaining -= 44;

  static uint8_t  netBuffer[512];
  static int16_t  stereo[512];     // mono duplicated to both channels
  bool interrupted = false;

  while ((client.connected() || client.available()) && contentRemaining != 0) {
    if (buttonPressed()) {          // let the button cut a long answer short
      interrupted = true;
      break;
    }
    if (!client.available()) {
      delay(1);
      continue;
    }

    int n = client.read(netBuffer, sizeof(netBuffer));
    if (n <= 0) continue;
    if (contentRemaining > 0) contentRemaining -= n;

    int samples = n / 2;
    int16_t *mono = (int16_t *)netBuffer;
    for (int i = 0; i < samples; i++) {
      int32_t value = ((int32_t)mono[i] * PLAYBACK_VOLUME) / 100;
      stereo[i * 2]     = (int16_t)value;   // same sample to both channels, so the
      stereo[i * 2 + 1] = (int16_t)value;   // amp's (L+R)/2 mode plays at full level
    }

    size_t written = 0;
    i2s_write(I2S_AMP, stereo, samples * 2 * sizeof(int16_t), &written, portMAX_DELAY);
  }

  i2s_zero_dma_buffer(I2S_AMP);
  client.stop();

  if (interrupted) {
    Serial.println("(playback stopped by button)");
    return false;   // treated as "end the conversation"
  }
  return true;
}

// --------------------------------------------------------------------------- //
// Setup and main loop
// --------------------------------------------------------------------------- //

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n\nwrist assistant starting");

  pinMode(BUTTON, INPUT_PULLUP);
  Serial2.begin(9600, SERIAL_8N1, GPS_RX, GPS_TX);

  audioBuffer = (int16_t *)malloc(MAX_SAMPLES * sizeof(int16_t));
  if (!audioBuffer) {
    Serial.println("FATAL: could not allocate the audio buffer - lower MAX_RECORD_SECONDS");
    while (true) delay(1000);
  }
  Serial.printf("audio buffer: %u KB, free heap after: %u KB\n",
                (unsigned)(MAX_SAMPLES * sizeof(int16_t) / 1024),
                (unsigned)(ESP.getFreeHeap() / 1024));

  setupMicrophone();
  setupAmplifier();
  connectWifi();
  randomSeed(esp_random());

  Serial.println("ready - press the button to start talking");
}

void loop() {
  updateGps();   // keep the fix fresh even while idle

  if (!inConversation) {
    if (buttonPressed()) {
      sessionId = makeSessionId();
      inConversation = true;
      Serial.println("\n--- conversation started (press again to stop) ---");
    }
    delay(20);
    return;
  }

  int result = recordUtterance();

  if (result == -1) {                     // button pressed - end the conversation
    Serial.println("--- conversation ended ---\n");
    resetSession();
    inConversation = false;
    sessionId = "";
    return;
  }

  if (result == 0) {                      // nobody spoke; keep waiting
    Serial.println("(still listening - press the button to stop)");
    return;
  }

  updateGps();
  if (!sendAndPlay()) {
    // A failed request should not silently kill the conversation, but a button
    // press during playback should.
    if (digitalRead(BUTTON) == LOW) {
      resetSession();
      inConversation = false;
      sessionId = "";
      Serial.println("--- conversation ended ---\n");
    }
  }
}
