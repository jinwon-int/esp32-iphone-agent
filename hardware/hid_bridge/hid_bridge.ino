// HID bridge: an ESP32-S3 that shows up as a USB mouse + keyboard and takes
// line commands over TCP (Wi-Fi). It holds no logic; the server decides.
//
// Protocol (one command per line, the board answers "ok" or "err <why>"):
//   ping | home | move <dx> <dy> | press | release | click
//   hidstat  (answers "ok resent=<n> failed=<n>": button reports sent again / given up, see confirmButtons, #60)
//   usbreset [off ms]  (drops off USB and comes back, like unplugging the board; answers "ok mounted <ms>", see
//                usbReset, #60)
//   scroll <v> <h> | type <ascii text> | key <combo, e.g. cmd+h, enter>
//   at <x> <y>   (ABSOLUTE_POINTER builds only: put the pointer at x, y in 0..32767 across the screen)
//   stroke <x1> <y1> <x2> <y2> <n>   (ABSOLUTE_POINTER builds only: press at x1, y1, n reports STEP_MS apart to
//                x2, y2, release -- a quick finger stroke played by the board itself, see stroke())
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
//   board  -> hello <why> <uptime ms> <board nonce>   (why: reset reason + ",wifi=<a>/<b>", see wifiDrop below)
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
//
// BLE_LINK=1 (experimental) replaces Wi-Fi with Bluetooth LE: the controller is an app on the same phone instead of a
// server, so the board needs no network at all. The board is a GATT peripheral with one service and two
// characteristics -- the app writes bytes to RX, the board notifies bytes on TX -- and the very same lines go over it:
// the same hello / challenge / auth handshake (why: reset reason + ",ble"), the same tags, the same commands. The board
// says hello once the app subscribes to TX, and hangs up a central that does not subscribe or authenticate in time, so
// a stranger in radio range cannot drive the phone or keep the one connection for long (but can take it again and
// again: a denial of service). The link is not BLE-encrypted; like the Wi-Fi link without WireGuard, only the tags
// protect it (commands, incl. typed text, are readable nearby). secrets.h needs only DEVICE_KEY in this build.
//
// BLE_HID=1 with BLE_LINK=1 (experimental, #104, not device-tested yet) sends the mouse, keyboard and consumer
// reports over a Bluetooth LE HID service (0x1812) on the same peripheral instead of USB, so the board only needs
// power near the phone, not the phone's port (see ble_hid.h). Pair it once in Settings > Bluetooth; the bonded link is
// encrypted, for the commands too. The phone keeps one connection for HID and the app, so the board never hangs it
// up: a failed or unauthenticated session just ends, and the app starts a new one by subscribing to TX again.
// usbreset answers err in this build.

#ifndef BLE_LINK
#define BLE_LINK 0  // 1: Bluetooth LE link to an app on the phone, no Wi-Fi; 0: TCP over Wi-Fi to the server
#endif
#ifndef BLE_HID
#define BLE_HID 0  // 1 (experimental, #104): mouse + keyboard over Bluetooth LE too, so the board need not be plugged in
#endif
#if BLE_HID && !BLE_LINK
#error "BLE_HID=1 needs BLE_LINK=1: the HID service shares the Bluetooth LE peripheral with the command link"
#endif

#if BLE_LINK
#include <BLEDevice.h>
#include <BLEServer.h>
#include <freertos/stream_buffer.h>
#else
#include <WiFi.h>
#endif
#include <esp_random.h>
#include <esp_task_wdt.h>
#include <mbedtls/md.h>
#include "USB.h"
#include "USBHIDMouse.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"
#include "tusb.h"
#include "secrets.h"  // WIFI_SSID, WIFI_PASS, SERVER_HOST, SERVER_PORT, DEVICE_KEY (+ optional WIFI_SSID2, WG_*)

#if defined(WG_PRIVATE_KEY) && !BLE_LINK
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
const long STROKE_MAX = 100;  // reports in one stroke: at most 0.8 s at STEP_MS, well inside the watchdog
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
#if BLE_HID
#include "ble_hid.h"  // same calls, reports go over the Bluetooth LE HID service instead of USB (#104)
BleHidKeyboard keyboard;
BleHidMouse mouse;
#if ABSOLUTE_POINTER
long absX = 0, absY = 0;  // where the absolute pointer is
#endif
BleHidConsumer consumer;
#else
USBHIDKeyboard keyboard;
#if ABSOLUTE_POINTER
USBHIDAbsoluteMouse mouse;  // one mouse per build: both kinds share the HID report id
long absX = 0, absY = 0;    // where the absolute pointer is
#else
USBHIDMouse mouse;
#endif
USBHIDConsumerControl consumer;
#endif
// Consumer-page keys Apple maps for keyboards (Accessory Design Guidelines, table 14-3). Not the library's
// CONSUMER_CONTROL_HOME (0x0223, AC Home): Apple's Home Button is 0x0040 (Menu).
const uint16_t CONSUMER_HOME = 0x0040, CONSUMER_SEARCH = 0x0221, CONSUMER_GLOBE = 0x029D, CONSUMER_KEYBOARD = 0x01AE;
#if !BLE_LINK
WiFiClient server;
const unsigned long WIFI_SWITCH_MS = 15000;  // with WIFI_SSID2: try the other network after this long without Wi-Fi
int wifiNet = 0;                             // 0 = WIFI_SSID, 1 = WIFI_SSID2
unsigned long wifiDownSince = 0;             // when Wi-Fi was last seen down (or the current network was chosen)
bool wifiWasUp = false;
// Set from the Wi-Fi task (onWifiEvent). Joining is not "up": WL_CONNECTED waits for an address (DHCP), and an iPhone
// hotspot can be slow to give one, so a join restarts the switch timer instead of being cut off by it (#65).
volatile unsigned long wifiJoinedAt = 0;
// The last reason (esp_wifi WIFI_REASON_*, 0 = none) each network dropped the board, sent in the hello, so a drop
// away from home can be read in the server log without a serial console (#65). Our own leave (a switch) is skipped.
volatile uint8_t wifiDrop[2] = {0, 0};
#endif
String session;      // per-connection key from the handshake; empty while not authenticated
uint32_t seq = 0;    // number of the next command in this session
const unsigned MAX_LINE = 512;

// The link to the controller, as a byte stream: TCP to the server, or the BLE pair of characteristics to the app.
#if BLE_LINK
const char *BLE_NAME = "HID Bridge";
const char *BLE_SERVICE = "23c2efd6-8e20-4e1b-a71a-e26bc6d9320c";
const char *BLE_RX = "aebdbe49-932f-4ce9-8d9d-ae64db99d2ed";  // app -> board: write (with or without response)
const char *BLE_TX = "7f186178-861e-4e86-98e0-347e67454906";  // board -> app: notify
const unsigned long BLE_SUBSCRIBE_MS = 5000;  // a central that does not subscribe to TX by then is dropped
BLEServer *bleServer = nullptr;
BLECharacteristic *bleTx = nullptr;
StreamBufferHandle_t bleIn = nullptr;  // RX bytes: written by the BLE host task, read by loop() (one each way)
// Set from the BLE host task. bleConnects counts connections, so loop() greets each connection once.
volatile bool bleLinked = false, bleSubscribed = false, bleOverflow = false;
volatile uint16_t bleConn = 0;
volatile uint32_t bleConnects = 0;
volatile unsigned long bleConnectedAt = 0;
uint32_t bleGreeted = 0;     // the connection the board last said hello on (a count of bleConnects)
uint16_t bleGreetedConn = 0;  // ... and its handle

class BleServerEvents : public BLEServerCallbacks {
  void onConnect(BLEServer *, ble_gap_conn_desc *desc) override {
    bleConn = desc->conn_handle;
    bleSubscribed = false;
    bleConnectedAt = millis();
    bleConnects++;
    bleLinked = true;  // advertising stops: one central at a time
#if BLE_HID
    blehid::secured = false;  // until the bonded link is encrypted again (onAuthenticationComplete)
#endif
  }
  void onDisconnect(BLEServer *, ble_gap_conn_desc *) override {
    bleLinked = false;
    bleSubscribed = false;  // advertising restarts (advertiseOnDisconnect)
#if BLE_HID
    blehid::secured = false;
#endif
  }
};

class BleRxEvents : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c, ble_gap_conn_desc *) override {  // keep it short: runs in the BLE host task
    size_t n = c->getLength();
    if (xStreamBufferSend(bleIn, c->getData(), n, 0) != n) bleOverflow = true;  // loop() hangs up
  }
};

class BleTxEvents : public BLECharacteristicCallbacks {
  void onSubscribe(BLECharacteristic *, ble_gap_conn_desc *, uint16_t subValue) override {
    bleSubscribed = subValue & 1;  // bit 0: notifications
#if BLE_HID
    // The system holds the connection for HID and the app shares it, so a connection is no longer an app session:
    // each subscription to TX is. Counting it as a "connect" gives it its own hello, as a new connection would.
    if (bleSubscribed) bleConnects++;
#endif
  }
};

void bleBegin() {
  bleIn = xStreamBufferCreate(2 * MAX_LINE, 1);
  BLEDevice::init(BLE_NAME);
  bleServer = BLEDevice::createServer();
  bleServer->setCallbacks(new BleServerEvents());
  bleServer->advertiseOnDisconnect(true);
  BLEService *service = bleServer->createService(BLE_SERVICE);
  BLECharacteristic *rx = service->createCharacteristic(
      BLE_RX, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rx->setCallbacks(new BleRxEvents());
  bleTx = service->createCharacteristic(BLE_TX, BLECharacteristic::PROPERTY_NOTIFY);
  bleTx->setCallbacks(new BleTxEvents());
  service->start();
  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(BLE_SERVICE);  // the app scans for this service; the name goes in the scan response
  adv->setScanResponse(true);
#if BLE_HID
  blehid::attach(bleServer, adv);
#endif
  BLEDevice::startAdvertising();
}

// Connected means: still the connection the board said hello on. A session never carries over to a new connection
// that came in while loop() was busy (e.g. typing), so the new one gets its own hello, and nothing stays held down.
bool linkConnected() { return bleLinked && bleSubscribed && bleGreeted == bleConnects; }
size_t linkAvailable() { return xStreamBufferBytesAvailable(bleIn); }
char linkRead() {
  char c = 0;
  xStreamBufferReceive(bleIn, &c, 1, 0);
  return c;
}
void linkWrite(const String &s) {  // the library cuts a notification at MTU - 3 bytes, so send it in pieces
  uint16_t mtu = bleServer->getPeerMTU(bleConn);
  size_t piece = mtu > 23 ? mtu - 3 : 20;
  for (size_t i = 0; i < s.length(); i += piece) {
    bleTx->setValue((const uint8_t *)s.c_str() + i, min(piece, s.length() - i));
    bleTx->notify();
  }
}
void linkStop() {  // hang up the greeted connection only, never one that came in since
#if BLE_HID
  // Never drop the connection: it is also the phone's HID link. The session is over (session = ""); the app gets a
  // new hello only when it subscribes to TX again, and nothing it writes before that is executed.
#else
  if (bleLinked && bleGreeted == bleConnects) bleServer->disconnect(bleGreetedConn);  // asynchronous: onDisconnect follows
#endif
}
void bleDrain() {  // empty the RX buffer as its one reader (xStreamBufferReset is not safe against a concurrent write)
  uint8_t junk[64];
  while (xStreamBufferReceive(bleIn, junk, sizeof junk, 0) > 0) {}
}
#else
bool linkConnected() { return server.connected(); }
size_t linkAvailable() { return server.available(); }
char linkRead() { return server.read(); }
void linkWrite(const String &s) { server.print(s); }
void linkStop() { server.stop(); }
#endif

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

// The library drops a HID report when the USB endpoint is still busy (USBHID::SendReport: "not ready", or the wait
// for the previous one timed out) and press()/release()/click() never say so. A dropped release left the phone
// with a finger down: a Home Screen swipe stopped between two pages until the screen was touched (#60). So after
// every button change send the current button state again until the phone took it; the same state twice is a
// no-op for the phone. hidResent/hidFailed count the retries and the give-ups (see "hidstat").
uint32_t hidResent = 0, hidFailed = 0;
const int BUTTON_TRIES = 3;
const int BUTTON_RETRY_MS = 10;

bool confirmButtons() {
  uint8_t b = mouse.isPressed(MOUSE_LEFT) ? MOUSE_LEFT : 0;
  for (int i = 0; i < BUTTON_TRIES; i++) {
#if ABSOLUTE_POINTER
    hid_abs_mouse_report_t r;
    r.x = (int16_t)absX;
    r.y = (int16_t)absY;
#else
    hid_mouse_report_t r;
    r.x = 0;
    r.y = 0;
#endif
    r.buttons = b;
    r.wheel = 0;
    r.pan = 0;
    if (mouse.sendReport(r)) return true;
    hidResent++;
    delay(BUTTON_RETRY_MS);
  }
  hidFailed++;
  return false;
}

#if ABSOLUTE_POINTER
// A Home Screen page turn needs a quick stroke, but every command is one link round trip (about 170 ms over Bluetooth
// LE, device 2026-10-22/23, #74): sent as single "at" reports the finger crawled and iOS snapped the page back. Here the
// board plays the whole stroke itself at STEP_MS per report. The button is confirmed down before moving and always
// let go at the end (#60: a lost release leaves a finger on the screen).
String stroke(const String &arg) {
  long x1, y1, x2, y2, n;
  if (sscanf(arg.c_str(), "%ld %ld %ld %ld %ld", &x1, &y1, &x2, &y2, &n) != 5) return "err stroke needs x1 y1 x2 y2 n";
  long lo = min(min(x1, y1), min(x2, y2)), hi = max(max(x1, y1), max(x2, y2));
  if (lo < 0 || hi > ABS_MAX) return "err stroke needs positions in 0..32767";
  if (n < 1 || n > STROKE_MAX) return "err stroke needs n in 1.." + String(STROKE_MAX);
  moveAbs(x1, y1);
  delay(STEP_MS);
  mouse.press(MOUSE_LEFT);
  if (!confirmButtons()) {
    mouse.release(MOUSE_LEFT);
    confirmButtons();
    return "err stroke press not sent (usb busy)";
  }
  for (long i = 1; i <= n; i++) {
    delay(STEP_MS);
    moveAbs(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n);
  }
  mouse.release(MOUSE_LEFT);
  return confirmButtons() ? "ok" : "err stroke release not sent (usb busy)";
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
#if BLE_HID
  return blehid::ready();  // a bonded, encrypted connection; there is no USB to wake
#endif
  if (tud_suspended()) {
    tud_remote_wakeup();
    for (int i = 0; i < 50 && tud_suspended(); i++) delay(10);
  }
  return tud_mounted() && !tud_suspended();
}

// #60, device 2026-10-23: after a Home Screen page turn the page sometimes stands three quarters turned and iOS
// ignores the pointer (a click does nothing, a move barely nudges it) while the board saw every report go out
// (hidstat resent=0 failed=0). Only unplugging the board freed it. usbreset does that without a hand: the device
// leaves the bus (tud_disconnect) for `off` ms, comes back (tud_connect) and waits for the phone to set it up
// again. The button and keys are let go first, so nothing is held when it returns. Bounded well inside the app's
// 5 s reply limit and the watchdog: off <= USB_RESET_OFF_MAX_MS plus at most USB_RESET_MOUNT_MS for the mount.
volatile uint32_t usbMounts = 0;  // counted by onUsbMounted (tud_mount_cb -> ARDUINO_USB_STARTED_EVENT)

void onUsbMounted(void *, esp_event_base_t, int32_t, void *) { usbMounts++; }

const long USB_RESET_OFF_MS = 500;
const long USB_RESET_OFF_MAX_MS = 1000;
const long USB_RESET_MOUNT_MS = 3000;

String usbReset(const String &arg) {
#if BLE_HID
  (void)arg;
  return "err usbreset needs the USB build (this is BLE_HID)";
#endif
  long off = arg.length() ? arg.toInt() : USB_RESET_OFF_MS;
  if (off < 100 || off > USB_RESET_OFF_MAX_MS) return "err usbreset needs off ms in 100.." + String(USB_RESET_OFF_MAX_MS);
  if (tud_mounted() && !tud_suspended()) {
    mouse.release(MOUSE_LEFT);
    keyboard.releaseAll();
    consumer.release();
  }
  // tud_mounted() stays true across tud_disconnect until the host resets the bus again, so it cannot tell the
  // new mount from the old one ("ok mounted 0", device 2026-10-23): wait for the mount event instead.
  uint32_t before = usbMounts;
  if (!tud_disconnect()) return "err usbreset not supported";
  delay(off);
  esp_task_wdt_reset();
  if (!tud_connect()) return "err usbreset could not reconnect";
  unsigned long started = millis();
  while (usbMounts == before && millis() - started < (unsigned long)USB_RESET_MOUNT_MS) delay(10);
  esp_task_wdt_reset();
  unsigned long took = millis() - started;
  return usbMounts != before ? "ok mounted " + String(took)
                             : "err usbreset not mounted again within " + String(USB_RESET_MOUNT_MS) + " ms";
}

String run(const String &line) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0;
  sscanf(arg.c_str(), "%ld %ld", &a, &b);

  if (cmd == "ping") return "ok";
  if (cmd == "usbreset") return usbReset(arg);  // also when the phone has USB asleep: coming back may wake it
#if BLE_HID
  if (!usbReady()) return "err phone not paired (pair the board in Settings > Bluetooth)";
#else
  if (!usbReady()) return "err phone usb asleep (unlock the phone)";
#endif
#if ABSOLUTE_POINTER
  if (cmd == "home") { moveAbs(0, 0); return "ok"; }
  if (cmd == "move") { moveAbs(absX + a, absY + b); return "ok"; }
  if (cmd == "at") {
    if (a < 0 || a > ABS_MAX || b < 0 || b > ABS_MAX) return "err at needs x y in 0..32767";
    moveAbs(a, b);
    return "ok";
  }
  if (cmd == "scroll") { moveAbs(absX, absY, a, b); return "ok"; }
  if (cmd == "stroke") return stroke(arg);
#else
  if (cmd == "home") {  // slam the pointer into the top-left corner
    for (int i = 0; i < HOME_REPORTS; i++) { mouse.move(-127, -127); delay(2); }
    return "ok";
  }
  if (cmd == "move") { movePaced(a, b); return "ok"; }
  if (cmd == "at" || cmd == "stroke") return "err absolute pointer off (build with ABSOLUTE_POINTER=1)";
  if (cmd == "scroll") { mouse.move(0, 0, a, b); return "ok"; }
#endif
  if (cmd == "press") {
    mouse.press(MOUSE_LEFT);
    return confirmButtons() ? "ok" : "err press not sent (usb busy)";
  }
  if (cmd == "release") {
    mouse.release(MOUSE_LEFT);
    return confirmButtons() ? "ok" : "err release not sent (usb busy)";
  }
  if (cmd == "click") {
    mouse.press(MOUSE_LEFT);
    bool down = confirmButtons();
    delay(60);
    mouse.release(MOUSE_LEFT);
    bool up = confirmButtons();
    return up ? (down ? "ok" : "err click press not sent (usb busy)") : "err click release not sent (usb busy)";
  }
  if (cmd == "hidstat") return "ok resent=" + String(hidResent) + " failed=" + String(hidFailed);
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

String nonce() {  // hardware RNG: truly random while the radio (Wi-Fi or Bluetooth) is on
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
  while (millis() - started < timeoutMs && linkConnected()) {
    while (linkAvailable() && millis() - started < timeoutMs) {
      char c = linkRead();
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
  char why[48];  // no spaces: the server reads the hello as four words
#if BLE_LINK
  snprintf(why, sizeof why, "%s,ble", resetReason());
#else
  snprintf(why, sizeof why, "%s,wifi=%u/%u", resetReason(), wifiDrop[0], wifiDrop[1]);
#endif
  linkWrite("hello " + String(why) + " " + String(millis()) + " " + boardNonce + "\n");
  String line = readLine(3000);  // "challenge <server nonce> <proof>"
  int sp = line.indexOf(' ', 10);
  if (!line.startsWith("challenge ") || sp != 42) return false;  // 10 + 32 hex digits
  String serverNonce = line.substring(10, sp);
  if (!sameText(line.substring(sp + 1), mac(DEVICE_KEY, "server", boardNonce, serverNonce))) return false;
  linkWrite("auth " + mac(DEVICE_KEY, "board", serverNonce, boardNonce) + "\n");
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
  linkStop();
}

#if !BLE_LINK
void wifiBegin(int net) {
  wifiNet = net;
  wifiDownSince = millis();
#ifdef WIFI_SSID2
  if (net == 1) { WiFi.begin(WIFI_SSID2, WIFI_PASS2); return; }
#endif
  WiFi.begin(WIFI_SSID, WIFI_PASS);  // not wifiBegin(0): that called itself for ever, and the watchdog reset the board
}

void onWifiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {  // runs in the Wi-Fi task: only sets two values
  if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
    wifiJoinedAt = millis();
  } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED && info.wifi_sta_disconnected.reason != WIFI_REASON_ASSOC_LEAVE) {
    wifiDrop[wifiNet] = info.wifi_sta_disconnected.reason;
  }
}

// True while Wi-Fi is up. Never blocks: WiFi.begin() returns at once and the driver keeps retrying; with a second
// network, switch after WIFI_SWITCH_MS without a connection (e.g. home Wi-Fi out of reach, the hotspot is on).
bool wifiUp() {
  if (WiFi.status() == WL_CONNECTED) { wifiWasUp = true; return true; }
  if (wifiWasUp) { wifiWasUp = false; wifiDownSince = millis(); }
#ifdef WIFI_SSID2
  unsigned long joined = wifiJoinedAt;
  if ((long)(joined - wifiDownSince) > 0) wifiDownSince = joined;  // joined since: give it the full time for an address
  if (millis() - wifiDownSince > WIFI_SWITCH_MS) {
    WiFi.disconnect();
    wifiBegin(1 - wifiNet);
  }
#endif
  return false;
}
#endif

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
  USB.onEvent(ARDUINO_USB_STARTED_EVENT, onUsbMounted);
  USB.begin();
#if BLE_LINK
  bleBegin();
#endif
  esp_task_wdt_config_t wdt = {.timeout_ms = WATCHDOG_MS, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  esp_task_wdt_add(NULL);
#if !BLE_LINK
  WiFi.setHostname("hid-bridge");
  WiFi.setSleep(false);  // modem sleep adds ~100 ms per command
  WiFi.setAutoReconnect(true);
  WiFi.onEvent(onWifiEvent);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
#endif
}

void releaseAll() {  // a gesture cut off mid-way must not leave the button held (long press)
  mouse.release(MOUSE_LEFT);
  keyboard.releaseAll();
  consumer.release();
}

// True once there is an authenticated link to the controller; otherwise works towards one and returns at once.
#if BLE_LINK
bool linkReady() {
  if (bleOverflow) {  // more than the buffer in one go: an over-long line, never executed
    bleOverflow = false;
    hangUp();
    return false;
  }
  if (linkConnected() && session.length()) return true;
  releaseAll();
  session = "";
  if (bleLinked && bleSubscribed && bleGreeted != bleConnects) {  // a new connection, listening: say hello once
    bleGreeted = bleConnects;
    bleGreetedConn = bleConn;
    bleDrain();  // nothing written before the hello counts
    bleOverflow = false;
    if (handshake()) return true;
#if DEBUG_SERIAL
    Serial.println("app failed the DEVICE_KEY handshake (wrong key or not our app): hanging up");
#endif
    hangUp();
    return false;
  }
  // connected but not subscribed in time, unsubscribed, or refused: free the one connection for the app
#if !BLE_HID  // with BLE_HID the connection is the phone's HID link and never subscribes to TX itself: keep it
  if (bleLinked && millis() - bleConnectedAt > BLE_SUBSCRIBE_MS) bleServer->disconnect(bleConn);
#endif
  delay(20);
  return false;
}
#else
bool linkReady() {
  if (!wifiUp()) { mouse.release(MOUSE_LEFT); delay(50); return false; }  // auto-reconnects (or switches network)
  if (server.connected()) return true;
  releaseAll();
  session = "";
  const char *host = SERVER_HOST;
#if USE_WIREGUARD
#ifdef SERVER_HOST_HOME
  if (wifiNet == 0) host = SERVER_HOST_HOME;  // on the first (home) Wi-Fi: straight to the server, no tunnel
  else
#endif
  if (!tunnelUp()) { delay(100); return false; }  // SERVER_HOST is inside the tunnel
#endif
  if (!server.connect(host, SERVER_PORT, 1000)) { delay(200); return false; }  // retry fast: show up at once
  server.setNoDelay(true);
  if (!handshake()) {  // the hello also tells the server why the board last restarted, and its uptime
#if DEBUG_SERIAL
    Serial.println("server failed the DEVICE_KEY handshake (wrong key or not our server)");
#endif
    hangUp();
    delay(1000);  // do not hammer a server that refuses us
    return false;
  }
#if DEBUG_SERIAL
  Serial.printf("connected to %s:%d as %s on %s after %lu ms\n", host, SERVER_PORT,
                WiFi.localIP().toString().c_str(), WiFi.SSID().c_str(), millis());
#endif
  return true;
}
#endif

void loop() {
  esp_task_wdt_reset();
  if (!linkReady()) return;
  if (linkAvailable()) {
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
    linkWrite(reply + " " + tag("reply", reply) + "\n");
    seq++;
#if DEBUG_SERIAL
    Serial.printf("%s -> %s (%lu ms)\n", command.c_str(), reply.c_str(), millis() - started);
#endif
  }
}
