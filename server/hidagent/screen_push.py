"""The phone's screen, pushed by a companion iOS app over one WebSocket connection, as JPEG frames.

Why: AirPlay discovery is mDNS multicast, which does not cross a VPN (Tailscale, WireGuard), so a phone that is not
on the server's network cannot mirror to it. Instead the phone runs a small app whose broadcast extension (ReplayKit)
encodes the screen as JPEGs and sends them here, to the server's VPN address. The agent, the calibration and the web
UI see exactly the interface of the AirPlay `Screen` (`frame`, `updated`, `live`, `aspect`, `settled`, `fresh`,
`start`, `stop`); nothing downstream knows which source is in use.

Transport: a plain WebSocket (RFC 6455) served directly on asyncio -- no HTTP framework in front of it, on its own
address, port and token, apart from the web UI. One binary message = one JPEG. The app proves the token once, in the
upgrade request (`Authorization: Bearer …`, never in the URL: URLs end up in logs). Nothing clever on purpose.

Protection, in the order a frame meets it: strangers get a short handshake window and a per-address cap (as the board
port does); a frame header announcing more than `max_frame` bytes closes the connection before anything is read;
bytes that are not a JPEG (SOI … EOI) are dropped; frames beyond `max_fps` are dropped, not disconnected. A new,
authenticated connection *replaces* the old one rather than being refused: a "refuse new connections" cap is exactly
how an idle stale socket would lock the real phone out after a network change.

Liveness: ReplayKit sends nothing while the screen is still, so the app re-sends its last JPEG (unchanged bytes) or a
WebSocket ping every second or two. A re-sent identical frame counts as "seen" but not as a change, so `settled()`
and `fresh()` keep their AirPlay meaning (only changes move `updated`).
"""

import asyncio
import base64
import hashlib
import ipaddress
import logging
import time

from . import auth
from .screen import Frames

log = logging.getLogger(__name__)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10
CLOSE_NORMAL, CLOSE_PROTOCOL, CLOSE_TOO_BIG = 1000, 1002, 1009

MAX_HEADER = 8192  # the upgrade request; the app sends a handful of short headers
MAX_PENDING_PER_PEER, MAX_PENDING = 2, 16  # connections still in the handshake (strangers); authenticated ones replace
HANDSHAKE_TIMEOUT = 3.0
DEFAULT_PORT = 8891
WILDCARDS = ("", "*", "0.0.0.0", "::", "[::]")
# Tailscale hands out 100.64.0.0/10 (CGNAT range) and fd7a:115c:a1e0::/48; WireGuard setups commonly use RFC 1918
_VPN_LIKE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"),
             ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


class PushAuthError(RuntimeError):
    pass


def check_host(host: str) -> str:
    """The bind address, after refusing a wildcard: the listener must sit on the VPN address only.

    The firewall is not relied on (both the VPN and the LAN/public interface exist on the same machine).
    """
    host = (host or "").strip()
    if host in WILDCARDS:
        raise ValueError("PUSH_HOST must be this computer's VPN address (e.g. its Tailscale 100.x address); "
                         "a wildcard would also listen on the LAN and the internet")
    return host


def vpn_like(host: str) -> bool:
    """Whether `host` looks like a VPN or loopback address (a warning helps, a block would be guessing)."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return any(ip in net for net in _VPN_LIKE)


def unmask(payload: bytes, mask: bytes) -> bytes:
    """XOR the client's masked payload with its 4-byte key (as one big integer: C speed, no per-byte loop)."""
    n = len(payload)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(key, "little")).to_bytes(n, "little")


def frame_bytes(opcode: int, payload: bytes = b"") -> bytes:
    """A server-to-client frame (never masked): FIN + opcode, then the 7/16/64-bit length form."""
    n = len(payload)
    if n < 126:
        head = bytes((0x80 | opcode, n))
    elif n < 1 << 16:
        head = bytes((0x80 | opcode, 126)) + n.to_bytes(2, "big")
    else:
        head = bytes((0x80 | opcode, 127)) + n.to_bytes(8, "big")
    return head + payload


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


class PushScreen(Frames):
    """Listens on the VPN address for the app's WebSocket; keeps the latest JPEG it sends."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, token: str = "", *, max_frame: int = 1 << 20,
                 max_fps: float = 15, stale: float = 5.0):
        super().__init__()
        self.host, self.port, self.token = check_host(host), int(port), token
        if len(token) < auth.MIN_UI_TOKEN:
            raise ValueError(f"PUSH_TOKEN must be at least {auth.MIN_UI_TOKEN} characters")
        self.max_frame, self.max_fps, self.stale = int(max_frame), float(max_fps), float(stale)
        self._server = None
        self._task = None
        self._writer = None  # the authenticated connection in use (one at a time)
        self._seen = 0.0  # last frame, re-sent frame or ping on that connection
        self._accepted_at = 0.0  # for the frame-rate cap
        self._pending: dict[str, int] = {}
        self._waiting: dict = {}
        self.links = 0  # authenticated connections so far
        self.dropped = 0  # frames refused after authentication (not JPEG, over the rate)
        self._stopping = False

    # -- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        """Called from the app's lifespan, inside the running loop (like Screen.start, but a coroutine underneath)."""
        self._task = asyncio.get_running_loop().create_task(self.serve())

    async def serve(self):
        # limit: bounds the upgrade request; frame payloads are read with readexactly after their length is checked
        self._server = await asyncio.start_server(self._accept, self.host, self.port, limit=MAX_HEADER)
        log.info("push screen listening on ws://%s:%d/ (VPN address%s)", self.host, self.port,
                 "" if vpn_like(self.host) else " -- WARNING: this does not look like a VPN or loopback address")
        return self._server

    def stop(self) -> None:
        self._stopping = True
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
        if self._task is not None:
            self._task.cancel()

    @property
    def live(self) -> bool:
        connected = self._writer is not None and not self._writer.is_closing()
        return connected and self.frame is not None and time.time() - max(self.updated, self._seen) < self.stale

    # -- one connection ------------------------------------------------------------
    async def _accept(self, reader, writer) -> None:
        peer = writer.get_extra_info("peername")
        host = peer[0] if peer else "?"
        if self._pending.get(host, 0) >= MAX_PENDING_PER_PEER:
            writer.close()
            return
        if len(self._waiting) >= MAX_PENDING:  # drop the oldest stranger, never the newcomer
            next(iter(self._waiting)).close()
        self._pending[host] = self._pending.get(host, 0) + 1
        self._waiting[writer] = None
        try:
            await asyncio.wait_for(self._handshake(reader, writer), HANDSHAKE_TIMEOUT)
        except PushAuthError as e:
            log.warning("push connection from %s refused: %s", host, e)
            await self._reject(writer, "401 Unauthorized")
            return
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError) as e:
            log.warning("push connection from %s refused: %s", host, str(e)[:120] or type(e).__name__)
            await self._reject(writer, "400 Bad Request")
            return
        finally:
            self._waiting.pop(writer, None)
            self._pending[host] -= 1
            if not self._pending[host]:
                del self._pending[host]
        previous, self._writer = self._writer, writer
        if previous is not None and not previous.is_closing():
            log.info("push: new connection from %s replaces the previous one", host)
            previous.close()
        self.links += 1
        self._seen = time.time()
        log.info("push: phone connected from %s", host)
        try:
            await self._receive(reader, writer)
        except (OSError, ValueError, asyncio.IncompleteReadError) as e:
            log.info("push: connection from %s ended: %s", host, str(e)[:120] or type(e).__name__)
        finally:
            if self._writer is writer:  # not when a newer connection already took over
                self._writer, self.frame = None, None
            writer.close()

    async def _handshake(self, reader, writer) -> None:
        """Read the HTTP upgrade request, check the token, answer 101. Anything else raises."""
        raw = await reader.readuntil(b"\r\n\r\n")  # LimitOverrunError beyond MAX_HEADER
        request, _, rest = raw.decode("latin-1").partition("\r\n")
        method, _path, version = (request.split(" ") + ["", ""])[:3]
        if method != "GET" or not version.startswith("HTTP/1."):
            raise ValueError(f"not a websocket upgrade: {request[:60]!r}")
        headers: dict[str, str] = {}
        for line in rest.split("\r\n"):
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        if (headers.get("upgrade", "").lower() != "websocket" or headers.get("sec-websocket-version") != "13"
                or not headers.get("sec-websocket-key")):
            raise ValueError("not a websocket upgrade")
        header = headers.get("authorization", "")
        given = header[7:] if header[:7].lower() == "bearer " else ""
        if not given or not auth.same(given, self.token):
            raise PushAuthError("wrong or missing push token")
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(headers['sec-websocket-key'])}\r\n\r\n").encode())
        await writer.drain()

    @staticmethod
    async def _reject(writer, status: str) -> None:
        try:
            writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode())
            await writer.drain()
        except (OSError, ConnectionError):
            pass
        writer.close()

    async def _receive(self, reader, writer) -> None:
        """Frames until the connection ends: binary messages are JPEGs, pings keep it alive, close closes."""
        message, message_op = b"", None
        while True:
            head = await reader.readexactly(2)
            fin, opcode, masked, length = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x80, head[1] & 0x7F
            if length == 126:
                length = int.from_bytes(await reader.readexactly(2), "big")
            elif length == 127:
                length = int.from_bytes(await reader.readexactly(8), "big")
            if not masked:
                await self._close(writer, CLOSE_PROTOCOL, "client frames must be masked")
                return
            if opcode >= OP_CLOSE and (not fin or length > 125):
                await self._close(writer, CLOSE_PROTOCOL, "bad control frame")
                return
            if length > self.max_frame or len(message) + length > self.max_frame:
                await self._close(writer, CLOSE_TOO_BIG, f"frame larger than {self.max_frame} bytes")
                return
            mask = await reader.readexactly(4)
            payload = unmask(await reader.readexactly(length), mask)
            if opcode == OP_PING:
                writer.write(frame_bytes(OP_PONG, payload))
                await writer.drain()
                self._seen = time.time()
            elif opcode == OP_PONG:
                self._seen = time.time()
            elif opcode == OP_CLOSE:
                await self._close(writer, CLOSE_NORMAL, "")
                return
            elif opcode in (OP_BINARY, OP_TEXT):
                if message_op is not None:
                    await self._close(writer, CLOSE_PROTOCOL, "new message inside a fragmented one")
                    return
                message, message_op = payload, opcode
            elif opcode == OP_CONT:
                if message_op is None:
                    await self._close(writer, CLOSE_PROTOCOL, "continuation without a message")
                    return
                message += payload
            else:
                await self._close(writer, CLOSE_PROTOCOL, f"unknown opcode {opcode}")
                return
            if opcode in (OP_BINARY, OP_TEXT, OP_CONT) and fin:
                if message_op == OP_BINARY:
                    self._frame(message)
                else:
                    self._seen = time.time()  # text is a keepalive at most; its content is not used
                message, message_op = b"", None

    def _frame(self, data: bytes) -> None:
        now = time.time()
        self._seen = now
        if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
            self.dropped += 1
            return
        if data == self.frame:  # the app's keepalive: the same picture again, not a change
            return
        if now - self._accepted_at < 1 / self.max_fps:
            self.dropped += 1
            return
        self.frame, self.updated, self._accepted_at = data, now, now

    @staticmethod
    async def _close(writer, code: int, reason: str) -> None:
        try:
            writer.write(frame_bytes(OP_CLOSE, code.to_bytes(2, "big") + reason.encode()[:120]))
            await writer.drain()
        except (OSError, ConnectionError):
            pass
        writer.close()
