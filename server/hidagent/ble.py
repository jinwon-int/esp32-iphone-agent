"""Bluetooth LE link to a board built with BLE_LINK=1 (#67), and a small command-line client to try one.

The board's BLE service carries the same byte stream as its TCP link (README, "Over Bluetooth LE"): the app writes
bytes to RX, the board notifies bytes on TX. `BleLink` turns that into the reader/writer pair `Device` already
speaks, so the handshake, tags, timeouts and error handling are the very same code as over TCP.

    python -m hidagent.ble                 # find the board, then type commands (ping, move 100 0, click, ...)
    python -m hidagent.ble --bench 50      # round-trip time of 50 pings: p50 / p90 / max

DEVICE_KEY comes from the environment, as for the server. Needs the `ble` extra (bleak) and a Bluetooth adapter.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable

from . import auth
from .bench import _pct
from .device import Device, DeviceError

SERVICE = "23c2efd6-8e20-4e1b-a71a-e26bc6d9320c"
RX = "aebdbe49-932f-4ce9-8d9d-ae64db99d2ed"  # app -> board: write
TX = "7f186178-861e-4e86-98e0-347e67454906"  # board -> app: notify

log = logging.getLogger(__name__)


class BleLink:
    """A StreamWriter-like object over GATT writes, with `reader` fed by notifications.

    `send` writes one chunk of at most `chunk_size` bytes; `disconnect` drops the connection. Call `notified` with
    each notification and `lost` when the connection is gone (it ends the reader, so a pending read fails).
    """

    def __init__(self, send: Callable[[bytes], Awaitable[None]], chunk_size: int,
                 disconnect: Callable[[], Awaitable[None]], peer=("ble", "?")):
        self.reader = asyncio.StreamReader(limit=1024)  # board lines are short, as over TCP (Device.serve)
        self._send, self._disconnect = send, disconnect
        self.chunk_size, self.peer = max(1, chunk_size), peer
        self._buffer = bytearray()
        self._lock = asyncio.Lock()  # one drain at a time keeps the chunks of a line in order
        self._closing = False
        self._closer: asyncio.Task | None = None

    # the board -> app direction
    def notified(self, data: bytes) -> None:
        if not self._closing:
            self.reader.feed_data(data)

    def lost(self) -> None:
        if not self._closing:
            self._closing = True
            self.reader.feed_eof()

    # the StreamWriter subset Device uses
    def write(self, data: bytes) -> None:
        if not self._closing:
            self._buffer += data

    async def drain(self) -> None:
        async with self._lock:
            while self._buffer and not self._closing:
                chunk = bytes(self._buffer[:self.chunk_size])
                try:
                    await self._send(chunk)
                except Exception as e:  # noqa: BLE001 - backends raise their own types; to Device it is a lost link
                    self.close()
                    raise ConnectionError(f"BLE write failed: {e!r}") from e
                del self._buffer[:len(chunk)]
            if self._closing:
                self._buffer.clear()

    def close(self) -> None:
        self.lost()
        self._buffer.clear()
        if self._closer is None:
            self._closer = asyncio.ensure_future(self._disconnect())

    def is_closing(self) -> bool:
        return self._closing

    def get_extra_info(self, name: str, default=None):
        return self.peer if name == "peername" else default


async def connect(device: Device, address: str | None = None, scan_timeout: float = 10.0):
    """Find the board (by address, or by its advertised service), connect and authenticate through `device`.

    Returns the BleakClient; raises DeviceError when no board is found or it fails the handshake.
    """
    from bleak import BleakClient, BleakScanner  # optional dependency: pip install -e ".[ble]"

    if address:
        found = await BleakScanner.find_device_by_address(address, timeout=scan_timeout)
    else:
        found = await BleakScanner.find_device_by_filter(
            lambda _d, adv: SERVICE in (u.lower() for u in adv.service_uuids), timeout=scan_timeout)
    if found is None:
        raise DeviceError(f"no board found in {scan_timeout:.0f} s (built with BLE_LINK=1, powered, not connected "
                          "to something else?)")
    link: BleLink | None = None
    client = BleakClient(found, disconnected_callback=lambda _c: link and link.lost())
    await client.connect()
    try:
        rx = client.services.get_characteristic(RX)
        if rx is None or client.services.get_characteristic(TX) is None:
            raise DeviceError("connected, but the board's link service is missing (firmware without BLE_LINK?)")
        link = BleLink(lambda chunk: client.write_gatt_char(rx, chunk, response=False),
                       max(20, rx.max_write_without_response_size), client.disconnect, peer=("ble", found.address))
        await client.start_notify(TX, lambda _c, data: link.notified(bytes(data)))  # the board says hello now
        await device.attach(link.reader, link)
        if not device.connected:
            raise DeviceError("the board did not authenticate (same DEVICE_KEY on both sides?)")
    except BaseException:
        with contextlib.suppress(Exception):
            await client.disconnect()
        raise
    return client


async def bench(device: Device, count: int) -> dict:
    """Round-trip time of `count` pings in ms: what a command costs on this link before the board does anything."""
    times = []
    for _ in range(count):
        started = time.perf_counter()
        await device.send("ping")
        times.append((time.perf_counter() - started) * 1000)
    return {"count": count, "p50_ms": round(_pct(times, 0.5), 1), "p90_ms": round(_pct(times, 0.9), 1),
            "max_ms": round(max(times), 1)}


async def repl(device: Device) -> None:
    print("connected. one command per line (ping, home, move 100 0, click, type hi, key enter, ...); "
          "an empty line quits", flush=True)
    while device.connected:
        line = (await asyncio.to_thread(sys.stdin.readline)).strip()
        if not line:
            return
        started = time.perf_counter()
        try:
            reply = await device.send(line)
        except DeviceError as e:
            reply = f"error: {e}"
        print(f"{reply}  ({(time.perf_counter() - started) * 1000:.0f} ms)", flush=True)
    print("board disconnected", flush=True)


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m hidagent.ble", description=__doc__.split("\n\n")[0])
    p.add_argument("--address", help="the board's Bluetooth address (default: the first board advertising)")
    p.add_argument("--scan-timeout", type=float, default=10.0)
    p.add_argument("--bench", type=int, metavar="N", help="send N pings, print p50 / p90 / max, and quit")
    args = p.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    key = auth.device_key()
    if key is None:
        return 2
    device = Device(key, handshake_timeout=5)  # a few BLE connection intervals per line, not a LAN round trip
    try:
        client = await connect(device, args.address, args.scan_timeout)
    except ImportError:
        print('error: needs bleak: pip install -e ".[ble]"', file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - no adapter, Bluetooth off, permission: say so instead of a traceback
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        if args.bench:
            print(await bench(device, args.bench), flush=True)
        else:
            await repl(device)
    finally:
        await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
