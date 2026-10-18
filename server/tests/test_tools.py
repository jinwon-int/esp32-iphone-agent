"""The Home Screen operator tools in server/tools (#35): settings resolution and the stop-on-unexpected-screen rule.

No server or device here: the API and frame helpers are replaced, so only the tools' own decisions are tested.
"""
import importlib
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture
def layout(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(TOOLS))
    for name in ("UI_TOKEN", "HIDAGENT_DATA", "HIDAGENT_URL", "HIDAGENT_TOOLS_DIR", "PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("layout", "steps"):
        sys.modules.pop(name, None)
    module = importlib.import_module("layout")
    yield module
    for name in ("layout", "steps"):
        sys.modules.pop(name, None)


def test_import_has_no_side_effects(layout, tmp_path):
    # importing must not read a token or create the frame folders (it did both at import before the move)
    assert layout._token is None
    assert not (tmp_path / "home").exists()


def test_token_prefers_env_then_data_dir(layout, monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "ui_token").write_text("from-file-0123456789\n")
    monkeypatch.setenv("HIDAGENT_DATA", str(data))
    assert layout.token() == "from-file-0123456789"
    layout._token = None
    monkeypatch.setenv("UI_TOKEN", "from-env-0123456789")
    assert layout.token() == "from-env-0123456789"


def test_missing_token_stops_with_a_hint(layout, monkeypatch, tmp_path):
    monkeypatch.setenv("HIDAGENT_DATA", str(tmp_path / "nowhere"))
    with pytest.raises(SystemExit, match="UI_TOKEN"):
        layout.token()


def test_server_url_and_tools_dir(layout, monkeypatch, tmp_path):
    assert layout.server_url() == "http://127.0.0.1:8890"
    monkeypatch.setenv("PORT", "9000")
    assert layout.server_url() == "http://127.0.0.1:9000"
    monkeypatch.setenv("HIDAGENT_URL", "http://10.0.0.5:8890")
    assert layout.server_url() == "http://10.0.0.5:8890"
    assert layout.tools_dir() == tmp_path / "home" / "hidagent-tools"
    monkeypatch.setenv("HIDAGENT_TOOLS_DIR", str(tmp_path / "t"))
    assert layout.tools_dir() == tmp_path / "t"


@pytest.fixture
def steps(layout, monkeypatch):
    module = importlib.import_module("steps")
    calls = []
    monkeypatch.setattr(module, "api", lambda path, body=None, **_: calls.append(path) or {"ok": True})
    monkeypatch.setattr(module, "frame", lambda step=None: b"frame")
    monkeypatch.setattr(module, "which_page", lambda data: ("p1", {}))
    module.calls = calls
    return module


def test_stops_before_a_step_when_edit_mode_cannot_be_entered(steps, monkeypatch):
    monkeypatch.setattr(steps, "ensure_edit", lambda step: False)
    with pytest.raises(SystemExit) as e:
        steps.run([{"op": "tap", "at": [0.5, 0.5]}])
    assert e.value.code == 2 and steps.calls == []  # nothing was sent to the phone


def test_stops_when_edit_mode_is_gone_after_a_step(steps, monkeypatch):
    monkeypatch.setattr(steps, "ensure_edit", lambda step: True)
    monkeypatch.setattr(steps, "is_edit", lambda data: False)  # e.g. a quick-actions menu opened
    with pytest.raises(SystemExit) as e:
        steps.run([{"op": "drag", "from": [0.2, 0.3], "to": [0.4, 0.3]},
                   {"op": "tap", "at": [0.5, 0.5]}])
    assert e.value.code == 4 and steps.calls == ["/api/drag"]  # the second step never ran


def test_in_folder_steps_never_long_press(steps, monkeypatch):
    monkeypatch.setattr(steps, "ensure_edit", lambda step: pytest.fail("long-press inside an open folder"))
    monkeypatch.setattr(steps, "folder_edit", lambda data: False)
    monkeypatch.setattr(steps, "folder_clear", lambda data: False)
    with pytest.raises(SystemExit) as e:
        steps.run([{"op": "drag", "infolder": True, "from": [0.2, 0.3], "to": [0.4, 0.3]}])
    assert e.value.code == 2 and steps.calls == []


def test_wrong_page_fails_the_step(steps, monkeypatch):
    monkeypatch.setattr(steps, "ensure_edit", lambda step: True)
    monkeypatch.setattr(steps, "is_edit", lambda data: True)
    with pytest.raises(SystemExit) as e:
        steps.run([{"op": "carry", "from": [0.2, 0.3], "to": [0.4, 0.3], "pages": -1, "expect": "p2"}])
    assert e.value.code == 3


def test_unknown_op_stops(steps, monkeypatch):
    monkeypatch.setattr(steps, "ensure_edit", lambda step: True)
    with pytest.raises(SystemExit) as e:
        steps.run([{"op": "delete-app"}])
    assert e.value.code == 2 and steps.calls == []


def test_frame_waits_out_a_brief_loss_of_the_live_screen(layout, monkeypatch, tmp_path):
    # the screen app reconnecting: /api/frame.jpg answered 404 "no live screen" for a moment, and the JSON error was
    # handed on as a frame (which_page: "a bytes-like object is required, not 'dict'", device 2026-10-18 18:06 KST)
    replies = [{"ok": False, "status": 404, "detail": "no live screen"}] * 2 + [b"jpeg"]
    monkeypatch.setattr(layout, "api", lambda path, body=None, raw=False, timeout=60: replies.pop(0))
    monkeypatch.setattr(layout.time, "sleep", lambda s: None)
    monkeypatch.setenv("HIDAGENT_TOOLS_DIR", str(tmp_path / "t"))
    assert layout.frame("step") == b"jpeg"
    assert [p.name.endswith("-step.jpg") for p in (tmp_path / "t" / "log").iterdir()] == [True]


def test_frame_stops_when_the_live_screen_stays_away(layout, monkeypatch):
    # acting without seeing the phone is what the stop rules exist for: stop with a clear message, not a traceback
    monkeypatch.setattr(layout, "api", lambda path, body=None, raw=False, timeout=60: {"ok": False, "status": 404})
    monkeypatch.setattr(layout.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="no live screen"):
        layout.frame()
