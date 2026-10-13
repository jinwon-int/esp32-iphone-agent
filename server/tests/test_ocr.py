"""Screen text with coordinates (#9 item 4): conversion, prompt lines, caching, and real OCR when installed."""

import asyncio
import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from hidagent.ocr import OCR, _importable, import_problem

RAW = [  # RapidOCR's shape: [[4 points], text, confidence]
    [[[59, 602], [145, 602], [145, 640], [59, 640]], "Wi-Fi", 0.99],
    [[[58, 200], [202, 203], [202, 240], [58, 240]], "Settings", 1.0],
    [[[301, 1101], [493, 1103], [493, 1140], [301, 1140]], "Place order", 0.98],
    [[[10, 10], [50, 10], [50, 20], [10, 20]], "9:41", 0.3],  # status-bar noise below min_conf
    [[[10, 10], [50, 10], [50, 20], [10, 20]], "   ", 0.9],  # empty text
]


def test_items_are_centres_as_fractions_in_reading_order():
    items = OCR.items(RAW, width=590, height=1280)
    assert [r["text"] for r in items] == ["Settings", "Wi-Fi", "Place order"]
    settings = items[0]
    assert settings["x"] == pytest.approx(130 / 590, abs=1e-3) and settings["y"] == pytest.approx(220 / 1280, abs=1e-3)
    assert settings["w"] == pytest.approx(144 / 590, abs=1e-3) and settings["h"] == pytest.approx(40 / 1280, abs=1e-3)
    assert all(0 <= r["x"] <= 1 and 0 <= r["y"] <= 1 for r in items)
    assert OCR.items(RAW, 590, 1280, max_items=1) == items[:1]
    assert OCR.items(None, 590, 1280) == []


def test_prompt_lines_use_the_actors_0_1000_units():
    items = OCR.items(RAW, width=590, height=1280)
    lines = OCR.lines(items).splitlines()
    assert lines[0] == '"Settings" @ 220 172'
    assert lines[1].startswith('"Wi-Fi" @ 173 485')


def test_modes_and_env(monkeypatch):
    with pytest.raises(ValueError):
        OCR("sometimes")
    monkeypatch.setenv("OCR_MODE", "off")
    assert OCR.from_env().mode == "off" and not OCR.from_env().enabled
    monkeypatch.delenv("OCR_MODE")
    assert OCR.from_env().mode == ("api" if _importable() else "off")
    monkeypatch.setenv("OCR_MODE", "actor")
    assert OCR.from_env().mode == ("actor" if _importable() else "off")  # asked for but missing: off, logged


def test_read_runs_the_engine_once_per_frame_and_skips_when_off():
    calls = []

    def engine(jpeg):
        calls.append(jpeg)
        return RAW, [0.1, 0.0, 0.0]

    frame = jpeg_with_text([])
    ocr = OCR("api", engine=engine)
    first = asyncio.run(ocr.read(frame))
    again = asyncio.run(ocr.read(frame))
    assert first == again and len(calls) == 1 and ocr.last_s >= 0
    assert [r["text"] for r in first] == ["Settings", "Wi-Fi", "Place order"]
    assert asyncio.run(OCR("off", engine=engine).read(frame)) == [] and len(calls) == 1
    assert asyncio.run(ocr.read(None)) == []


def jpeg_with_text(labels: list[tuple[str, int, int]], size=(590, 1280)) -> bytes:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    for text, x, y in labels:
        draw.text((x, y), text, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


@pytest.mark.skipif(not _importable(), reason=f"OCR extra not importable: {import_problem()}")
def test_real_ocr_finds_text_near_where_it_was_drawn():
    frame = jpeg_with_text([("Settings", 60, 200), ("Wi-Fi", 60, 600)])
    items = asyncio.run(OCR("api").read(frame))
    texts = {r["text"].replace(" ", "").lower(): r for r in items}
    assert "settings" in texts and "wi-fi" in texts
    s = texts["settings"]
    assert abs(s["x"] - 130 / 590) < 0.05 and abs(s["y"] - 220 / 1280) < 0.03


def test_import_problem_is_checked_once_and_explains_itself(monkeypatch):
    import hidagent.ocr as ocr_module

    monkeypatch.setattr(ocr_module, "_import_problem", False)
    import builtins
    real_import = builtins.__import__

    def failing(name, *a, **k):
        if name == "rapidocr_onnxruntime":
            raise ImportError("libGL.so.1: cannot open shared object file: No such file or directory")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", failing)
    problem = ocr_module.import_problem()
    assert "libGL" in problem and "apt install libgl1" in problem
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert ocr_module.import_problem() == problem  # cached: not re-tried every call
    monkeypatch.setenv("OCR_MODE", "actor")
    assert ocr_module.OCR.from_env().mode == "off"  # asked for, cannot import: off, with the reason logged
