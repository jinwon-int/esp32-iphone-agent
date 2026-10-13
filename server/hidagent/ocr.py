"""Text on the screen, read from the current frame with RapidOCR — an optional extra (#9, after iCTRL).

Why: the vision model sees the whole screenshot but misreads or misplaces small text (list rows, settings labels,
web views). OCR gives the exact text with the centre of each line, so an agent can aim at words instead of
guessing coordinates. It runs on the server's CPU (about 0.5–1 s per portrait frame) and loads lazily, so a server
without the extra, or with OCR off, pays nothing.

Modes (``OCR_MODE``): ``off`` · ``api`` (``GET /api/screen_text`` for outside agents; the default when the extra
is installed) · ``actor`` (``api`` plus the lines are added to the built-in actor's prompt). The gate never sees
OCR output: it only ever gets the action itself.
"""

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MODES = ("off", "api", "actor")
MAX_ITEMS = 80
MIN_CONF = 0.5


def _importable() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


class OCR:
    """Lazy RapidOCR wrapper: ``read(jpeg)`` -> text lines with their centre as fractions of the frame."""

    def __init__(self, mode: str = "api", engine: Callable[..., Any] | None = None,
                 min_conf: float = MIN_CONF, max_items: int = MAX_ITEMS):
        if mode not in MODES:
            raise ValueError(f"OCR_MODE must be one of {', '.join(MODES)}, not {mode!r}")
        self.mode = mode
        self.min_conf, self.max_items = min_conf, max_items
        self._engine = engine  # injected in tests; otherwise built on first use
        self._cache: tuple[str, list[dict]] | None = None
        self._lock = asyncio.Lock()
        self.last_s = 0.0

    @classmethod
    def from_env(cls) -> "OCR":
        """``OCR_MODE`` from the environment; ``api`` when unset and the extra is installed, else ``off``."""
        mode = os.environ.get("OCR_MODE", "").strip().lower()
        if not mode:
            mode = "api" if _importable() else "off"
        if mode != "off" and not _importable():
            log.error("OCR_MODE=%s but rapidocr is not installed: pip install -e '.[ocr]' — OCR is off", mode)
            mode = "off"
        return cls(mode)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def _engine_or_load(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR  # the models ship inside the wheel

            started = time.time()
            self._engine = RapidOCR()
            log.info("OCR engine loaded in %.1fs", time.time() - started)
        return self._engine

    async def read(self, jpeg: bytes | None) -> list[dict]:
        """Text lines on this frame: ``{text, x, y, w, h, conf}`` with x/y the centre and w/h the size, all 0-1.

        Sorted top to bottom, then left to right; the same frame is not read twice (polling is cheap).
        """
        if not self.enabled or not jpeg:
            return []
        key = hashlib.sha1(jpeg).hexdigest()
        async with self._lock:
            if self._cache and self._cache[0] == key:
                return self._cache[1]
            started = time.time()
            items = await asyncio.to_thread(self._read, jpeg)
            self.last_s = round(time.time() - started, 2)
            self._cache = (key, items)
            return items

    def _read(self, jpeg: bytes) -> list[dict]:
        import io

        from PIL import Image

        width, height = Image.open(io.BytesIO(jpeg)).size
        result, _elapse = self._engine_or_load()(jpeg)
        return self.items(result, width, height, self.min_conf, self.max_items)

    @staticmethod
    def items(result: Any, width: int, height: int, min_conf: float = MIN_CONF,
              max_items: int = MAX_ITEMS) -> list[dict]:
        """RapidOCR's ``[[box(4 points), text, conf], …]`` -> our rows (pure, for tests)."""
        rows = []
        for box, text, conf in result or []:
            text = str(text).strip()
            if not text or float(conf) < min_conf:
                continue
            xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            rows.append({"text": text, "x": round((x0 + x1) / 2 / width, 4), "y": round((y0 + y1) / 2 / height, 4),
                         "w": round((x1 - x0) / width, 4), "h": round((y1 - y0) / height, 4),
                         "conf": round(float(conf), 3)})
        rows.sort(key=lambda r: (round(r["y"] * 40), r["x"]))  # reading order: rows of ~2.5 % height, then x
        return rows[:max_items]

    @staticmethod
    def lines(items: list[dict], scale: int = 1000) -> str:
        """One line per text for a prompt: ``"<text>" @ x y`` in 0-``scale`` coordinates (the actor's units)."""
        return "\n".join(f'"{r["text"]}" @ {round(r["x"] * scale)} {round(r["y"] * scale)}' for r in items)
