# Flashing: troubleshooting & verification

A short, machine-independent companion to [Quick start → Flash the board](../README.md#1-flash-the-board).
Everything here works from any computer that has the board on USB; nothing depends on a particular host.

## Match `FLASH_SIZE` to the board's flash chip

`FLASH_SIZE` defaults to `8M` (the XIAO ESP32S3). A 4 MB board — the ESP32-S3-Zero and most ESP32-S3 Super
Minis — **compiles and uploads fine at 8M but then crashes on every boot** and never joins Wi‑Fi. On the serial
console (`--debug` build, or any serial monitor) you'll see:

```
E (197) spi_flash: Detected size(4096k) smaller than the size in the binary image header..
assert failed: __esp_system_init_fn_init_flash startup_funcs.c
```

Reflash with the real size:

```bash
FLASH_SIZE=4M hardware/flash.sh [port]
```

Not sure of the size? `esptool --port <port> flash-id` prints the detected flash chip size.

## "Permission denied" opening the serial port

```
A fatal error occurred: Could not open /dev/ttyACM0 ... Permission denied: '/dev/ttyACM0'
```

means your user cannot open the serial device. Add it to the group that owns the port (usually `dialout`), then
use a shell that has the new group:

```bash
sudo usermod -aG dialout "$USER"          # persistent; takes effect on next login
sg dialout -c 'FLASH_SIZE=4M hardware/flash.sh [port]'   # or just log out and back in
```

## Verify a flash without the phone

The board needs only power and Wi‑Fi to reach the server, so a good flash can be confirmed before the phone is
involved: power the board from any USB port and start the server (`python -m hidagent`). A healthy board appears
in the server log and holds an established TCP connection to the device port:

```
hidagent.device board connected from ('<board-ip>', <port>) usb <n>
```
```bash
ss -tnp | grep :7300     # ESTAB <server-ip>:7300  <board-ip>:<port>
```

If the board connects and is **not** dropped, its firmware, the Wi‑Fi credentials and the `DEVICE_KEY` in
`secrets.h` are all correct — the server refuses a board whose `DEVICE_KEY` does not match the one in `server/.env`.

## `secrets.h` fields at a glance

| Field | Required | Purpose |
|---|---|---|
| `WIFI_SSID`, `WIFI_PASS` | yes | 2.4 GHz Wi‑Fi the board joins |
| `SERVER_HOST`, `SERVER_PORT` | yes | the computer running `python -m hidagent` |
| `DEVICE_KEY` | yes | shared secret; must equal `DEVICE_KEY` in `server/.env` |
| `WIFI_SSID2` | optional | a second Wi‑Fi (e.g. a phone hotspot) |
| `WG_*` | optional | route the link through a WireGuard tunnel so the board reaches the server from any Wi‑Fi |
| `SERVER_HOST_HOME` | optional | with `WG_*` set, skip the tunnel on the first (home) Wi‑Fi and connect directly |

`secrets.h` is git-ignored. Keep real Wi‑Fi passwords, device keys and tunnel keys out of commits; copy
`hardware/hid_bridge/secrets.example.h` and fill it in locally.
