// HID bridge: an ESP32-S3 that shows up as a USB mouse + keyboard and takes
// line commands over TCP (Wi-Fi). It holds no logic; the server decides.
//
// Protocol (one command per line, the board answers "ok" or "err <why>"):
//   ping | home | move <dx> <dy> | press | release | click
//   scroll <v> <h> | type <ascii text> | key <combo, e.g. cmd+h, enter>
//   at <x> <y>   (ABSOLUTE_POINTER builds only: put the pointer at x, y in 0..32767 across the screen)
//   consumer <home | search | globe | keyboard | 0x0001..0x03ff>   (a consumer-page key, see consumerUsage)
//   key combos may include "globe" (held as a modifier, e.g. globe+h)
//   input-language keys for a hardware keyboard (#17-E): capslock | lang1 (alias hangul, HID 0x90 Hangul/English,
//   Kana on Apple keyboards) | lang2 (HID 0x91 Hanja, Eisu on Apple keyboards) -- which one iOS honours is a
//   device test, not something this firmware assumes
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
//
// Away from home (#6, optional, see secrets.example.h): WIFI_SSID2 adds a second network (e.g. the phone's hotspot),
// and WG_* settings send the link through a WireGuard tunnel, so the board reaches the server from any Wi-Fi and the
// link is encrypted on the way. With SERVER_HOST_HOME too, the board skips the tunnel on the first (home) Wi-Fi and
// connects to that address directly. Without WG_* the board connects to SERVER_HOST directly, as before.

#include <WiFi.h>
#include <esp_random.h>
#include <esp_task_wdt.h>
#include <mbedtls/md.h>
#include "USB.h"
#include "USBHIDMouse.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"
#include "tusb.h"
#include "secrets.h"  // WIFI_SSID, WIFI_PASS, SERVER_HOST, SERVER_PORT, DEVICE_KEY (+ optional WIFI_SSID2, WG_*)

#ifdef WG_PRIVATE_KEY
#define USE_WIREGUARD 1
#include <time.h>
#include "lwip/tcpip.h"
#include "src/esp_wireguard/esp_wireguard.h"  // droscy/esp_wireguard, vendored (see its VENDORED.md)
#ifndef WG_ENDPOINT_PORT
#define WG_ENDPOINT_PORT 51820
#endif
#ifndef WG_NETMASK
#define WG_NETMASK "255.255.255.0"
#endif
#if !defined(WG_PEER_PUBLIC_KEY) || !defined(WG_ENDPOINT) || !defined(WG_ADDRESS)
#error "WG_PRIVATE_KEY needs WG_PEER_PUBLIC_KEY, WG_ENDPOINT and WG_ADDRESS too (see secrets.example.h)"
#endif
#else
#define USE_WIREGUARD 0
#endif

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

// Declared first on purpose (#17-E): arduino-esp32 registers each HID function in its constructor, so declaration
// order is descriptor order, and the first USBHID constructed sets the interface protocol. Keyboard first makes the
// descriptor start with the keyboard collection and the interface protocol KEYBOARD. With the mouse first, iOS adopted
// the board as an AssistiveTouch pointer only: no "Hardware Keyboard" in Settings, Shift ignored, and typing always
// read with the primary language's layout (same as adafruit/Adafruit_nRF52_Arduino#870). Device test pending.
USBHIDKeyboard keyboard;
#if ABSOLUTE_POINTER
USBHIDAbsoluteMouse mouse;  // one mouse per build: both kinds share the HID report id
long absX = 0, absY = 0;    // where the absolute pointer is
#else
USBHIDMouse mouse;
#endif
USBHIDConsumerControl consumer;
// Consumer-page keys Apple maps for keyboards (Accessory Design Guidelines, table 14-3). Not the library's
// CONSUMER_CONTROL_HOME (0x0223, AC Home): Apple's Home Button is 0x0040 (Menu).
const uint16_t CONSUMER_HOME = 0x0040, CONSUMER_SEARCH = 0x0221, CONSUMER_GLOBE = 0x029D, CONSUMER_KEYBOARD = 0x01AE;
WiFiClient server;
const unsigned long WIFI_SWITCH_MS = 15000;  // with WIFI_SSID2: try the other network after this long without Wi-Fi
int wifiNet = 0;                             // 0 = WIFI_SSID, 1 = WIFI_SSID2
unsigned long wifiDownSince = 0;             // when Wi-Fi was last seen down (or the current network was chosen)
bool wifiWasUp = false;
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
  if (k == "capslock") return KEY_CAPS_LOCK;
  return k.length() == 1 ? k[0] : 0;
}

// Keys the Arduino keyboard API cannot press by name (usage IDs above its 0x88 offset range): sent as raw usages.
uint8_t rawKeyCode(const String &k) {
  if (k == "lang1" || k == "hangul") return 0x90;  // Keyboard LANG1
  if (k == "lang2") return 0x91;                    // Keyboard LANG2
  return 0;
}

uint16_t consumerUsage(const String &k) {
  if (k == "home") return CONSUMER_HOME;          // Home Button
  if (k == "search") return CONSUMER_SEARCH;      // Spotlight
  if (k == "globe") return CONSUMER_GLOBE;        // Globe key: next keyboard / input language
  if (k == "keyboard") return CONSUMER_KEYBOARD;  // show / hide the on-screen keyboard
  if (k.startsWith("0x")) {                       // any usage of the 10-bit consumer report, for experiments
    long u = strtol(k.c_str() + 2, nullptr, 16);
    return u > 0 && u <= 0x3FF ? u : 0;
  }
  return 0;
}

bool pressCombo(String combo) {
  combo += '+';
  bool globe = false;  // the Globe key is a consumer usage, held for the whole combo like a modifier
  for (int i; (i = combo.indexOf('+')) >= 0; combo = combo.substring(i + 1)) {
    String k = combo.substring(0, i);
    if (k == "globe") {
      consumer.press(CONSUMER_GLOBE);
      globe = true;
      delay(10);
      continue;
    }
    if (uint8_t raw = rawKeyCode(k)) {
      keyboard.pressRaw(raw);
      continue;
    }
    uint8_t c = keyCode(k);
    if (!c) { keyboard.releaseAll(); consumer.release(); return false; }
    keyboard.press(c);
  }
  delay(30);
  keyboard.releaseAll();
  if (globe) consumer.release();
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
  if (cmd == "consumer") {
    uint16_t u = consumerUsage(arg);
    if (!u) return "err unknown consumer key";
    consumer.press(u);
    delay(40);
    consumer.release();
    return "ok";
  }
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
  consumer.release();
  session = "";
  server.stop();
}

void wifiBegin(int net) {
  wifiNet = net;
  wifiDownSince = millis();
#ifdef WIFI_SSID2
  if (net == 1) { WiFi.begin(WIFI_SSID2, WIFI_PASS2); return; }
#endif
  WiFi.begin(WIFI_SSID, WIFI_PASS);  // not wifiBegin(0): that called itself for ever, and the watchdog reset the board
}

// True while Wi-Fi is up. Never blocks: WiFi.begin() returns at once and the driver keeps retrying; with a second
// network, switch after WIFI_SWITCH_MS without a connection (e.g. home Wi-Fi out of reach, the hotspot is on).
bool wifiUp() {
  if (WiFi.status() == WL_CONNECTED) { wifiWasUp = true; return true; }
  if (wifiWasUp) { wifiWasUp = false; wifiDownSince = millis(); }
#ifdef WIFI_SSID2
  if (millis() - wifiDownSince > WIFI_SWITCH_MS) {
    WiFi.disconnect();
    wifiBegin(1 - wifiNet);
  }
#endif
  return false;
}

#if USE_WIREGUARD
wireguard_config_t wgConfig = ESP_WIREGUARD_CONFIG_DEFAULT();
wireguard_ctx_t wgCtx = ESP_WIREGUARD_CONTEXT_DEFAULT();
bool wgInited = false, wgConnected = false, clockAsked = false;

// The library calls lwIP's raw API (netif_add, dns_gethostbyname, udp_*). arduino-esp32 3.x builds lwIP with
// TCPIP core locking and thread-safety checks, so every call from this task must hold the core lock, or lwIP
// asserts and the board resets (a compile-only check cannot see this).
esp_err_t wgCall(esp_err_t (*fn)(wireguard_ctx_t *), wireguard_ctx_t *ctx) {
  LOCK_TCPIP_CORE();
  esp_err_t err = fn(ctx);
  UNLOCK_TCPIP_CORE();
  return err;
}
esp_err_t wgInit(wireguard_ctx_t *ctx) { return esp_wireguard_init(&wgConfig, ctx); }
esp_err_t wgPeerUp(wireguard_ctx_t *ctx) { return esp_wireguard_peer_is_up(ctx); }

// True once the tunnel to the hub is up. Each step returns at once and is retried on the next loop: the clock first
// (a WireGuard handshake carries a timestamp, so it needs NTP), then the interface, then the peer (the hub's host
// name, if any, resolves in the background).
bool tunnelUp() {
  if (!clockAsked) { configTime(0, 0, "pool.ntp.org", "time.google.com"); clockAsked = true; }
  if (time(nullptr) < 1700000000) return false;  // not synced yet
  if (!wgInited) {
    wgConfig.private_key = WG_PRIVATE_KEY;
    wgConfig.public_key = WG_PEER_PUBLIC_KEY;
    wgConfig.address = WG_ADDRESS;
    wgConfig.netmask = WG_NETMASK;
    wgConfig.endpoint = WG_ENDPOINT;
    wgConfig.port = WG_ENDPOINT_PORT;
    wgConfig.persistent_keepalive = 25;  // keeps the hotspot's NAT mapping open
    if (wgCall(wgInit, &wgCtx) != ESP_OK) return false;
    wgInited = true;
  }
  if (!wgConnected) {
    if (wgCall(esp_wireguard_connect, &wgCtx) != ESP_OK) return false;  // ESP_ERR_RETRY while DNS is pending
    wgConnected = true;
  }
  return wgCall(wgPeerUp, &wgCtx) == ESP_OK;
}
#endif

void setup() {
#if DEBUG_SERIAL
  Serial.begin(115200);
#endif
  keyboard.begin();
  mouse.begin();
  consumer.begin();
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
  if (!wifiUp()) { mouse.release(MOUSE_LEFT); delay(50); return; }  // auto-reconnects (or switches network)
  if (!server.connected()) {
    mouse.release(MOUSE_LEFT);  // a gesture cut off mid-way must not leave the button held (long press)
    keyboard.releaseAll();
    consumer.release();
    session = "";
    const char *host = SERVER_HOST;
#if USE_WIREGUARD
#ifdef SERVER_HOST_HOME
    if (wifiNet == 0) host = SERVER_HOST_HOME;  // on the first (home) Wi-Fi: straight to the server, no tunnel
    else
#endif
    if (!tunnelUp()) { delay(100); return; }  // SERVER_HOST is inside the tunnel
#endif
    if (!server.connect(host, SERVER_PORT, 1000)) { delay(200); return; }  // retry fast: show up at once
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
    Serial.printf("connected to %s:%d as %s on %s after %lu ms\n", host, SERVER_PORT,
                  WiFi.localIP().toString().c_str(), WiFi.SSID().c_str(), millis());
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
