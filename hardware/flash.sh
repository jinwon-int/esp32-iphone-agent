#!/usr/bin/env bash
# Build and flash the HID bridge (needs arduino-cli and the esp32 core), or flash an image built earlier.
# Usage: [FLASH_SIZE=4M|8M|16M|32M] hardware/flash.sh [serial port] [options]   (arguments in any order)
#   --debug               build with DEBUG_SERIAL=1: the board echoes each command and its duration over USB serial
#   --image PATH          do not build: write an image built earlier, with esptool (#106). A `*.merged.bin` (bootloader
#                         + partitions + app, as arduino-cli leaves it in the output dir) goes to 0x0 and erases the
#                         whole flash, NVS included -- a BLE_HID board loses its bonds and must be paired again. Any
#                         other `.bin` is the app alone and goes to 0x10000, so NVS (bonds) stays.
#   --wait[=SECONDS]      no board on USB yet: wait for one (default 1800 s) instead of giving up
#   --expect-flash SIZE   with --image: read the chip's flash size first (esptool flash-id) and stop if it is not SIZE
#                         (4M|8M|16M|32M). Defaults to FLASH_SIZE, so an image built for 8M never lands on a 4M board.
#   --dry-run             print the plan (tools, image, offset, port) and exit without touching a board; CI runs this
#   FLASH_SIZE: the board's flash chip, default 8M (XIAO ESP32S3). 4MB boards such as the ESP32-S3-Zero or most
#   ESP32-S3 Super Minis need FLASH_SIZE=4M; the firmware itself needs about 1 MB.
#   ABSOLUTE_POINTER=1: an absolute pointer instead of a relative mouse (#14); set POINTER_MODE=absolute on the server.
#   BLE_LINK=1: the controller is an app on the phone over Bluetooth LE instead of the server over Wi-Fi (no Wi-Fi at
#   all; secrets.h needs only DEVICE_KEY). See README, "Board protocol".
#   BLE_HID=1 (with BLE_LINK=1): mouse + keyboard over Bluetooth LE too (#104), so the board need not be plugged into
#   the phone, only powered near it. Pair it once in Settings > Bluetooth.
# A running bridge is a USB device (TinyUSB, or USB-Serial/JTAG with BLE_HID), so it is first kicked into the ROM
# bootloader with a 1200-baud touch; a blank board is flashed directly.
set -euo pipefail
cd "$(dirname "$0")"
FLASH_SIZE=${FLASH_SIZE:-8M}
check_size() {
  case "$2" in
    4M|8M|16M|32M) ;;
    *) echo "$1 must be 4M, 8M, 16M or 32M (got '$2')" >&2; exit 2 ;;
  esac
}
check_size FLASH_SIZE "$FLASH_SIZE"
FQBN="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=$FLASH_SIZE"
# the first serial port that looks like the board, or nothing (never a failure: set -e would end the script
# silently whenever no board is plugged in, e.g. while it restarts into the bootloader below)
port() { for p in /dev/cu.usbmodem* /dev/ttyACM*; do [[ -e $p ]] && { echo "$p"; return 0; }; done; return 0; }
PORT="" DEBUG=0 IMAGE="" WAIT=0 WAIT_SECONDS=1800 EXPECT="" DRY=0
while (($#)); do
  case "$1" in
    --debug) DEBUG=1 ;;
    --dry-run) DRY=1 ;;
    --wait) WAIT=1 ;;
    --wait=*) WAIT=1; WAIT_SECONDS=${1#--wait=} ;;
    --image) IMAGE=${2:?--image needs a path}; shift ;;
    --image=*) IMAGE=${1#--image=} ;;
    --expect-flash) EXPECT=${2:?--expect-flash needs 4M, 8M, 16M or 32M}; shift ;;
    --expect-flash=*) EXPECT=${1#--expect-flash=} ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) PORT=$1 ;;
  esac
  shift
done
[[ $WAIT_SECONDS =~ ^[0-9]+$ ]] || { echo "--wait needs a number of seconds (got '$WAIT_SECONDS')" >&2; exit 2; }
[[ -n $EXPECT ]] && check_size --expect-flash "$EXPECT"
if [[ -n $IMAGE ]]; then
  [[ -f $IMAGE ]] || { echo "--image: no such file: $IMAGE" >&2; exit 2; }
  [[ $DEBUG == 1 ]] && { echo "--debug is a build option; it does nothing with --image" >&2; exit 2; }
  EXPECT=${EXPECT:-$FLASH_SIZE}
elif [[ -n $EXPECT ]]; then
  echo "--expect-flash works with --image (the build path uploads through arduino-cli; see docs/flashing-troubleshooting.md)" >&2
  exit 2
fi
PORT=${PORT:-$(port)}
ABSOLUTE_POINTER=${ABSOLUTE_POINTER:-0}
case "$ABSOLUTE_POINTER" in
  0|1) ;;
  *) echo "ABSOLUTE_POINTER must be 0 or 1 (got '$ABSOLUTE_POINTER')" >&2; exit 2 ;;
esac
BLE_LINK=${BLE_LINK:-0}
case "$BLE_LINK" in
  0|1) ;;
  *) echo "BLE_LINK must be 0 or 1 (got '$BLE_LINK')" >&2; exit 2 ;;
esac
BLE_HID=${BLE_HID:-0}
case "$BLE_HID" in
  0) ;;
  1) [[ $BLE_LINK == 1 ]] || { echo "BLE_HID=1 needs BLE_LINK=1" >&2; exit 2; } ;;
  *) echo "BLE_HID must be 0 or 1 (got '$BLE_HID')" >&2; exit 2 ;;
esac

# esptool 5 (the esp32 core ships one; `esptool`/`esptool.py` on PATH wins). Only --image needs it.
esptool_bin() {
  local e d
  for e in esptool esptool.py; do command -v "$e" && return 0; done
  for d in "${ARDUINO_DIRECTORIES_DATA:-}" "$HOME/.arduino15" "$HOME/Library/Arduino15"; do
    [[ -n $d ]] || continue
    e=$(find "$d"/packages/esp32/tools/esptool_py -maxdepth 2 -name esptool -type f 2>/dev/null | sort -V | tail -n 1) || true
    [[ -n $e ]] && { echo "$e"; return 0; }
  done
  return 1
}
sha() { shasum -a 256 "$1" 2>/dev/null || sha256sum "$1"; }

if [[ -n $IMAGE ]]; then
  ESPTOOL=$(esptool_bin) || { echo "esptool not found: install the esp32 core (arduino-cli core install esp32:esp32) or pip install esptool" >&2; exit 1; }
  case "$IMAGE" in
    *.merged.bin) OFFSET=0x0; WHAT="full image (erases NVS: a BLE_HID board must be paired again)" ;;
    *) OFFSET=0x10000; WHAT="app only (NVS, bonds and settings stay)" ;;
  esac
  echo "image: $IMAGE  sha256: $(sha "$IMAGE" | cut -c1-16)  -> $OFFSET  $WHAT"
  echo "esptool: $ESPTOOL  expect flash: $EXPECT  port: ${PORT:-none yet}$([[ $WAIT == 1 ]] && echo " (wait up to $WAIT_SECONDS s)")"
else
  DEFINES="-DABSOLUTE_POINTER=$ABSOLUTE_POINTER -DBLE_LINK=$BLE_LINK -DBLE_HID=$BLE_HID"; [[ $DEBUG == 1 ]] && DEFINES+=" -DDEBUG_SERIAL=1"
  FLAGS=(--build-property "compiler.cpp.extra_flags=$DEFINES")
  echo "board: $FQBN  absolute pointer: $ABSOLUTE_POINTER  link: $([[ $BLE_LINK == 1 ]] && echo ble || echo wifi)  hid: $([[ $BLE_HID == 1 ]] && echo ble || echo usb)  port: ${PORT:-none found}"
fi
[[ $DRY == 1 ]] && { echo "dry run: nothing flashed"; exit 0; }

if [[ -z $IMAGE ]]; then
  arduino-cli compile -b "$FQBN" "${FLAGS[@]}" --output-dir build hid_bridge
fi
if [[ -z $PORT && $WAIT == 1 ]]; then
  echo "waiting up to $WAIT_SECONDS s for a board on USB…"
  end=$((SECONDS + WAIT_SECONDS))
  until PORT=$(port); [[ -n $PORT ]]; do
    ((SECONDS < end)) || { echo "no board showed up in $WAIT_SECONDS s" >&2; exit 1; }
    sleep 1
  done
  echo "board on $PORT"
  sleep 2  # let its USB settle
fi
if [[ -z $PORT ]]; then
  echo "${IMAGE:+image ready, }built, but no board found to flash: plug it in (USB-C to this computer), pass its serial port, or --wait" >&2
  exit 1
fi
OLD=$PORT
stty -f "$PORT" 1200 2>/dev/null || stty -F "$PORT" 1200 2>/dev/null || true
for _ in $(seq 20); do sleep 0.5; PORT=$(port); [[ -n "$PORT" && "$PORT" != "$OLD" ]] && break; done
sleep 1  # let the ROM bootloader's serial come up
PORT=$(port)
PORT=${PORT:-$OLD}

if [[ -z $IMAGE ]]; then
  arduino-cli upload -b "$FQBN" -p "$PORT" --input-dir build hid_bridge
  exit 0
fi
# the same esptool sequence as the hand-written node scripts it replaces (#106): flash-id (stays in the stub) ->
# write-flash (no reset in between) -> read-mac, then a watchdog reset, which also works on USB-Serial/JTAG
INFO=$("$ESPTOOL" --chip esp32s3 -p "$PORT" --before default-reset --after no-reset flash-id 2>&1) || { echo "$INFO" >&2; exit 1; }
echo "$INFO" | grep -E 'MAC:|Detected flash size|Features' || true
DETECTED=$(echo "$INFO" | sed -n 's/.*Detected flash size: *\([0-9]*\)MB.*/\1M/p' | tail -n 1)
if [[ $DETECTED != "$EXPECT" ]]; then
  echo "flash size: the chip reports '${DETECTED:-unknown}', the image and --expect-flash say $EXPECT: not written (a wrong size boot-loops; see docs/flashing-troubleshooting.md)" >&2
  exit 3
fi
"$ESPTOOL" --chip esp32s3 -p "$PORT" --before no-reset --after no-reset write-flash -z "$OFFSET" "$IMAGE"
sleep 1
PORT=$(port); PORT=${PORT:-$OLD}
"$ESPTOOL" --chip esp32s3 -p "$PORT" --before no-reset --after watchdog-reset read-mac 2>&1 | grep -E 'MAC:|resetting' || true
echo "flashed $IMAGE ($(sha "$IMAGE" | cut -c1-16)) at $OFFSET on a ${DETECTED} board"
