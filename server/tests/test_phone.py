import asyncio
import json

import pytest

from hidagent.agent import _pointer_path
from hidagent.phone import HOME_SWIPE, HomeVia, Phone


class FakeDevice:
    links = 0

    def __init__(self):
        self.sent = []

    async def send(self, command):
        self.sent.append(command)


def make_phone(tmp_path, home=None):
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps({"w": 500, "h": 1000}))
    device = FakeDevice()
    return Phone(device, calibration, home=home), device


def test_home_defaults_to_the_bottom_flick(tmp_path):
    phone, device = make_phone(tmp_path)
    asyncio.run(phone.go_home())
    assert "press" in device.sent and device.sent[-1] == "release"
    assert "click" not in device.sent


def test_home_via_assistivetouch_menu_taps_button_then_home(tmp_path, monkeypatch):
    monkeypatch.setattr("hidagent.phone.MENU_OPEN_S", 0)
    phone, device = make_phone(tmp_path, HomeVia(button=(0.9, 0.25), menu_home=(0.6, 0.34)))
    asyncio.run(phone.go_home())
    assert device.sent.count("click") == 2 and "press" not in device.sent
    moves = [c for c in device.sent if c.startswith("move")]
    assert moves == ["move 450 250", "move -150 90"]  # to the button, then on to the menu's Home item


def test_home_via_assistivetouch_single_tap(tmp_path):
    phone, device = make_phone(tmp_path, HomeVia(button=(0.9, 0.25)))
    asyncio.run(phone.go_home())
    assert device.sent.count("click") == 1 and "press" not in device.sent


def test_home_via_from_env():
    assert HomeVia.from_env({}) == HomeVia()
    assert HomeVia.from_env({"HOME_METHOD": "swipe", "ASSISTIVETOUCH_BUTTON": "0.9,0.2"}) == HomeVia()
    via = HomeVia.from_env({"HOME_METHOD": "AssistiveTouch", "ASSISTIVETOUCH_BUTTON": " 0.898, 0.266 ",
                            "ASSISTIVETOUCH_MENU_HOME": "0.6,0.34"})
    assert via == HomeVia(button=(0.898, 0.266), menu_home=(0.6, 0.34))
    assert HomeVia.from_env({"HOME_METHOD": "assistivetouch", "ASSISTIVETOUCH_BUTTON": "0.9,0.2"}).menu_home is None


@pytest.mark.parametrize("env", [
    {"HOME_METHOD": "globe"},
    {"HOME_METHOD": "assistivetouch"},
    {"HOME_METHOD": "assistivetouch", "ASSISTIVETOUCH_BUTTON": "898,266"},
    {"HOME_METHOD": "assistivetouch", "ASSISTIVETOUCH_BUTTON": "0.9"},
    {"HOME_METHOD": "assistivetouch", "ASSISTIVETOUCH_BUTTON": "0.9,0.2", "ASSISTIVETOUCH_MENU_HOME": "x,y"},
])
def test_home_via_from_env_rejects_bad_settings(env):
    with pytest.raises(ValueError):
        HomeVia.from_env(env)


def test_pointer_path_follows_the_way_home():
    assert _pointer_path({"action": "home"}) == list(HOME_SWIPE)
    via = HomeVia(button=(0.9, 0.25), menu_home=(0.6, 0.34))
    assert _pointer_path({"action": "home"}, via.path) == [(0.9, 0.25), (0.6, 0.34)]
