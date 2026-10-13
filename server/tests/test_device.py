import asyncio

import pytest

from hidagent import auth
from hidagent.device import MAX_PENDING_PER_PEER, Device, DeviceError


def test_timeout_grows_with_move_distance_and_text_length():
    assert Device._timeout("click") == 5
    assert Device._timeout("move 1000 -20") > Device._timeout("move 10 10") >= 5
    assert Device._timeout("type " + "x" * 40) > Device._timeout("type hi")


def test_send_without_board_fails_fast():
    with pytest.raises(DeviceError, match="not connected"):
        asyncio.run(Device().send("ping"))


KEY = b"0123456789abcdef0123456789abcdef"


async def connect_board(port: int, key: bytes = KEY, hello: str | None = None, check_server: bool = True):
    """Open a connection and run the board's half of the handshake; returns (reader, writer, server's last line)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    board_nonce = auth.nonce()
    writer.write((hello or f"hello power-on 1234 {board_nonce}").encode() + b"\n")
    await writer.drain()
    line = (await reader.readline()).decode().split()
    if len(line) != 3 or line[0] != "challenge":
        return reader, writer, " ".join(line)
    server_nonce, proof = line[1], line[2]
    if check_server and proof != auth.mac(key, "server", board_nonce, server_nonce):  # real firmware hangs up here
        return reader, writer, "server proof rejected"
    writer.write(f"auth {auth.mac(key, 'board', server_nonce, board_nonce)}\n".encode())
    await writer.drain()
    return reader, writer, (await reader.readline()).decode().strip()


async def serve(key: bytes | None = KEY):
    device = Device(key, handshake_timeout=1)
    server = await device.serve("127.0.0.1", 0)
    return device, server, server.sockets[0].getsockname()[1]


def session_of(key: bytes, board_nonce: str, server_nonce: str) -> bytes:
    return auth.mac(key, "session", board_nonce, server_nonce).encode()


class FakeBoard:
    """The firmware's logic in Python: handshake, then only commands with a valid tag for this session."""

    def __init__(self, reader, writer, key: bytes = KEY):
        self.reader, self.writer, self.key = reader, writer, key
        self.session, self.seq, self.executed = b"", 0, []

    async def handshake(self) -> bool:
        board_nonce = auth.nonce()
        self.writer.write(f"hello power-on 1234 {board_nonce}\n".encode())
        await self.writer.drain()
        line = (await self.reader.readline()).decode().split()
        if len(line) != 3 or line[0] != "challenge":
            return False
        if not auth.same(line[2], auth.mac(self.key, "server", board_nonce, line[1])):
            return False
        self.writer.write(f"auth {auth.mac(self.key, 'board', line[1], board_nonce)}\n".encode())
        await self.writer.drain()
        if (await self.reader.readline()).strip() != b"ok":
            return False
        self.session, self.seq = session_of(self.key, board_nonce, line[1]), 0
        return True

    async def serve(self, reply=lambda command: "ok" if command != "bad" else "err unknown command"):
        while line := (await self.reader.readline()).decode().strip():
            command, _, mark = line.rpartition(" ")
            if not self.session or not auth.same(mark, auth.tag(self.session, "cmd", self.seq, command)):
                self.writer.close()  # hang up, never execute
                return
            self.executed.append(command)
            answer = reply(command)
            self.writer.write(f"{answer} {auth.tag(self.session, 'reply', self.seq, answer)}\n".encode())
            await self.writer.drain()
            self.seq += 1


async def board_connected(port: int, key: bytes = KEY) -> FakeBoard:
    board = FakeBoard(*await asyncio.open_connection("127.0.0.1", port), key=key)
    assert await board.handshake()
    return board


def test_round_trip_with_a_fake_board():
    async def scenario():
        device, server, port = await serve()
        board = await board_connected(port)
        task = asyncio.create_task(board.serve())
        assert await device.wait_connected(2)
        await device.send("ping")
        with pytest.raises(DeviceError, match="unknown command"):
            await device.send("bad")
        await device.send("type hello world ")  # text with spaces, even a trailing one, survives the tag
        assert board.executed == ["ping", "bad", "type hello world "]
        assert device.links == 1
        task.cancel()
        board.writer.close()
        server.close()

    asyncio.run(scenario())


def test_board_with_wrong_key_is_refused():  # an attacker ignores the server's proof and answers anyway
    async def scenario():
        device, server, port = await serve()
        _, writer, result = await connect_board(port, key=b"another-key-another-key-another!!", check_server=False)
        assert result == "err auth"
        assert not device.connected
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_board_refuses_a_server_with_another_key():
    async def scenario():
        device, server, port = await serve(b"not-the-board-key-not-the-board!")
        _, writer, result = await connect_board(port)
        assert result == "server proof rejected"
        writer.close()
        server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("hello", ["hello power-on 1234", "ping", "hello a b NOT-HEX-NONCE"])
def test_old_firmware_or_junk_is_refused(hello):
    async def scenario():
        device, server, port = await serve()
        _, writer, result = await connect_board(port, hello=hello)
        assert result == "err auth" and not device.connected
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_server_without_key_refuses_every_board():
    async def scenario():
        device, server, port = await serve(key=None)
        _, writer, result = await connect_board(port)
        assert result == "err auth" and not device.connected
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_silent_connection_times_out_and_is_refused():
    async def scenario():
        device, server, port = await serve()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert (await asyncio.wait_for(reader.readline(), 3)) == b"err auth\n"
        assert not device.connected
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_stranger_cannot_kick_the_board_in_use():
    async def scenario():
        device, server, port = await serve()
        _, board_writer, result = await connect_board(port)
        assert result == "ok" and device.connected
        _, stranger, result = await connect_board(port, key=b"stranger-key-stranger-key-strang", check_server=False)
        assert result == "err auth"
        assert device.connected and device.links == 1 and not device._writer.is_closing()
        stranger.close()
        board_writer.close()
        server.close()

    asyncio.run(scenario())


def test_relayed_handshake_gives_an_attacker_no_commands():
    """Review M1: an attacker who captures the board's connection relays the handshake to the real server, then
    tries to inject commands. Without the session key it cannot tag them, so the board hangs up and runs nothing."""
    async def scenario():
        device, server, port = await serve()
        to_board = asyncio.Queue()  # what the "attacker" sends towards the board

        class Pipe:  # the board's view of its connection: reads come from the attacker
            async def readline(self):
                return await to_board.get()

        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
        sent_by_board = []

        class Tap:
            def write(self, data):
                sent_by_board.append(data)
                upstream_writer.write(data)  # relay board -> real server

            async def drain(self):
                await upstream_writer.drain()

            def close(self):
                to_board.put_nowait(b"")

        board = FakeBoard(Pipe(), Tap())

        async def relay_server_to_board():
            while line := await upstream_reader.readline():
                await to_board.put(line)
                if line.strip() == b"ok":
                    return

        relay = asyncio.create_task(relay_server_to_board())
        assert await board.handshake()  # both sides are satisfied: the relay worked
        await relay
        serving = asyncio.create_task(board.serve())
        for forged in (b"type attacker\n", b"key cmd+h " + b"0" * 32 + b"\n"):
            await to_board.put(forged)
        await asyncio.wait_for(serving, 2)
        assert board.executed == []  # nothing ran
        upstream_writer.close()
        server.close()

    asyncio.run(scenario())


def test_reply_with_a_wrong_tag_closes_the_link():
    async def scenario():
        device, server, port = await serve()
        board = await board_connected(port)

        async def forger():  # answers "ok" but tagged for the wrong sequence number (a replayed reply)
            line = (await board.reader.readline()).decode().strip()
            command, _, _ = line.rpartition(" ")
            board.writer.write(f"ok {auth.tag(board.session, 'reply', 7, 'ok')}\n".encode())
            await board.writer.drain()

        task = asyncio.create_task(forger())
        assert await device.wait_connected(2)
        with pytest.raises(DeviceError, match="failed authentication"):
            await device.send("ping")
        assert not device.connected
        await task
        board.writer.close()
        server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["type a\nkey cmd+h", "type a\rb", "type " + "x" * 500])
def test_commands_cannot_smuggle_a_second_line_or_be_too_long(command):
    with pytest.raises(DeviceError, match="single line|longer than"):
        asyncio.run(Device(KEY).send(command))


def test_long_text_is_typed_in_pieces():
    from hidagent.phone import Phone

    class Recorder:
        sent = []

        async def send(self, command):
            self.sent.append(command)

    phone = Phone.__new__(Phone)
    phone.device = Recorder()
    asyncio.run(phone.type("x" * 450))
    assert [len(c) for c in phone.device.sent] == [205, 205, 55]


def test_over_long_hello_is_refused_without_an_unhandled_error(caplog):
    """Review L1: a line over the stream limit used to escape the handler with a traceback."""
    async def scenario():
        device, server, port = await serve()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"hello " + b"x" * 70_000 + b"\n")  # over the old 64 KiB default too
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), 3) in (b"err auth\n", b"")
        await asyncio.sleep(0.1)
        assert not device.connected
        writer.close()
        server.close()

    asyncio.run(scenario())
    assert "Unhandled exception" not in caplog.text and "refused" in caplog.text


def test_too_many_pending_handshakes_from_one_peer_are_closed_at_once():
    """Review L2: silent strangers cannot pile up; past the per-peer cap they are closed immediately."""
    async def scenario():
        device, server, port = await serve()
        device.handshake_timeout = 5
        held = [await asyncio.open_connection("127.0.0.1", port) for _ in range(MAX_PENDING_PER_PEER)]
        await asyncio.sleep(0.1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(reader.read(100), 1) == b""  # closed straight away, not after 5 s
        for _, w in held:
            w.close()
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_idle_strangers_cannot_lock_the_board_out():
    """Re-review N1: 8 idle sockets used to fill every pending slot, so the real board could never reconnect."""
    async def scenario():
        device, server, port = await serve()
        device.handshake_timeout = 5
        squatters = []
        for _ in range(12):  # the attacker keeps (re)opening idle connections from its address
            squatters.append(await asyncio.open_connection("127.0.0.1", port))
        await asyncio.sleep(0.1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=("127.0.0.2", 0))
        board = FakeBoard(reader, writer)
        assert await asyncio.wait_for(board.handshake(), 2)  # the board, from its own address, still gets in
        assert await device.wait_connected(1)
        for _, w in squatters:
            w.close()
        writer.close()
        server.close()

    asyncio.run(scenario())


def test_cancelled_command_drops_the_link_instead_of_desynchronising():
    """Re-review N2: a command cancelled mid-flight (emergency stop) left its late reply to be read by the next."""
    async def scenario():
        device, server, port = await serve()
        board = await board_connected(port)

        async def slow(command):
            await asyncio.sleep(0.5)
            return "ok"

        async def serve_slowly():  # like board.serve, but the reply comes late
            line = (await board.reader.readline()).decode().strip()
            command, _, _ = line.rpartition(" ")
            await slow(command)
            board.writer.write(f"ok {auth.tag(board.session, 'reply', 0, 'ok')}\n".encode())

        task = asyncio.create_task(serve_slowly())
        assert await device.wait_connected(2)
        sending = asyncio.create_task(device.send("move 500 0"))
        await asyncio.sleep(0.1)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert not device.connected  # dropped: the board releases its buttons and reconnects with a fresh session
        task.cancel()
        board.writer.close()
        server.close()

    asyncio.run(scenario())


def test_non_ascii_text_is_refused_clearly():
    """Re-review N5: 200 characters of UTF-8 could exceed the byte limit; the keyboard is ASCII-only anyway."""
    from hidagent.phone import Phone

    phone = Phone.__new__(Phone)
    phone.device = None  # never reached
    with pytest.raises(DeviceError, match="ASCII"):
        asyncio.run(phone.type("안녕하세요"))


def test_flood_from_many_addresses_cannot_lock_the_board_out():
    """Re-review N1 follow-up: strangers from many addresses (2 each) filling every slot. The oldest pending
    connection is dropped for a new one, so the board, which answers in milliseconds, still gets in."""
    from hidagent.device import MAX_PENDING

    async def scenario():
        device, server, port = await serve()
        device.handshake_timeout = 5
        squatters = []
        for i in range(MAX_PENDING // 2 + 8):
            for _ in range(2):
                squatters.append(await asyncio.open_connection("127.0.0.1", port, local_addr=(f"127.0.1.{i + 1}", 0)))
        await asyncio.sleep(0.2)
        assert len(device._waiting) <= MAX_PENDING
        reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=("127.0.0.2", 0))
        assert await asyncio.wait_for(FakeBoard(reader, writer).handshake(), 2)
        assert await device.wait_connected(1)
        for _, w in squatters:
            w.close()
        writer.close()
        server.close()

    asyncio.run(scenario())
