import asyncio
import json

import pytest

from hidagent.agent import _pointer_path
from hidagent.phone import ABS_MAX, HOME_SWIPE, HomeVia, Phone


class FakeDevice:
    links = 0

    def __init__(self):
        self.sent = []

    async def send(self, command):
        self.sent.append(command)


def make_phone(tmp_path, home=None, pointer="relative", calibrated=True):
    calibration = tmp_path / "calibration.json"
    if calibrated:
        calibration.write_text(json.dumps({"w": 500, "h": 1000}))
    device = FakeDevice()
    return Phone(device, calibration, home=home, pointer=pointer), device


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


def test_absolute_pointer_needs_no_calibration_and_taps_in_place(tmp_path):
    phone, device = make_phone(tmp_path, pointer="absolute", calibrated=False)
    assert phone.calibrated
    assert asyncio.run(phone.start_calibration()) is True and device.sent == []
    asyncio.run(phone.tap(0.5, 0.25, aim=True))  # nothing to aim: no wiggle, no homing
    assert device.sent == [f"at {round(0.5 * ABS_MAX)} {round(0.25 * ABS_MAX)}", "click"]


def test_absolute_pointer_clamps_to_the_screen(tmp_path):
    phone, device = make_phone(tmp_path, pointer="absolute")
    asyncio.run(phone.point(-0.1, 1.2))
    assert device.sent == [f"at 0 {ABS_MAX}"]


def test_absolute_drag_glides_in_small_steps(tmp_path, monkeypatch):
    monkeypatch.setattr("hidagent.phone.ABS_STEP_S", 0)
    phone, device = make_phone(tmp_path, pointer="absolute")
    asyncio.run(phone.swipe(0.5, 0.8, 0.5, 0.5, hold=False))
    ats = [c for c in device.sent if c.startswith("at")]
    assert device.sent[1] == "press" and device.sent[-1] == "release"
    assert len(ats) == 1 + 10  # start, then 0.3 of the screen in 0.03 steps
    assert ats[-1] == f"at {round(0.5 * ABS_MAX)} {round(0.5 * ABS_MAX)}"
    assert not any(c.startswith(("move", "home")) for c in device.sent)


def test_pointer_mode_is_checked():
    with pytest.raises(ValueError):
        Phone(FakeDevice(), None, pointer="tablet")
