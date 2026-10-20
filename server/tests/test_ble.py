"""The BLE link (#67): the board's byte stream over GATT, driven by the same Device as over TCP. No radio here:
the GATT side is faked, the firmware side is FakeBoard from test_device, so the handshake and tags are the real ones."""

import asyncio
import random

import pytest
from test_device import KEY, FakeBoard

from hidagent.ble import BleLink
from hidagent.device import Device, DeviceError


class Radio:
    """Both directions of the fake GATT link. App -> board: every write lands in the board's reader. Board -> app:
    what the board writes arrives as notifications of at most `mtu - 3` bytes, split at random places."""

    def __init__(self, write_size: int = 20, mtu: int = 23, seed: int = 1):
        self.writes: list[bytes] = []
        self.board_in = asyncio.StreamReader()
        self.rng = random.Random(seed)
        self.mtu, self.disconnects = mtu, 0
        self.link = BleLink(self.write, write_size, self.disconnect, peer=("ble", "AA:BB"))

    async def write(self, chunk: bytes) -> None:
        self.writes.append(chunk)
        self.board_in.feed_data(chunk)

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.link.lost()
        self.board_in.feed_eof()

    # the board's writer
    def write_board(self, data: bytes) -> None:
        while data:
            n = self.rng.randint(1, self.mtu - 3)
            self.link.notified(data[:n])
            data = data[n:]

    async def drain_board(self) -> None:
        await asyncio.sleep(0)

    def close_board(self) -> None:  # the firmware hangs up: the app sees a disconnect
        self.link.lost()
        self.board_in.feed_eof()

    def board(self, key: bytes = KEY) -> FakeBoard:
        writer = type("W", (), {"write": lambda _, d: self.write_board(d), "drain": lambda _: self.drain_board(),
                                "close": lambda _: self.close_board()})()
        return FakeBoard(self.board_in, writer, key=key)


async def attach(radio: Radio, key: bytes = KEY) -> tuple[Device, FakeBoard, asyncio.Task]:
    device = Device(KEY, handshake_timeout=2)
    board = radio.board(key)
    handshake = asyncio.create_task(board.handshake())
    await device.attach(radio.link.reader, radio.link)
    assert await handshake == device.connected
    return device, board, asyncio.create_task(board.serve())


def test_handshake_and_commands_over_split_notifications_and_small_writes():
    async def scenario():
        radio = Radio(write_size=20, mtu=23)
        device, board, task = await attach(radio)
        assert device.connected and device.links == 1
        assert await device.send("ping") == "ok"
        await device.send("type hello world, a line longer than one write ")
        assert board.executed == ["ping", "type hello world, a line longer than one write "]
        assert radio.writes and max(len(w) for w in radio.writes) <= 20  # never more than one write can carry
        assert b"".join(radio.writes).count(b"\n") == 4  # challenge, ok, two commands: lines survive the split
        task.cancel()

    asyncio.run(scenario())


def test_a_board_with_another_key_is_refused_and_disconnected():
    async def scenario():
        radio = Radio()
        device = Device(KEY, handshake_timeout=0.3)  # FakeBoard just stops; the firmware would also hang up
        board = radio.board(key=b"another-key-0123456789abcdef")
        handshake = asyncio.create_task(board.handshake())
        await device.attach(radio.link.reader, radio.link)
        assert not await handshake  # the board rejects the server's proof: it never learns a session
        assert not device.connected
        await asyncio.sleep(0)
        assert radio.disconnects == 1  # closing the writer drops the BLE connection, like a TCP close

    asyncio.run(scenario())


def test_losing_the_connection_fails_the_next_command_fast():
    async def scenario():
        radio = Radio()
        device, _board, task = await attach(radio)
        radio.close_board()  # e.g. the board was unplugged
        assert not device.connected
        with pytest.raises(DeviceError, match="not connected"):
            await device.send("ping")
        task.cancel()

    asyncio.run(scenario())


def test_a_failed_write_is_a_lost_link_not_a_crash():
    async def scenario():
        radio = Radio()
        device, _board, task = await attach(radio)

        async def broken(_chunk: bytes) -> None:
            raise RuntimeError("GATT write failed")  # what bleak raises varies by platform

        radio.link._send = broken
        with pytest.raises(DeviceError, match="link lost"):
            await device.send("ping")
        assert not device.connected
        task.cancel()

    asyncio.run(scenario())


def test_writes_after_close_are_dropped():
    async def scenario():
        radio = Radio()
        link = radio.link
        link.close()
        assert link.is_closing()
        link.write(b"ping\n")
        await link.drain()
        assert radio.writes == []
        await asyncio.sleep(0)
        assert radio.disconnects == 1
        link.close()  # twice is fine
        await asyncio.sleep(0)
        assert radio.disconnects == 1

    asyncio.run(scenario())
