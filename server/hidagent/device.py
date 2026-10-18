"""TCP link to the HID bridge board (see firmware/hid_bridge).

Both ends prove they know DEVICE_KEY before any command is sent (see README, "Board protocol"):
  board  -> hello <reset reason>,wifi=<a>/<b> <uptime ms> <board nonce>   (a/b: last Wi-Fi drop reason per network)
  server -> challenge <server nonce> <HMAC(key, "server:<board nonce>:<server nonce>")>
  board  -> auth <HMAC(key, "board:<server nonce>:<board nonce>")>
  server -> ok
Then every command and reply carries a tag bound to this session and its sequence number, so a relayed handshake
gives an attacker nothing: session = HMAC(key, "session:<board nonce>:<server nonce>"),
  server -> <command> <HMAC(session, "cmd:<n>:<command>")[:32]>
  board  -> <reply> <HMAC(session, "reply:<n>:<reply>")[:32]>     (n = 0, 1, 2, … within the session)
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


MAX_LINE = 400  # the firmware drops a line over 512 bytes (with its tag); Phone.type sends long text in pieces
MAX_PENDING = 64  # unauthenticated connections at once; past it the OLDEST is dropped, so a flood cannot keep a
# new connection (the board answers within milliseconds) from being heard
MAX_PENDING_PER_PEER = 2  # per source address: extra ones are closed straight away


class Device:
    """Accepts one authenticated board connection and sends it commands, one at a time."""

    def __init__(self, key: bytes | None = None, handshake_timeout: float = 1.5):  # a real board answers in ms
        self.key, self.handshake_timeout = key, handshake_timeout
        self._reader = self._writer = None
        self._session, self._seq = b"", 0
        self._pending: dict[str, int] = {}  # source address -> handshakes in progress
        self._waiting: dict = {}  # writer of every handshake in progress, oldest first
        self._lock = asyncio.Lock()
        self._last = self._beat = 0.0
        self.links = 0  # bumped on every (re)connect, so gestures know to release and re-home

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def serve(self, host: str, port: int):
        # limit: board lines are short; a longer one raises ValueError instead of buffering 64 KiB per connection
        return await asyncio.start_server(self._accept, host, port, limit=1024)

    async def _accept(self, reader, writer):
        peer = writer.get_extra_info("peername")
        host = peer[0] if peer else "?"
        if self._pending.get(host, 0) >= MAX_PENDING_PER_PEER:
            writer.close()  # one address cannot hold more than a couple of slots
            return
        if len(self._waiting) >= MAX_PENDING:  # full (strangers from many addresses): drop the oldest, not the new
            next(iter(self._waiting)).close()
        self._pending[host] = self._pending.get(host, 0) + 1
        self._waiting[writer] = None
        try:
            hello, session = await asyncio.wait_for(self._handshake(reader, writer), self.handshake_timeout)
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError, AuthError) as e:
            # a stranger (or a board with the wrong key / old firmware) never replaces the board in use;
            # ValueError covers undecodable bytes and over-long lines
            log.warning("board connection from %s refused: %s", peer, str(e)[:120] or type(e).__name__)
            try:
                writer.write(b"err auth\n")
                writer.close()
            except OSError:
                pass
            return
        finally:
            self._waiting.pop(writer, None)
            self._pending[host] -= 1
            if not self._pending[host]:
                del self._pending[host]
        if self.connected:
            self._writer.close()
        self._reader, self._writer = reader, writer
        self._session, self._seq = session, 0
        self.links += 1
        self._last = self._beat = time.time()  # fresh link: nothing to keep awake yet
        log.info("board connected from %s %s", writer.get_extra_info("peername"), hello)

    async def _handshake(self, reader, writer) -> tuple[str, bytes]:
        """Mutual challenge-response on DEVICE_KEY. Returns ("<reset reason> <uptime ms>", session key)."""
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
        return " ".join(parts[1:3]), auth.mac(self.key, "session", board_nonce, server_nonce).encode()

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

    async def send(self, command: str) -> str:
        """Send one command and return the board's reply: "ok", or "ok <data>" (e.g. hidstat, #60). Anything else
        (an "err ..." or no reply) raises DeviceError."""
        if "\n" in command or "\r" in command:  # one line is one command: text must not smuggle in another
            raise DeviceError("a command must be a single line")
        if len(command.encode()) > MAX_LINE:
            raise DeviceError(f"command longer than {MAX_LINE} bytes")
        if not self.connected:
            raise DeviceError("board not connected")
        timeout = self._timeout(command)
        async with self._lock:
            reader, writer, session = self._reader, self._writer, self._session
            seq, self._seq = self._seq, self._seq + 1
            started = time.time()
            try:
                writer.write(f"{command} {auth.tag(session, 'cmd', seq, command)}\n".encode())
                await writer.drain()
                raw = (await asyncio.wait_for(reader.readline(), timeout)).decode().strip()
            except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                writer.close()  # the board reconnects on its own within a few seconds
                raise DeviceError(f"board link lost during '{command[:30]}': {e!r}") from e
            except asyncio.CancelledError:
                # e.g. the emergency stop: the reply would arrive late and be read as the next one's, so drop the
                # link; the board releases every button when it sees that, and reconnects
                writer.close()
                raise
            reply, _, mark = raw.rpartition(" ")
            if raw and not auth.same(mark, auth.tag(session, "reply", seq, reply)):
                writer.close()
                raise DeviceError("board reply failed authentication: link closed")
        self._beat = time.time()  # last reply of any kind
        if command != "ping":
            self._last = self._beat  # last real command: the phone's idle time
            log.debug("%s -> %s (%.0f ms)", command, reply, (time.time() - started) * 1000)
        if reply != "ok" and not reply.startswith("ok "):
            raise DeviceError(reply or "board disconnected")
        return reply

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
