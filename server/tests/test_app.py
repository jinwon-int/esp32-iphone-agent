"""The HTTP surface an outside agent uses: the manual at /api/skill and the action report (#9, after iCTRL)."""

import asyncio
import io
import os
import re

import pytest
from PIL import Image


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    """The app imported with a throw-away data dir and known secrets (no lifespan: no board, no UxPlay)."""
    data = tmp_path_factory.mktemp("hidagent")
    os.environ.update({"HIDAGENT_DATA": str(data), "DEVICE_KEY": "0123456789abcdef0123456789abcdef",
                       "UI_TOKEN": "test-ui-token-0123456789", "SCREEN_SOURCE": "airplay"})
    import importlib

    from fastapi.testclient import TestClient

    import hidagent.app as app_module
    importlib.reload(app_module)
    client = TestClient(app_module.app)
    client.headers.update({"Authorization": "Bearer test-ui-token-0123456789"})
    return app_module, client


def jpeg(color: tuple[int, int, int], size=(90, 195)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG", quality=80)
    return out.getvalue()


def test_skill_needs_the_token_and_reads_as_a_skill_file(web):
    module, client = web
    anonymous = client.get("/api/skill", headers={"Authorization": ""})
    assert anonymous.status_code == 401
    r = client.get("/api/skill")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    text = r.text
    assert text.startswith("---\nname: iphone-agent-control\n")
    for heading in ("## Access", "## Coordinates", "## Actions", "## Rules", "## The built-in agent", "## Limits"):
        assert heading in text, heading
    assert "Authorization: Bearer" in text and "fractions of the screenshot" in text
    assert "Never press a final" in text  # the irreversible-action rule is in the manual too


def test_skill_mentions_every_action_route_the_app_serves(web):
    module, client = web
    manual = client.get("/api/skill").text
    posts = {r.path for r in module.app.routes if "POST" in getattr(r, "methods", ()) and r.path.startswith("/api/")}
    posts -= {"/api/raw", "/api/calibrate/finish"}  # debug-only; the finish click is a web-UI gesture
    missing = [p for p in sorted(posts) if p not in manual]
    assert not missing, f"routes the manual does not mention: {missing}"
    assert "/api/frame.jpg" in manual and "/api/status" in manual and "/openapi.json" in manual


def test_actions_report_settle_and_whether_the_screen_changed(web, monkeypatch):
    module, client = web
    frames = {"now": jpeg((20, 20, 20))}
    taps: list[tuple[float, float]] = []

    class FakeScreen:
        live, aspect = True, 90 / 195
        @property
        def frame(self):
            return frames["now"]
        async def settled(self, **_):
            return 0.42, True

    class FakePhone:
        async def tap(self, x, y, aim=False):
            taps.append((x, y))
            frames["now"] = frames.get("after", frames["now"])
        async def swipe(self, x1, y1, x2, y2):
            pass

    monkeypatch.setattr(module, "screen", FakeScreen())
    monkeypatch.setattr(module, "phone", FakePhone())

    # The tap changes nothing: the report says so (and the pointer spot itself is masked out).
    r = client.post("/api/tap", json={"x": 0.5, "y": 0.4})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "settle": {"waited_s": 0.42, "stable": True}, "screen_changed": False}
    assert taps == [(0.5, 0.4)]

    # A tap that opens something: the frame differs afterwards.
    frames["after"] = jpeg((230, 230, 230))
    r = client.post("/api/tap", json={"x": 0.5, "y": 0.4})
    assert r.json()["screen_changed"] is True and r.json()["ok"] is True

    # Swipes report the same shape.
    r = client.post("/api/swipe", json={"x1": 0.5, "y1": 0.8, "x2": 0.5, "y2": 0.5})
    assert set(r.json()) == {"ok", "settle", "screen_changed"}


def test_action_without_a_live_frame_reports_unknown_change(web, monkeypatch):
    module, client = web

    class NoScreen:
        live, aspect, frame = False, 0.46, None
        async def settled(self, **_):
            return 0.25, True

    class FakePhone:
        async def go_home(self):
            pass

    monkeypatch.setattr(module, "screen", NoScreen())
    monkeypatch.setattr(module, "phone", FakePhone())
    r = client.post("/api/home")
    assert r.json() == {"ok": True, "settle": {"waited_s": 0.25, "stable": True}, "screen_changed": None}


def test_action_errors_are_409_with_the_reason(web, monkeypatch):
    module, client = web
    from hidagent.device import DeviceError

    class FakePhone:
        async def tap(self, x, y, aim=False):
            raise DeviceError("board not connected")

    class FakeScreen:
        live, aspect, frame = True, 0.46, jpeg((1, 2, 3))
        async def settled(self, **_):
            return 0.0, True

    monkeypatch.setattr(module, "screen", FakeScreen())
    monkeypatch.setattr(module, "phone", FakePhone())
    r = client.post("/api/tap", json={"x": 0.1, "y": 0.1})
    assert r.status_code == 409 and "board not connected" in r.json()["detail"]


def test_skill_file_ships_with_the_package():
    import importlib.resources as resources

    text = resources.files("hidagent").joinpath("skill.md").read_text(encoding="utf-8")
    assert re.search(r"^name: iphone-agent-control$", text, re.M)
    assert asyncio.iscoroutinefunction(__import__("hidagent.app", fromlist=["act"]).act)


def test_skill_file_explains_folder_rename_and_korean_typing():
    import importlib.resources as resources

    text = " ".join(resources.files("hidagent").joinpath("skill.md").read_text(encoding="utf-8").split())
    assert "Renaming a Home Screen folder" in text and "Remove from Home Screen" in text
    assert "ASCII only" not in text and "or Korean" in text  # Korean typing since #27


# --- screen text (OCR, #9 item 4) ---------------------------------------------------------------------------


class FakeOCR:
    last_s = 0.3

    def __init__(self, mode="api", items=None):
        self.mode, self.items_, self.frames = mode, items or [], []

    @property
    def enabled(self):
        return self.mode != "off"

    async def read(self, jpeg):
        self.frames.append(jpeg)
        return self.items_


def test_screen_text_is_503_when_ocr_is_off(web, monkeypatch):
    module, client = web
    monkeypatch.setattr(module, "ocr", FakeOCR("off"))
    r = client.get("/api/screen_text")
    detail = r.json()["detail"]
    assert r.status_code == 503 and "pip install -e '.[ocr]'" in detail and "OCR is off (" in detail


def test_screen_text_needs_a_live_screen_and_filters_by_confidence(web, monkeypatch):
    module, client = web
    rows = [{"text": "Wi-Fi", "x": 0.17, "y": 0.47, "w": 0.15, "h": 0.02, "conf": 0.99},
            {"text": "9:41", "x": 0.1, "y": 0.02, "w": 0.1, "h": 0.01, "conf": 0.55}]
    ocr = FakeOCR("api", rows)
    monkeypatch.setattr(module, "ocr", ocr)

    class NoScreen:
        live, frame, updated, aspect = False, None, 0.0, 0.46

    monkeypatch.setattr(module, "screen", NoScreen())
    assert client.get("/api/screen_text").status_code == 404

    class LiveScreen:
        live, frame, updated, aspect = True, jpeg((9, 9, 9)), 1234.5, 0.46

    monkeypatch.setattr(module, "screen", LiveScreen())
    r = client.get("/api/screen_text")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2 and body["took_s"] == 0.3 and body["frame_updated"] == 1234.5
    assert client.get("/api/screen_text?min_conf=0.9").json()["items"] == rows[:1]
    assert ocr.frames and ocr.frames[0] == LiveScreen.frame
    assert client.get("/api/status").json()["ocr"] == "api"


def test_longpress_and_drag_endpoints(web, monkeypatch):
    from hidagent.phone import DROP, LONG_PRESS, PICK_UP

    module, client = web
    calls = []

    class FakeScreen:
        live, aspect, frame = True, 90 / 195, None

        async def settled(self, **_):
            return 0.3, True

    class FakePhone:
        async def long_press(self, x, y, seconds=1.0, aim=False):
            calls.append(("long_press", x, y, seconds))

        async def drag(self, points, hold=True, pick=0.0, rest=None, aim=False, stops=None, lift=0.0):
            calls.append(("drag", points, pick, rest, aim) + ((stops, lift) if stops or lift else ()))

    monkeypatch.setattr(module, "screen", FakeScreen())
    monkeypatch.setattr(module, "phone", FakePhone())

    r = client.post("/api/longpress", json={"x": 0.5, "y": 0.3})
    assert r.status_code == 200 and set(r.json()) == {"ok", "settle", "screen_changed"}
    r = client.post("/api/drag", json={"x1": 0.15, "y1": 0.35, "x2": 0.4, "y2": 0.35})
    assert r.status_code == 200
    assert calls == [("long_press", 0.5, 0.3, LONG_PRESS), ("drag", [(0.15, 0.35), (0.4, 0.35)], PICK_UP, DROP, True)]

    # the hold times can be tuned per call, within limits (a button held for minutes would be a stuck finger)
    client.post("/api/longpress", json={"x": 0.5, "y": 0.3, "seconds": 2.0})
    assert calls[-1] == ("long_press", 0.5, 0.3, 2.0)
    assert client.post("/api/longpress", json={"x": 0.5, "y": 0.3, "seconds": 30}).status_code == 422
    assert client.post("/api/drag", json={"x1": 0.1, "y1": 0.1, "x2": 0.2, "y2": 0.2, "rest": 9}).status_code == 422


def test_startup_log_names_where_the_tokens_are_kept_never_their_values(web, monkeypatch):
    module, _ = web
    ui, push = module.UI_TOKEN, "push-token-must-not-be-logged-0123"

    class FakeScreen:
        host, port, token = "100.64.0.1", 8891, push

    monkeypatch.setattr(module, "UI_TOKEN_FROM_ENV", False)
    monkeypatch.setattr(module, "PUSH_TOKEN_FROM_ENV", False)
    monkeypatch.setattr(module, "SCREEN_SOURCE", "push")
    monkeypatch.setattr(module, "screen", FakeScreen())
    text = "\n".join(module.startup_notes(8890))
    assert ui not in text and push not in text
    assert str(module.DATA_DIR / "ui_token") in text and str(module.DATA_DIR / "push_token") in text
    assert "ws://100.64.0.1:8891/" in text


def test_drag_endpoint_passes_via_points_and_lift(web, monkeypatch):
    module, client = web
    calls = []

    class FakeScreen:
        live, aspect, frame = True, 90 / 195, None

        async def settled(self, **_):
            return 0.3, True

    class FakePhone:
        async def drag(self, points, hold=True, pick=0.0, rest=None, aim=False, stops=None, lift=0.0):
            calls.append((points, pick, rest, stops, lift))

    monkeypatch.setattr(module, "screen", FakeScreen())
    monkeypatch.setattr(module, "phone", FakePhone())
    r = client.post("/api/drag", json={"x1": 0.85, "y1": 0.14, "x2": 0.5, "y2": 0.6, "pick": 0.9, "rest": 1.0,
                                       "lift": 0.6, "via": [{"x": 0.985, "y": 0.45, "rest": 2.5}]})
    assert r.status_code == 200
    assert calls == [([(0.85, 0.14), (0.985, 0.45), (0.5, 0.6)], 0.9, 1.0, [2.5], 0.6)]
    assert client.post("/api/drag", json={"x1": 0.1, "y1": 0.1, "x2": 0.2, "y2": 0.2,
                                          "via": [{"x": 0.9, "y": 0.5, "rest": 9}]}).status_code == 422


def test_carry_endpoint(web, monkeypatch):
    module, client = web
    calls = []

    class FakeScreen:
        live, aspect, frame = True, 90 / 195, None

        async def settled(self, **_):
            return 0.3, True

    class FakePhone:
        async def carry(self, start, target, pages, pick=0.5, lift=0.6, rest=1.0):
            calls.append((start, target, pages, pick, lift, rest))
            return abs(pages)

    monkeypatch.setattr(module, "screen", FakeScreen())
    monkeypatch.setattr(module, "phone", FakePhone())
    r = client.post("/api/carry", json={"x1": 0.85, "y1": 0.13, "x2": 0.5, "y2": 0.6, "pages": -1})
    assert r.status_code == 200 and r.json()["pages_turned"] == 1
    assert calls == [((0.85, 0.13), (0.5, 0.6), -1, 0.5, 0.6, 1.0)]
    assert client.post("/api/carry", json={"x1": 0.1, "y1": 0.1, "x2": 0.2, "y2": 0.2, "pages": 0}).status_code == 422
    assert client.post("/api/carry", json={"x1": 0.1, "y1": 0.1, "x2": 0.2, "y2": 0.2, "pages": 2}).status_code == 422
