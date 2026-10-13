import asyncio

import pytest

from hidagent import auth
from hidagent.device import Device, DeviceError


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


def test_round_trip_with_a_fake_board():
    async def scenario():
        device, server, port = await serve()
        reader, writer, result = await connect_board(port)
        assert result == "ok"

        async def board():  # answers every command like the firmware does
            while line := await reader.readline():
                writer.write(b"ok\n" if line.strip() != b"bad" else b"err unknown command\n")
                await writer.drain()

        task = asyncio.create_task(board())
        assert await device.wait_connected(2)
        await device.send("ping")
        with pytest.raises(DeviceError, match="unknown command"):
            await device.send("bad")
        assert device.links == 1
        task.cancel()
        writer.close()
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
