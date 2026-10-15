import asyncio
import io
import json

import pytest
from PIL import Image, ImageDraw

from hidagent import bench
from hidagent.phone import ABS_MAX, Phone

W, H = 498, 1080


class Board:
    """Tracks where the pointer is from the commands it gets; `off` shifts where it really lands (screen fraction)."""

    links = 0

    def __init__(self, off=(0.0, 0.0), span=(500, 1000)):
        self.off, self.span, self.pos = off, span, (0.5, 0.5)
        self.units = (0, 0)

    async def send(self, command):
        name, *args = command.split()
        if name == "at":
            self.pos = (int(args[0]) / ABS_MAX, int(args[1]) / ABS_MAX)
        elif name == "home":
            self.units = (0, 0)
            self.pos = (0.0, 0.0)
        elif name == "move":
            self.units = (self.units[0] + int(args[0]), self.units[1] + int(args[1]))
            self.pos = (self.units[0] / self.span[0], self.units[1] / self.span[1])

    @property
    def seen(self):
        return self.pos[0] + self.off[0], self.pos[1] + self.off[1]


class Screen:
    live, aspect = True, W / H

    def __init__(self, board):
        self.board = board

    @property
    def frame(self):
        img = Image.new("RGB", (W, H))
        draw = ImageDraw.Draw(img)
        for y in range(H):
            draw.line([(0, y), (W, y)], fill=(40, 60 + y // 10, 120))
        x, y = self.board.seen[0] * W, self.board.seen[1] * H
        draw.ellipse([x - 12, y - 12, x + 12, y + 12], fill=(200, 200, 200))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=75)
        return buf.getvalue()

    async def settled(self, quiet=0.35, least=0.25, most=4):
        return 0.0, True

    async def fresh(self, since, timeout=0.8):
        return self.frame


def phone(tmp_path, pointer, board):
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"w": 500, "h": 1000}))
    return Phone(board, calibration, Screen(board), pointer=pointer)


@pytest.mark.parametrize("mode", ["absolute", "relative"])
def test_bench_finds_the_pointer_at_all_nine_spots(tmp_path, mode):
    board = Board()
    result = asyncio.run(bench.pointer(phone(tmp_path, mode, board)))
    s = result["summary"]
    assert s["mode"] == mode and s["points"] == 9 and s["seen"] == 9
    assert s["within_1pct"] == 9 and s["err_max"] <= 0.01
    assert "| 0.10, 0.15 |" in result["markdown"] and f"**{mode}**: seen 9/9" in result["markdown"]


def test_bench_reports_a_systematic_miss(tmp_path):
    board = Board(off=(0.02, 0.0))  # the phone puts the pointer 2 % to the right of where it was sent
    s = asyncio.run(bench.pointer(phone(tmp_path, "absolute", board)))["summary"]
    assert s["seen"] == 9 and s["within_1pct"] == 0
    assert s["err_max"] == pytest.approx(0.02, abs=0.006)


def test_bench_clicks_nothing(tmp_path):
    sent = []
    board = Board()
    original = board.send

    async def record(command):
        sent.append(command)
        await original(command)

    board.send = record
    asyncio.run(bench.pointer(phone(tmp_path, "absolute", board), points=[(0.5, 0.5)]))
    assert not any(c.split()[0] in ("click", "press", "release") for c in sent)


def test_percentile_is_nearest_rank():
    assert bench._pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 5
    assert bench._pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == 9
    assert bench._pct([3], 0.9) == 3
