// MUNIM edge mic — REFERENCE SKETCH (ESP32-S3 + I2S MEMS mic, e.g. INMP441). NOT FIELD-TESTED.
//
// What this is: wake-word-gated, short-clip uploader. It never streams continuously.
// What this is NOT: a trained wake word. `wakeDetected()` below is a stub — drop in a model
// (e.g. an Edge Impulse / ESP-SR keyword model trained on "Munim ji") before use. Until then the
// button on WAKE_PIN is the wake trigger, which is also the honest fallback for noisy shops.
//
// Protocol (see docs/EDGE.md): POST <server>/v1/edge/clip?wake_ms=<ms since wake>
//   headers  X-Edge-Device, X-Edge-Ts (unix s), X-Edge-Sig = HMAC-SHA256(secret, "<dev>|<ts>|<sha256(body)>")
//   body     16 kHz mono 16-bit WAV, <= 12 s. The server drops anything without the wake word in the transcript.
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <driver/i2s.h>
#include <mbedtls/md.h>
#include <mbedtls/sha256.h>
#include <time.h>

// ---- configure (device id/secret come from Telegram: /edge provision) ----
static const char *WIFI_SSID = "SHOP_WIFI";
static const char *WIFI_PASS = "CHANGE_ME";
static const char *SERVER = "https://your-munim-host/v1/edge/clip";
static const char *DEVICE_ID = "edg_xxxxxxxx";
static const char *SECRET_HEX = "PASTE_SECRET_FROM_TELEGRAM";   // store in NVS/flash encryption in production

static const int WAKE_PIN = 0;          // push-to-talk fallback (BOOT button on most dev boards)
static const int MUTE_PIN = 4;          // hardware mute switch: LOW = mic powered logically off, nothing is captured
static const int SAMPLE_RATE = 16000;
static const int MAX_SECONDS = 8;       // server hard limit is 12 s
static const int CLIP_SAMPLES = SAMPLE_RATE * MAX_SECONDS;
static int16_t *clip;                   // PSRAM buffer

static bool wakeDetected() {            // STUB: replace with a real keyword-spotting model
  return digitalRead(WAKE_PIN) == LOW;
}

static void i2sBegin() {
  i2s_config_t cfg = {.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX), .sample_rate = SAMPLE_RATE,
                      .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT, .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
                      .communication_format = I2S_COMM_FORMAT_STAND_I2S, .intr_alloc_flags = 0, .dma_buf_count = 8, .dma_buf_len = 256};
  i2s_pin_config_t pins = {.bck_io_num = 14, .ws_io_num = 15, .data_out_num = I2S_PIN_NO_CHANGE, .data_in_num = 32};
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
}

static int captureClip() {              // ends early after ~1 s of silence; returns samples
  int n = 0, quiet = 0; int32_t raw[256];
  while (n < CLIP_SAMPLES && digitalRead(MUTE_PIN) == HIGH) {
    size_t got = 0; i2s_read(I2S_NUM_0, raw, sizeof(raw), &got, portMAX_DELAY);
    long peak = 0;
    for (size_t i = 0; i < got / 4 && n < CLIP_SAMPLES; i++) { int16_t s = raw[i] >> 14; clip[n++] = s; if (abs(s) > peak) peak = abs(s); }
    quiet = (peak < 400) ? quiet + 1 : 0;
    if (n > SAMPLE_RATE && quiet > 60) break;
  }
  return n;
}

static String hex(const uint8_t *b, size_t n) { String s; char t[3]; for (size_t i = 0; i < n; i++) { sprintf(t, "%02x", b[i]); s += t; } return s; }

static void hexToBytes(const char *h, uint8_t *out, size_t n) { for (size_t i = 0; i < n; i++) { char t[3] = {h[2 * i], h[2 * i + 1], 0}; out[i] = strtoul(t, NULL, 16); } }

static void wavHeader(uint8_t *h, uint32_t bytes) {
  memcpy(h, "RIFF", 4); uint32_t v = 36 + bytes; memcpy(h + 4, &v, 4); memcpy(h + 8, "WAVEfmt ", 8);
  v = 16; memcpy(h + 16, &v, 4); uint16_t a = 1, ch = 1, bits = 16, blk = 2; memcpy(h + 20, &a, 2); memcpy(h + 22, &ch, 2);
  v = SAMPLE_RATE; memcpy(h + 24, &v, 4); v = SAMPLE_RATE * 2; memcpy(h + 28, &v, 4); memcpy(h + 32, &blk, 2); memcpy(h + 34, &bits, 2);
  memcpy(h + 36, "data", 4); memcpy(h + 40, &bytes, 4);
}

static void upload(int samples, uint32_t wakeMs) {
  uint32_t bytes = samples * 2; size_t total = 44 + bytes;
  uint8_t *body = (uint8_t *)ps_malloc(total); if (!body) return;
  wavHeader(body, bytes); memcpy(body + 44, clip, bytes);
  uint8_t digest[32]; mbedtls_sha256(body, total, digest, 0);
  time_t ts = time(NULL);
  String msg = String(DEVICE_ID) + "|" + String((unsigned long)ts) + "|" + hex(digest, 32);
  uint8_t key[32], mac[32]; hexToBytes(SECRET_HEX, key, 32);
  mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), key, 32, (const uint8_t *)msg.c_str(), msg.length(), mac);
  WiFiClientSecure tls; tls.setInsecure();   // TODO(prod): pin the server CA — setInsecure() is for bench testing only
  HTTPClient http; http.begin(tls, String(SERVER) + "?wake_ms=" + wakeMs);
  http.addHeader("X-Edge-Device", DEVICE_ID); http.addHeader("X-Edge-Ts", String((unsigned long)ts)); http.addHeader("X-Edge-Sig", hex(mac, 32));
  http.POST(body, total); http.end(); free(body);   // audio is dropped from RAM immediately; nothing is stored on the device
}

void setup() {
  pinMode(WAKE_PIN, INPUT_PULLUP); pinMode(MUTE_PIN, INPUT_PULLUP);
  clip = (int16_t *)ps_malloc(CLIP_SAMPLES * 2);
  WiFi.begin(WIFI_SSID, WIFI_PASS); while (WiFi.status() != WL_CONNECTED) delay(300);
  configTime(0, 0, "pool.ntp.org"); while (time(NULL) < 1700000000) delay(200);   // signatures need correct time (±60 s)
  i2sBegin();
}

void loop() {
  if (digitalRead(MUTE_PIN) == LOW) { delay(200); return; }     // muted: no capture, no upload
  if (!wakeDetected()) { delay(20); return; }
  uint32_t t0 = millis(); int n = captureClip();
  if (n > SAMPLE_RATE / 2) upload(n, millis() - t0);
  delay(1500);
}
