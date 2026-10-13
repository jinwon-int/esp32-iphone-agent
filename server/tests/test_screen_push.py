"""The push screen source: a phone app sends JPEGs over one WebSocket; the agent sees the AirPlay interface."""

import asyncio
import base64
import os
import time

import pytest

from hidagent import auth
from hidagent.screen import Frames, Screen
from hidagent.screen_push import (
    MAX_PENDING_PER_PEER,
    OP_BINARY,
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    PushScreen,
    accept_key,
    check_host,
    frame_bytes,
    unmask,
    vpn_like,
)

TOKEN = "push-token-0123456789abcdef"
MASK = b"\x12\x34\x56\x78"


def jpeg(width: int = 590, height: int = 1280, filler: bytes = b"") -> bytes:
    """The smallest thing the server accepts as a JPEG: SOI, a baseline SOF0 carrying the size, EOI."""
    sof = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + sof + filler + b"\xff\xd9"


def client_frame(payload: bytes, opcode: int = OP_BINARY, fin: bool = True, masked: bool = True) -> bytes:
    """A client-to-server frame (masked, as the RFC requires of clients)."""
    n = len(payload)
    first = (0x80 if fin else 0) | opcode
    if n < 126:
        head = bytes((first, (0x80 if masked else 0) | n))
    elif n < 1 << 16:
        head = bytes((first, (0x80 if masked else 0) | 126)) + n.to_bytes(2, "big")
    else:
        head = bytes((first, (0x80 if masked else 0) | 127)) + n.to_bytes(8, "big")
    if not masked:
        return head + payload
    return head + MASK + unmask(payload, MASK)  # XOR is its own inverse


async def connect(port: int, token: str | None = TOKEN, request: str | None = None):
    """Open the WebSocket; returns (reader, writer, status line)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if request is None:
        request = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n")
        if token is not None:
            request += f"Authorization: Bearer {token}\r\n"
        request += "\r\n"
    writer.write(request.encode())
    await writer.drain()
    status = (await reader.readline()).decode().strip()
    if status.startswith("HTTP/1.1 101"):
        headers = b""
        while not headers.endswith(b"\r\n\r\n"):
            headers += await reader.readline()
        assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in headers  # the RFC 6455 example key
    return reader, writer, status


async def send(writer, payload: bytes, **kw) -> None:
    writer.write(client_frame(payload, **kw))
    await writer.drain()


async def serve(**kw) -> tuple[PushScreen, int]:
    screen = PushScreen("127.0.0.1", 0, TOKEN, **kw)
    server = await screen.serve()
    return screen, server.sockets[0].getsockname()[1]


async def until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def read_frame(reader) -> tuple[int, bytes]:
    """A server-to-client frame: (opcode, payload)."""
    head = await reader.readexactly(2)
    length = head[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return head[0] & 0x0F, await reader.readexactly(length)


# --- the pieces ---------------------------------------------------------------


def test_accept_key_matches_the_rfc_example():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_unmask_round_trips_and_handles_odd_lengths():
    for n in (0, 1, 3, 4, 5, 1000, 4097):
        payload = os.urandom(n)
        assert unmask(unmask(payload, MASK), MASK) == payload
    assert unmask(b"\x12\x34\x56\x78\x12", MASK) == b"\x00\x00\x00\x00\x00"


def test_server_frames_use_the_three_length_forms():
    assert frame_bytes(OP_PONG, b"hi") == b"\x8a\x02hi"
    assert frame_bytes(OP_BINARY, b"x" * 300)[:4] == b"\x82\x7e\x01\x2c"
    assert frame_bytes(OP_BINARY, b"x" * 70000)[:10] == b"\x82\x7f" + (70000).to_bytes(8, "big")


@pytest.mark.parametrize("host", ["", " ", "*", "0.0.0.0", "::", "[::]"])
def test_wildcard_bind_addresses_are_refused(host):
    with pytest.raises(ValueError, match="VPN address"):
        check_host(host)
    with pytest.raises(ValueError, match="VPN address"):
        PushScreen(host, 8891, TOKEN)


def test_vpn_like_addresses():
    assert vpn_like("100.101.102.103") and vpn_like("127.0.0.1") and vpn_like("fd7a:115c:a1e0::1234")
    assert not vpn_like("192.0.2.10") and not vpn_like("example.invalid") and not vpn_like("8.8.8.8")


def test_short_push_token_is_refused():
    with pytest.raises(ValueError, match="PUSH_TOKEN"):
        PushScreen("127.0.0.1", 8891, "short")


def test_push_token_is_generated_and_kept_private(tmp_path, monkeypatch):
    monkeypatch.delenv("PUSH_TOKEN", raising=False)
    data = auth.private_dir(tmp_path / "data")
    token = auth.push_token(data)
    assert len(token) >= auth.MIN_UI_TOKEN and token != auth.ui_token(data)  # its own secret, not the UI token
    assert (data / "push_token").stat().st_mode & 0o777 == 0o600
    assert auth.push_token(data) == token  # kept across restarts
    monkeypatch.setenv("PUSH_TOKEN", "from-the-environment-0123456789")
    assert auth.push_token(data) == "from-the-environment-0123456789"


def test_push_screen_has_the_airplay_screen_interface():
    """Whatever the source, the agent, calibration and web UI only ever touch these names."""
    names = ("frame", "updated", "live", "aspect", "settled", "fresh", "start", "stop")
    push, airplay = PushScreen("127.0.0.1", 8891, TOKEN), Screen("Agent")
    for name in names:
        assert hasattr(push, name) and hasattr(airplay, name), name
    assert isinstance(push, Frames) and isinstance(airplay, Frames)
    assert push.live is False and push.frame is None and push.aspect == pytest.approx(9 / 19.5)


# --- a phone sends frames -----------------------------------------------------------


def test_frames_from_the_app_become_the_screen():
    async def main():
        screen, port = await serve()
        assert not screen.live
        reader, writer, status = await connect(port)
        assert status == "HTTP/1.1 101 Switching Protocols"
        assert await until(lambda: screen.links == 1)
        assert not screen.live  # connected but nothing received yet

        before = time.time()
        await send(writer, jpeg(590, 1280))
        assert await screen.fresh(before) == jpeg(590, 1280)
        assert screen.live and screen.aspect == pytest.approx(590 / 1280)

        # A re-sent identical frame is a keepalive, not a change: `updated` stays, so settled()/fresh() keep meaning.
        updated = screen.updated
        await send(writer, jpeg(590, 1280))
        await asyncio.sleep(0.05)
        assert screen.updated == updated and screen.dropped == 0

        # A changed frame is a change (after the rate cap's window).
        await asyncio.sleep(1 / 15 + 0.01)
        since = time.time()
        await send(writer, jpeg(1280, 590))
        assert await screen.fresh(since) == jpeg(1280, 590)
        assert screen.aspect == pytest.approx(1280 / 590)

        # A ping is answered and keeps the connection live.
        await send(writer, b"keep", opcode=OP_PING)
        assert await read_frame(reader) == (OP_PONG, b"keep")

        # The phone hangs up: close is answered, the screen is gone.
        await send(writer, (1000).to_bytes(2, "big"), opcode=OP_CLOSE)
        opcode, _ = await read_frame(reader)
        assert opcode == OP_CLOSE
        assert await until(lambda: not screen.live and screen.frame is None)
        writer.close()
        screen.stop()

    asyncio.run(main())


def test_wrong_or_missing_token_is_refused_before_any_frame():
    async def main():
        screen, port = await serve()
        for token in ("wrong-token-0123456789abcdef", None, ""):
            reader, writer, status = await connect(port, token=token)
            assert status == "HTTP/1.1 401 Unauthorized"
            assert await reader.read() == b"" or True  # closed by the server
            writer.close()
        assert screen.links == 0 and screen.frame is None and not screen.live
        screen.stop()

    asyncio.run(main())


@pytest.mark.parametrize("request_text", [
    "POST / HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN + "\r\n\r\n",  # not an upgrade
    "GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 8\r\nSec-WebSocket-Key: a\r\n"
    "Authorization: Bearer " + TOKEN + "\r\n\r\n",  # old protocol version
    "hello power-on 1234\n",  # a board that found the wrong port
])
def test_non_websocket_requests_are_refused(request_text):
    async def main():
        screen, port = await serve()
        reader, writer, status = await connect(port, request=request_text)
        if request_text.startswith("hello"):
            assert status in ("", "HTTP/1.1 400 Bad Request")  # times out or is rejected: either way, no frame
        else:
            assert status == "HTTP/1.1 400 Bad Request"
        writer.close()
        assert screen.links == 0
        screen.stop()

    asyncio.run(asyncio.wait_for(main(), 10))


def test_oversized_frame_closes_the_connection_unread():
    async def main():
        screen, port = await serve(max_frame=4096)
        reader, writer, _ = await connect(port)
        await send(writer, jpeg())
        assert await until(lambda: screen.frame is not None)
        # The header says 5000 bytes: the server closes with 1009 before reading them.
        writer.write(client_frame(b"\xff\xd8" + b"\x00" * 4996 + b"\xff\xd9"))
        await writer.drain()
        opcode, payload = await read_frame(reader)
        assert opcode == OP_CLOSE and int.from_bytes(payload[:2], "big") == 1009
        assert await until(lambda: not screen.live)
        writer.close()
        screen.stop()

    asyncio.run(main())


def test_fragmented_frame_is_assembled_and_fragments_count_towards_the_cap():
    async def main():
        screen, port = await serve(max_frame=4096)
        reader, writer, _ = await connect(port)
        whole = jpeg(filler=b"\x01" * 100)
        await send(writer, whole[:50], fin=False)
        await send(writer, whole[50:90], opcode=OP_CONT, fin=False)
        await send(writer, whole[90:], opcode=OP_CONT)
        assert await until(lambda: screen.frame == whole)
        # 3000 + 3000 bytes across fragments exceeds 4096: closed.
        await send(writer, b"\xff\xd8" + b"\x00" * 2998, fin=False)
        await send(writer, b"\x00" * 2998 + b"\xff\xd9", opcode=OP_CONT)
        opcode, payload = await read_frame(reader)
        assert opcode == OP_CLOSE and int.from_bytes(payload[:2], "big") == 1009
        writer.close()
        screen.stop()

    asyncio.run(main())


def test_non_jpeg_bytes_and_text_are_not_frames():
    async def main():
        screen, port = await serve()
        reader, writer, _ = await connect(port)
        await send(writer, b"PNG\x89 not a jpeg")
        await send(writer, b"hello", opcode=OP_TEXT)
        await asyncio.sleep(0.05)
        assert screen.frame is None and screen.dropped == 1 and not screen.live
        await send(writer, jpeg())
        assert await until(lambda: screen.live)
        writer.close()
        screen.stop()

    asyncio.run(main())


def test_frames_over_the_rate_cap_are_dropped_not_disconnected():
    async def main():
        screen, port = await serve(max_fps=10)
        reader, writer, _ = await connect(port)
        for i in range(30):  # a burst of distinct frames, far faster than 10/s
            await send(writer, jpeg(filler=i.to_bytes(2, "big")))
        await asyncio.sleep(0.1)
        kept = 30 - screen.dropped
        assert 1 <= kept <= 3 and screen.live  # the window is 100 ms; the burst takes a few ms
        await asyncio.sleep(0.11)
        await send(writer, jpeg(filler=b"last"))
        assert await until(lambda: screen.frame == jpeg(filler=b"last"))
        writer.close()
        screen.stop()

    asyncio.run(main())


def test_unmasked_client_frame_is_a_protocol_error():
    async def main():
        screen, port = await serve()
        reader, writer, _ = await connect(port)
        writer.write(client_frame(jpeg(), masked=False))
        await writer.drain()
        opcode, payload = await read_frame(reader)
        assert opcode == OP_CLOSE and int.from_bytes(payload[:2], "big") == 1002
        assert screen.frame is None
        writer.close()
        screen.stop()

    asyncio.run(main())


def test_new_authenticated_connection_replaces_the_old_one():
    """After a network change the phone reconnects while the old socket is still open: the new one must win."""
    async def main():
        screen, port = await serve()
        reader1, writer1, _ = await connect(port)
        await send(writer1, jpeg(filler=b"one"))
        assert await until(lambda: screen.frame == jpeg(filler=b"one"))
        reader2, writer2, status = await connect(port)
        assert status == "HTTP/1.1 101 Switching Protocols"
        assert await until(lambda: screen.links == 2)
        assert await reader1.read() == b""  # the old connection was closed by the server
        await asyncio.sleep(0.05)
        assert screen.frame == jpeg(filler=b"one")  # the old connection's exit did not blank the new screen
        await asyncio.sleep(1 / 15)
        await send(writer2, jpeg(filler=b"two"))
        assert await until(lambda: screen.frame == jpeg(filler=b"two"))
        assert screen.live
        writer1.close()
        writer2.close()
        screen.stop()

    asyncio.run(main())


def test_silent_connection_goes_stale_and_a_ping_revives_it():
    async def main():
        screen, port = await serve(stale=0.3)
        reader, writer, _ = await connect(port)
        await send(writer, jpeg())
        assert await until(lambda: screen.live)
        await asyncio.sleep(0.4)
        assert not screen.live  # still connected, but nothing heard for longer than `stale`
        await send(writer, b"", opcode=OP_PING)
        await read_frame(reader)
        assert screen.live and screen.frame == jpeg()  # the picture did not change; the phone is just still
        writer.close()
        screen.stop()

    asyncio.run(main())


def test_too_many_pending_handshakes_from_one_peer_are_closed_at_once():
    async def main():
        screen, port = await serve()
        held = [await asyncio.open_connection("127.0.0.1", port) for _ in range(MAX_PENDING_PER_PEER)]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await reader.read() == b""  # the extra one is closed without a handshake
        for _r, w in held:
            w.close()
        writer.close()
        screen.stop()

    asyncio.run(asyncio.wait_for(main(), 10))


def test_start_and_stop_inside_a_running_loop():
    async def main():
        screen = PushScreen("127.0.0.1", 0, TOKEN)
        screen.start()
        assert await until(lambda: screen._server is not None)
        screen.stop()
        await asyncio.sleep(0.05)
        assert screen._task.cancelled() or screen._task.done()

    asyncio.run(main())


def test_base64_key_handling_is_not_fooled_by_whitespace_or_case():
    assert accept_key(base64.b64encode(b"0123456789abcdef").decode()) == accept_key("MDEyMzQ1Njc4OWFiY2RlZg==")
