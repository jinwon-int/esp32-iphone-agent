"""TCP link to the HID bridge board (see firmware/hid_bridge).

Both ends prove they know DEVICE_KEY before any command is sent (see README, "Board protocol"):
  board  -> hello <reset reason> <uptime ms> <board nonce>
  server -> challenge <server nonce> <HMAC(key, "server:<board nonce>:<server nonce>")>
  board  -> auth <HMAC(key, "board:<server nonce>:<board nonce>")>
  server -> ok
"""

import asyncio
import logging
import time

from . import auth

log = logging.getLogger(__name__)


class DeviceError(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


class Device:
    """Accepts one authenticated board connection and sends it commands, one at a time."""

    def __init__(self, key: bytes | None = None, handshake_timeout: float = 3):
        self.key, self.handshake_timeout = key, handshake_timeout
        self._reader = self._writer = None
        self._lock = asyncio.Lock()
        self._last = self._beat = 0.0
        self.links = 0  # bumped on every (re)connect, so gestures know to release and re-home

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def serve(self, host: str, port: int):
        return await asyncio.start_server(self._accept, host, port)

    async def _accept(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            hello = await asyncio.wait_for(self._handshake(reader, writer), self.handshake_timeout)
        except (OSError, UnicodeDecodeError, asyncio.TimeoutError, AuthError) as e:
            # a stranger (or a board with the wrong key / old firmware) never replaces the board in use
            log.warning("board connection from %s refused: %s", peer, str(e) or type(e).__name__)
            try:
                writer.write(b"err auth\n")
                writer.close()
            except OSError:
                pass
            return
        if self.connected:
            self._writer.close()
        self._reader, self._writer = reader, writer
        self.links += 1
        self._last = self._beat = time.time()  # fresh link: nothing to keep awake yet
        log.info("board connected from %s %s", writer.get_extra_info("peername"), hello)

    async def _handshake(self, reader, writer) -> str:
        """Mutual challenge-response on DEVICE_KEY. Returns "<reset reason> <uptime ms>" from the board's hello."""
        parts = (await reader.readline()).decode().split()
        if len(parts) != 4 or parts[0] != "hello" or not auth.NONCE_RE.match(parts[3]):
            raise AuthError("no 'hello <reason> <uptime> <nonce>' (old firmware without DEVICE_KEY?)")
        if not self.key:
            raise AuthError("DEVICE_KEY is not set on the server")
        board_nonce, server_nonce = parts[3], auth.nonce()
        writer.write(f"challenge {server_nonce} {auth.mac(self.key, 'server', board_nonce, server_nonce)}\n".encode())
        await writer.drain()
        reply = (await reader.readline()).decode().split()
        expected = auth.mac(self.key, "board", server_nonce, board_nonce)
        if len(reply) != 2 or reply[0] != "auth" or not auth.same(reply[1], expected):
            raise AuthError("wrong DEVICE_KEY")
        writer.write(b"ok\n")
        await writer.drain()
        return " ".join(parts[1:3])

    async def wait_connected(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while not self.connected and time.time() < deadline:
            await asyncio.sleep(0.2)
        return self.connected

    @staticmethod
    def _timeout(command: str) -> float:
        """Seconds the board may take: Wi-Fi replies need up to ~2 s at p99, moves are paced
        (10 units per 8 ms), typing is per character."""
        name, _, arg = command.partition(" ")
        if name == "move":
            return 5 + max(abs(int(n)) for n in arg.split()) / 10 * 0.012
        if name == "type":
            return 5 + 0.1 * len(arg)
        if name == "ping":
            return 8  # an idle check: tolerate a Wi-Fi hiccup rather than drop a healthy board
        return 5

    async def send(self, command: str) -> None:
        if not self.connected:
            raise DeviceError("board not connected")
        timeout = self._timeout(command)
        async with self._lock:
            started = time.time()
            try:
                self._writer.write(command.encode() + b"\n")
                await self._writer.drain()
                reply = (await asyncio.wait_for(self._reader.readline(), timeout)).decode().strip()
            except (OSError, asyncio.TimeoutError) as e:
                self._writer.close()  # the board reconnects on its own within a few seconds
                raise DeviceError(f"board link lost during '{command[:30]}': {e!r}") from e
        self._beat = time.time()  # last reply of any kind
        if command != "ping":
            self._last = self._beat  # last real command: the phone's idle time
            log.debug("%s -> %s (%.0f ms)", command, reply, (time.time() - started) * 1000)
        if reply != "ok":
            raise DeviceError(reply or "board disconnected")

    async def keep_awake(self, idle: float = 25, heartbeat: float = 2, screen=None) -> None:
        """While idle: ping every few seconds, so a board that was unplugged or hung shows as gone within
        seconds (not at the next command), and nudge the pointer now and then so the phone does not auto-lock."""
        while True:
            await asyncio.sleep(heartbeat)
            if not self.connected or time.time() - self._beat < heartbeat:
                continue
            try:
                # nudge only a phone that is awake and mirroring: moving a sleeping phone's pointer can stall the
                # board on its USB link, and there is nothing to keep awake anyway
                if time.time() - self._last > idle and (screen is None or screen.live):
                    await self.send("move 1 0")
                    await self.send("move -1 0")
                else:
                    await self.send("ping")
            except DeviceError as e:
                log.info("board gone: %s", e)
