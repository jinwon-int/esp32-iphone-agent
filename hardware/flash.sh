#!/usr/bin/env bash
# Build and flash the HID bridge (needs arduino-cli and the esp32 core).
# Usage: [FLASH_SIZE=4M|8M|16M|32M] hardware/flash.sh [serial port] [--debug]   (arguments in any order)
#   FLASH_SIZE: the board's flash chip, default 8M (XIAO ESP32S3). 4MB boards such as the ESP32-S3-Zero or most
#   ESP32-S3 Super Minis need FLASH_SIZE=4M; the firmware itself needs about 1 MB.
#   ABSOLUTE_POINTER=1: an absolute pointer instead of a relative mouse (#14); set POINTER_MODE=absolute on the server.
# A running bridge is a TinyUSB device, so it is first kicked into the ROM
# bootloader with a 1200-baud touch; a blank board is flashed directly.
set -euo pipefail
cd "$(dirname "$0")"
FLASH_SIZE=${FLASH_SIZE:-8M}
case "$FLASH_SIZE" in
  4M|8M|16M|32M) ;;
  *) echo "FLASH_SIZE must be 4M, 8M, 16M or 32M (got '$FLASH_SIZE')" >&2; exit 2 ;;
esac
FQBN="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=$FLASH_SIZE"
# the first serial port that looks like the board, or nothing (never a failure: set -e would end the script
# silently whenever no board is plugged in, e.g. while it restarts into the bootloader below)
port() { for p in /dev/cu.usbmodem* /dev/ttyACM*; do [[ -e $p ]] && { echo "$p"; return 0; }; done; return 0; }
PORT="" DEBUG=0
for arg in "$@"; do
  case "$arg" in
    --debug) DEBUG=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) PORT=$arg ;;
  esac
done
PORT=${PORT:-$(port)}
ABSOLUTE_POINTER=${ABSOLUTE_POINTER:-0}
case "$ABSOLUTE_POINTER" in
  0|1) ;;
  *) echo "ABSOLUTE_POINTER must be 0 or 1 (got '$ABSOLUTE_POINTER')" >&2; exit 2 ;;
esac
DEFINES="-DABSOLUTE_POINTER=$ABSOLUTE_POINTER"; [[ $DEBUG == 1 ]] && DEFINES+=" -DDEBUG_SERIAL=1"
FLAGS=(--build-property "compiler.cpp.extra_flags=$DEFINES")
echo "board: $FQBN  absolute pointer: $ABSOLUTE_POINTER  port: ${PORT:-none found}"

arduino-cli compile -b "$FQBN" "${FLAGS[@]}" --output-dir build hid_bridge
if [[ -z $PORT ]]; then
  echo "built, but no board found to flash: plug it in (USB-C to this computer) or pass its serial port" >&2
  exit 1
fi
OLD=$PORT
stty -f "$PORT" 1200 2>/dev/null || stty -F "$PORT" 1200 2>/dev/null || true
for _ in $(seq 20); do sleep 0.5; PORT=$(port); [[ -n "$PORT" && "$PORT" != "$OLD" ]] && break; done
sleep 1  # let the ROM bootloader's serial come up
PORT=$(port)
PORT=${PORT:-$OLD}
arduino-cli upload -b "$FQBN" -p "$PORT" --input-dir build hid_bridge
