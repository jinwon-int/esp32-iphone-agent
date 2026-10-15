// HID bridge: an ESP32-S3 that shows up as a USB mouse + keyboard and takes
// line commands over TCP (Wi-Fi). It holds no logic; the server decides.
//
// Protocol (one command per line, the board answers "ok" or "err <why>"):
//   ping | home | move <dx> <dy> | press | release | click
//   scroll <v> <h> | type <ascii text> | key <combo, e.g. cmd+h, enter>
//   at <x> <y>   (ABSOLUTE_POINTER builds only: put the pointer at x, y in 0..32767 across the screen)
//
// ABSOLUTE_POINTER=1 builds an absolute pointer (like a tablet) instead of a relative mouse (#14): the server
// then sends screen positions and needs no calibration. In that build `home` goes to the top-left corner and
// `move` shifts the last position by dx, dy absolute units, so older callers (e.g. the keep-awake nudge) still work.
//
// Before any command, board and server prove to each other that they know DEVICE_KEY (HMAC-SHA256 over fresh
// nonces), so nothing else on the network can pose as the server and drive the phone:
//   board  -> hello <reset reason> <uptime ms> <board nonce>
//   server -> challenge <server nonce> <HMAC(key, "server:<board nonce>:<server nonce>")>
//   board  -> auth <HMAC(key, "board:<server nonce>:<board nonce>")>
//   server -> ok
// Then every line carries a tag bound to the session and a sequence number n = 0, 1, 2, …, so a relayed handshake
// is useless to an attacker: session = HMAC(key, "session:<board nonce>:<server nonce>"),
//   server -> <command> <HMAC(session, "cmd:<n>:<command>")[:32]>
//   board  -> <reply> <HMAC(session, "reply:<n>:<reply>")[:32]>

#include <WiFi.h>
#include <esp_random.h>
#include <esp_task_wdt.h>
#include <mbedtls/md.h>
#include "USB.h"
#include "USBHIDMouse.h"
#include "USBHIDKeyboard.h"
#include "tusb.h"
#include "secrets.h"  // WIFI_SSID, WIFI_PASS, SERVER_HOST, SERVER_PORT, DEVICE_KEY

#ifndef DEVICE_KEY
#error "secrets.h must define DEVICE_KEY (the same value as DEVICE_KEY in server/.env)"
#endif
static_assert(sizeof(DEVICE_KEY) - 1 >= 16, "DEVICE_KEY must be at least 16 characters");

const int STEP = 10;      // max pointer delta per report while moving
const int STEP_MS = 8;    // report interval; constant pace keeps iOS acceleration predictable
const int HOME_REPORTS = 50;
#ifndef ABSOLUTE_POINTER
#define ABSOLUTE_POINTER 0  // 1: absolute pointer (POINTER_MODE=absolute on the server); 0: relative mouse
#endif
const long ABS_MAX = 32767;  // logical range of the absolute pointer's x and y
#ifndef DEBUG_SERIAL
#define DEBUG_SERIAL 0  // 1: echo every command over USB serial (needs CDCOnBoot=cdc)
#endif

const int WATCHDOG_MS = 8000;  // a hung loop (e.g. blocked USB) reboots the board instead of staying offline

#if ABSOLUTE_POINTER
USBHIDAbsoluteMouse mouse;  // one mouse per build: both kinds share the HID report id
long absX = 0, absY = 0;    // where the absolute pointer is
#else
USBHIDMouse mouse;
#endif
USBHIDKeyboard keyboard;
WiFiClient server;
String session;      // per-connection key from the handshake; empty while not authenticated
uint32_t seq = 0;    // number of the next command in this session
const unsigned MAX_LINE = 512;

#if ABSOLUTE_POINTER
void moveAbs(long x, long y, int8_t wheel = 0, int8_t pan = 0) {
  absX = constrain(x, 0, ABS_MAX);
  absY = constrain(y, 0, ABS_MAX);
  mouse.move(absX, absY, wheel, pan);
}
#else
void movePaced(long dx, long dy) {
  while (dx || dy) {
    int sx = constrain(dx, -STEP, STEP), sy = constrain(dy, -STEP, STEP);
    mouse.move(sx, sy);
    dx -= sx;
    dy -= sy;
    delay(STEP_MS);
  }
}
#endif

uint8_t keyCode(const String &k) {
  if (k == "enter") return KEY_RETURN;
  if (k == "esc") return KEY_ESC;
  if (k == "tab") return KEY_TAB;
  if (k == "backspace") return KEY_BACKSPACE;
  if (k == "space") return ' ';
  if (k == "up") return KEY_UP_ARROW;
  if (k == "down") return KEY_DOWN_ARROW;
  if (k == "left") return KEY_LEFT_ARROW;
  if (k == "right") return KEY_RIGHT_ARROW;
  if (k == "cmd") return KEY_LEFT_GUI;
  if (k == "ctrl") return KEY_LEFT_CTRL;
  if (k == "alt") return KEY_LEFT_ALT;
  if (k == "shift") return KEY_LEFT_SHIFT;
  return k.length() == 1 ? k[0] : 0;
}

bool pressCombo(String combo) {
  combo += '+';
  for (int i; (i = combo.indexOf('+')) >= 0; combo = combo.substring(i + 1)) {
    uint8_t c = keyCode(combo.substring(0, i));
    if (!c) { keyboard.releaseAll(); return false; }
    keyboard.press(c);
  }
  delay(30);
  keyboard.releaseAll();
  return true;
}

// A locked or sleeping phone suspends USB; sending HID reports then can stall. Ask it to wake up instead,
// and report "asleep" right away if it does not, so the board never hangs.
bool usbReady() {
  if (tud_suspended()) {
    tud_remote_wakeup();
    for (int i = 0; i < 50 && tud_suspended(); i++) delay(10);
  }
  return tud_mounted() && !tud_suspended();
}

String run(const String &line) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0;
  sscanf(arg.c_str(), "%ld %ld", &a, &b);

  if (cmd == "ping") return "ok";
  if (!usbReady()) return "err phone usb asleep (unlock the phone)";
#if ABSOLUTE_POINTER
  if (cmd == "home") { moveAbs(0, 0); return "ok"; }
  if (cmd == "move") { moveAbs(absX + a, absY + b); return "ok"; }
  if (cmd == "at") {
    if (a < 0 || a > ABS_MAX || b < 0 || b > ABS_MAX) return "err at needs x y in 0..32767";
    moveAbs(a, b);
    return "ok";
  }
  if (cmd == "scroll") { moveAbs(absX, absY, a, b); return "ok"; }
#else
  if (cmd == "home") {  // slam the pointer into the top-left corner
    for (int i = 0; i < HOME_REPORTS; i++) { mouse.move(-127, -127); delay(2); }
    return "ok";
  }
  if (cmd == "move") { movePaced(a, b); return "ok"; }
  if (cmd == "at") return "err absolute pointer off (build with ABSOLUTE_POINTER=1)";
  if (cmd == "scroll") { mouse.move(0, 0, a, b); return "ok"; }
#endif
  if (cmd == "press") { mouse.press(MOUSE_LEFT); return "ok"; }
  if (cmd == "release") { mouse.release(MOUSE_LEFT); return "ok"; }
  if (cmd == "click") { mouse.press(MOUSE_LEFT); delay(60); mouse.release(MOUSE_LEFT); return "ok"; }
  if (cmd == "type") { keyboard.print(arg); return "ok"; }
  if (cmd == "key") return pressCombo(arg) ? "ok" : "err unknown key";
  return "err unknown command";
}

String hex(const uint8_t *bytes, size_t n) {
  static const char digits[] = "0123456789abcdef";
  String out;
  out.reserve(2 * n);
  for (size_t i = 0; i < n; i++) {
    out += digits[bytes[i] >> 4];
    out += digits[bytes[i] & 15];
  }
  return out;
}

String nonce() {  // hardware RNG: truly random while Wi-Fi is on
  uint8_t bytes[16];
  esp_fill_random(bytes, sizeof bytes);
  return hex(bytes, sizeof bytes);
}

String mac(const char *key, const char *label, const String &a, const String &b) {  // HMAC-SHA256(key, "label:a:b")
  String msg = String(label) + ":" + a + ":" + b;
  uint8_t out[32];
  mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), (const uint8_t *)key, strlen(key),
                  (const uint8_t *)msg.c_str(), msg.length(), out);
  return hex(out, sizeof out);
}

String tag(const char *direction, const String &line) {  // per-message tag in this session (128 bits)
  return mac(session.c_str(), direction, String(seq), line).substring(0, 32);
}

bool sameText(const String &a, const String &b) {  // constant time for equal lengths
  if (a.length() != b.length()) return false;
  uint8_t diff = 0;
  for (size_t i = 0; i < a.length(); i++) diff |= a[i] ^ b[i];
  return diff == 0;
}

// One line from the server, or "" on timeout, disconnect or a line over MAX_LINE (never waits past the deadline,
// even if bytes keep coming).
String readLine(unsigned long timeoutMs) {
  String line;
  unsigned long started = millis();
  while (millis() - started < timeoutMs && server.connected()) {
    while (server.available() && millis() - started < timeoutMs) {
      char c = server.read();
      if (c == '\n') { line.trim(); return line; }
      if (line.length() >= MAX_LINE) return "";
      line += c;
    }
    delay(2);
  }
  return "";
}

const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_TASK_WDT: case ESP_RST_INT_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_SW: return "software";
    case ESP_RST_USB: return "usb";
    default: return "other";
  }
}

// Mutual challenge-response on DEVICE_KEY; false means: not our server (or a different key), hang up.
bool handshake() {
  String boardNonce = nonce();
  server.printf("hello %s %lu %s\n", resetReason(), millis(), boardNonce.c_str());
  String line = readLine(3000);  // "challenge <server nonce> <proof>"
  int sp = line.indexOf(' ', 10);
  if (!line.startsWith("challenge ") || sp != 42) return false;  // 10 + 32 hex digits
  String serverNonce = line.substring(10, sp);
  if (!sameText(line.substring(sp + 1), mac(DEVICE_KEY, "server", boardNonce, serverNonce))) return false;
  server.printf("auth %s\n", mac(DEVICE_KEY, "board", serverNonce, boardNonce).c_str());
  esp_task_wdt_reset();
  if (readLine(3000) != "ok") return false;
  session = mac(DEVICE_KEY, "session", boardNonce, serverNonce);
  seq = 0;
  return true;
}

void hangUp() {  // drop the link and anything held down; the loop reconnects
  mouse.release(MOUSE_LEFT);
  keyboard.releaseAll();
  session = "";
  server.stop();
}

void setup() {
#if DEBUG_SERIAL
  Serial.begin(115200);
#endif
  mouse.begin();
  keyboard.begin();
  USB.productName("HID Bridge");
  USB.begin();
  esp_task_wdt_config_t wdt = {.timeout_ms = WATCHDOG_MS, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  esp_task_wdt_add(NULL);
  WiFi.setHostname("hid-bridge");
  WiFi.setSleep(false);  // modem sleep adds ~100 ms per command
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
  esp_task_wdt_reset();
  if (WiFi.status() != WL_CONNECTED) { mouse.release(MOUSE_LEFT); delay(50); return; }  // auto-reconnects
  if (!server.connected()) {
    mouse.release(MOUSE_LEFT);  // a gesture cut off mid-way must not leave the button held (long press)
    keyboard.releaseAll();
    session = "";
    if (!server.connect(SERVER_HOST, SERVER_PORT, 1000)) { delay(200); return; }  // retry fast: show up at once
    server.setNoDelay(true);
    if (!handshake()) {  // the hello also tells the server why the board last restarted, and its uptime
#if DEBUG_SERIAL
      Serial.println("server failed the DEVICE_KEY handshake (wrong key or not our server)");
#endif
      hangUp();
      delay(1000);  // do not hammer a server that refuses us
      return;
    }
#if DEBUG_SERIAL
    Serial.printf("connected to %s:%d as %s after %lu ms\n", SERVER_HOST, SERVER_PORT,
                  WiFi.localIP().toString().c_str(), millis());
#endif
  }
  if (server.available()) {
    String line = readLine(1000);
    int sp = line.lastIndexOf(' ');
    // no tag, a wrong tag (not our session, replayed, reordered) or an over-long line: hang up, never execute
    if (session.length() == 0 || sp < 0 || !sameText(line.substring(sp + 1), tag("cmd", line.substring(0, sp)))) {
#if DEBUG_SERIAL
      Serial.println("command failed authentication: hanging up");
#endif
      hangUp();
      return;
    }
    String command = line.substring(0, sp);
#if DEBUG_SERIAL
    unsigned long started = millis();
#endif
    String reply = run(command);
    server.print(reply + " " + tag("reply", reply) + "\n");
    seq++;
#if DEBUG_SERIAL
    Serial.printf("%s -> %s (%lu ms)\n", command.c_str(), reply.c_str(), millis() - started);
#endif
  }
}
