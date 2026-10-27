// BLE_HID=1 (experimental, #104): the board is a Bluetooth LE mouse + keyboard instead of a USB one, so it does not
// have to be plugged into the phone -- only powered near it. Included by hid_bridge.ino in BLE_LINK=1 builds only.
//
// The HID-over-GATT service (0x1812) runs on the same peripheral as the command link, so the phone keeps one BLE
// connection that the system uses for HID and the controller app uses for the RX/TX characteristics. HID needs an
// encrypted, bonded link: pair once in Settings > Bluetooth ("Just Works", no passkey); after that the link is
// encrypted for the app too.
//
// The three classes mirror the parts of USBHIDKeyboard / USBHIDMouse / USBHIDAbsoluteMouse / USBHIDConsumerControl
// that hid_bridge.ino calls, with the same report layouts, so the command code is the same for both transports.
// Nothing here is device-tested yet; see #104 for the open questions (absolute pointer over BLE, app reconnection).

#pragma once

#include <BLEHIDDevice.h>
#include <BLESecurity.h>
#include "USBHIDKeyboard.h"                 // key constants (KEY_*) and KeyboardLayout_en_US, as in the USB build
#include "keyboardLayout/KeyboardLayout.h"  // SHIFT, ALT_GR, ISO_KEY: the layout table's flag bits
#include "USBHIDMouse.h"     // MOUSE_LEFT ...; hid_mouse_report_t / hid_abs_mouse_report_t come from tusb.h

#include "host/ble_store.h"  // ble_store_util_count: how many phones the board holds a bond with

extern volatile bool bleLinked;      // hid_bridge.ino: a central is connected
extern volatile uint16_t bleConn;    // ... and its connection handle

namespace blehid {

const uint8_t REPORT_KEYBOARD = 1, REPORT_MOUSE = 2, REPORT_CONSUMER = 3;
const uint16_t APPEARANCE_HID = 0x03C0;  // generic HID; iOS lists the board under Settings > Bluetooth

// Report map: the same three reports as the USB build, one id each. The mouse is relative (int8 x/y) or, with
// ABSOLUTE_POINTER, absolute (uint16 x/y in 0..32767, as USBHIDAbsoluteMouse).
static const uint8_t REPORT_MAP[] = {
    // keyboard: modifiers, reserved, 6 keys
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, REPORT_KEYBOARD,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x05, 0x07, 0x19, 0x00, 0x2A, 0xFF, 0x00, 0x81, 0x00,
    0xC0,
    // mouse: 5 buttons, x, y, wheel, pan
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, REPORT_MOUSE, 0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00, 0x25, 0x01, 0x95, 0x05, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x81, 0x01,
#if ABSOLUTE_POINTER
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x16, 0x00, 0x00, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02, 0x81, 0x02,
    0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
#else
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,
#endif
    0x05, 0x0C, 0x0A, 0x38, 0x02, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
    0xC0, 0xC0,
    // consumer control: one 10-bit usage (Home 0x0040, Search 0x0221, Globe 0x029D ...)
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, REPORT_CONSUMER,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
};

BLEHIDDevice *device = nullptr;
BLECharacteristic *keyboardIn = nullptr, *mouseIn = nullptr, *consumerIn = nullptr;
volatile int lastAuth = -1;  // status of the current connection's last encryption change (0 = ok); -1: none yet
unsigned long askedAt = 0;    // when the board last asked the phone to encrypt (loop task only)

class Security : public BLESecurityCallbacks {
  bool onSecurityRequest() override { return true; }
  bool onConfirmPIN(uint32_t) override { return true; }  // not used with "Just Works" (no input / no output)
  void onAuthenticationComplete(ble_gap_conn_desc *, int status) override { lastAuth = status; }
};

// arduino-esp32 3.3.12 (NimBLE, same on master): BLEService::addCharacteristic does not register a characteristic
// whose UUID the service already has. HID has one Input Report characteristic (0x2A4D) per report id, so only the
// keyboard's was registered: the mouse and consumer reports were missing from GATT, their service pointer was never
// set, and the first notify on them crashed the board (device 2026-10-27, core dump: BLECharacteristic::notify ->
// BLEService::getServer, from "release"). The service's map is keyed by the characteristic, so register them there.
// The map is private; an explicit instantiation may name a private member, which gives a pointer to it.
namespace detail {
template <typename Tag, typename Tag::type M> struct Expose {
  friend typename Tag::type member(Tag) { return M; }
};
struct ServiceCharacteristics {
  typedef BLECharacteristicMap BLEService::*type;
  friend type member(ServiceCharacteristics);
};
template struct Expose<ServiceCharacteristics, &BLEService::m_characteristicMap>;
}  // namespace detail

inline BLECharacteristic *inputReport(uint8_t reportId) {
  BLECharacteristic *c = device->inputReport(reportId);
  BLECharacteristicMap &map = device->hidService()->*member(detail::ServiceCharacteristics());
  bool registered = false;
  for (BLECharacteristic *p = map.getFirst(); p; p = map.getNext()) registered |= p == c;
  if (!registered) map.setByUUID(c, c->getUUID());  // a core that registers it itself: nothing to do
  return c;
}

// Called from bleBegin() on the server the command service lives on, before advertising starts.
inline void attach(BLEServer *server, BLEAdvertising *adv) {
  BLEDevice::setSecurityCallbacks(new Security());
  BLESecurity *security = new BLESecurity();  // as the library's Server_Gamepad example: kept for the program's life
  security->setAuthenticationMode(true, false, true);  // bond, no MITM (no passkey: the board has no display), SC
  security->setCapability(ESP_IO_CAP_NONE);
  security->setInitEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);
  security->setRespEncryptionKey(ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK);

  device = new BLEHIDDevice(server);
  keyboardIn = inputReport(REPORT_KEYBOARD);
  mouseIn = inputReport(REPORT_MOUSE);
  consumerIn = inputReport(REPORT_CONSUMER);
  // manufacturer() creates the characteristic; manufacturer(String) only sets an existing one (it dereferences a null
  // pointer when called first: a boot loop on the device, 2026-10-27)
  device->manufacturer()->setValue("HID Bridge");
  device->pnp(0x02, 0x303A, 0x4004, 0x0100);  // USB-IF vendor id source, Espressif VID, a PID for this firmware
  device->hidInfo(0x00, 0x02);                // not localized; normally connectable
  device->reportMap((uint8_t *)REPORT_MAP, sizeof REPORT_MAP);
  device->startServices();
  adv->setAppearance(APPEARANCE_HID);
  adv->addServiceUUID(device->hidService()->getUUID());
}

// Ready = a central is connected over an encrypted link. Reports sent otherwise are dropped by the stack.
// The link's own state is the answer, not a flag kept from callbacks: device 2026-10-27, the phone was paired and
// connected in Settings and the app linked, yet a flag set on the encryption event said "not paired".
// Not encrypted: ask the phone to encrypt (again), at most every 2 s; an iPhone that holds the bond does so at once.
inline bool ready() {
  if (!bleLinked) return false;
  ble_gap_conn_desc d;
  if (ble_gap_conn_find(bleConn, &d) != 0) return false;
  if (d.sec_state.encrypted) return true;
  if (millis() - askedAt > 2000) {
    askedAt = millis();
    ble_gap_security_initiate(bleConn);
  }
  return false;
}

// For "hidstat" and the not-paired error: what the link looks like from the board.
inline String status() {
  ble_gap_conn_desc d = {};
  bool found = bleLinked && ble_gap_conn_find(bleConn, &d) == 0;
  int bonds = -1;
  ble_store_util_count(BLE_STORE_OBJ_TYPE_OUR_SEC, &bonds);
  uint8_t txPhy = 0, rxPhy = 0;
  if (found) ble_gap_read_le_phy(bleConn, &txPhy, &rxPhy);
  return "linked=" + String(found) + " enc=" + String(found && d.sec_state.encrypted) +
         " bonded=" + String(found && d.sec_state.bonded) + " auth=" + String(lastAuth) + " bonds=" + String(bonds) + " phy=" + String(txPhy) + "/" + String(rxPhy);
}

inline bool send(BLECharacteristic *c, const void *data, size_t n) {
  if (!c || !ready()) return false;
  c->setValue((const uint8_t *)data, n);
  c->notify();
  return true;
}

}  // namespace blehid

// --- drop-in replacements for the USB classes, as far as hid_bridge.ino uses them ---

class BleHidKeyboard {
public:
  void begin() {}
  size_t press(uint8_t k) {  // same mapping as USBHIDKeyboard::press (core 3.x)
    if (k >= 0x88) {
      k -= 0x88;
    } else if (k >= 0x80) {
      report[0] |= 1 << (k - 0x80);
      k = 0;
    } else {
      k = KeyboardLayout_en_US[k];
      if (!k) return 0;
      if (k & SHIFT) {
        report[0] |= 0x02;
        k &= ~SHIFT;
      }
      if (k & ALT_GR) {
        report[0] |= 0x40;
        k &= ~ALT_GR;
      }
      if (k == ISO_REPLACEMENT) k = ISO_KEY;
    }
    return pressRaw(k);
  }
  size_t pressRaw(uint8_t k) {
    if (k >= 0xE0 && k < 0xE8) {
      report[0] |= 1 << (k - 0xE0);
    } else if (k) {
      bool present = false;
      for (int i = 2; i < 8; i++) present |= report[i] == k;
      for (int i = 2; i < 8 && !present; i++) {
        if (!report[i]) {
          report[i] = k;
          present = true;
        }
      }
      if (!present) return 0;  // six keys already down
    }
    blehid::send(blehid::keyboardIn, report, sizeof report);
    return 1;
  }
  size_t release(uint8_t k) {
    if (k >= 0x88) k -= 0x88;
    else if (k >= 0x80) { report[0] &= ~(1 << (k - 0x80)); k = 0; }
    else {
      k = KeyboardLayout_en_US[k] & ~(SHIFT | ALT_GR);
      report[0] &= ~(0x02 | 0x40);
    }
    for (int i = 2; i < 8; i++) if (k && report[i] == k) report[i] = 0;
    blehid::send(blehid::keyboardIn, report, sizeof report);
    return 1;
  }
  // Only when something is down: hid_bridge.ino calls this on every idle loop (every 20 ms), harmless over USB but a
  // notification each time over BLE -- device 2026-10-27, the flood crashed the board as soon as the link encrypted.
  void releaseAll() {
    static const uint8_t none[8] = {0};
    if (!memcmp(report, none, sizeof report)) return;
    memset(report, 0, sizeof report);
    blehid::send(blehid::keyboardIn, report, sizeof report);
  }
  size_t write(uint8_t c) {
    size_t p = press(c);
    delay(5);
    release(c);
    delay(5);
    return p;
  }
  size_t print(const String &s) {
    size_t n = 0;
    for (size_t i = 0; i < s.length(); i++) n += write((uint8_t)s[i]);
    return n;
  }

private:
  uint8_t report[8] = {0};  // modifiers, reserved, keys[6]
};

class BleHidMouse {
public:
  void begin() {}
#if ABSOLUTE_POINTER
  void move(int16_t x, int16_t y, int8_t wheel = 0, int8_t pan = 0) {
    lastX = x;
    lastY = y;
    hid_abs_mouse_report_t r = {buttons, x, y, wheel, pan};
    sendReport(r);
  }
#else
  void move(int8_t x, int8_t y, int8_t wheel = 0, int8_t pan = 0) {
    hid_mouse_report_t r = {buttons, x, y, wheel, pan};
    sendReport(r);
  }
#endif
  void press(uint8_t b = MOUSE_LEFT) { setButtons(buttons | b); }
  void release(uint8_t b = MOUSE_LEFT) { setButtons(buttons & ~b); }
  bool isPressed(uint8_t b = MOUSE_LEFT) { return (buttons & b) != 0; }
  template<typename T> bool sendReport(T report) {  // the report as laid out for USB: buttons first, then x, y ...
    return blehid::send(blehid::mouseIn, &report, sizeof report);
  }

private:
  void setButtons(uint8_t b) {
    if (b == buttons) return;
    buttons = b;
#if ABSOLUTE_POINTER
    move(lastX, lastY);
#else
    move(0, 0);
#endif
  }
  uint8_t buttons = 0;
#if ABSOLUTE_POINTER
  int16_t lastX = 0, lastY = 0;
#endif
};

class BleHidConsumer {
public:
  void begin() {}
  bool press(uint16_t usage) {
    down = usage;
    return blehid::send(blehid::consumerIn, &usage, sizeof usage);
  }
  bool release() {  // only when a key is down: called on every idle loop, as BleHidKeyboard::releaseAll
    if (!down) return true;
    down = 0;
    return blehid::send(blehid::consumerIn, &down, sizeof down);
  }

private:
  uint16_t down = 0;
};
