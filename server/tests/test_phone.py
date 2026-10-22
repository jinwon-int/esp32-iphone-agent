import asyncio
import json

import pytest

from hidagent.agent import _pointer_path
from hidagent.phone import ABS_MAX, HOLD, HOME_SWIPE, LONG_PRESS, HomeVia, Phone


class FakeDevice:
    links = 0

    def __init__(self):
        self.sent = []

    async def send(self, command):
        self.sent.append(command)

PARK_AT = "at 492 16384"  # phone.PARK (0.015, 0.5) as an absolute report: where the pointer parks after a gesture


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
    assert device.sent == [f"at {round(0.5 * ABS_MAX)} {round(0.25 * ABS_MAX)}", "click", PARK_AT]  # then it parks


def test_absolute_pointer_clamps_to_the_screen(tmp_path):
    phone, device = make_phone(tmp_path, pointer="absolute")
    asyncio.run(phone.point(-0.1, 1.2))
    assert device.sent == [f"at 0 {ABS_MAX}"]


def test_absolute_drag_glides_in_small_steps(tmp_path, monkeypatch):
    monkeypatch.setattr("hidagent.phone.ABS_STEP_S", 0)
    phone, device = make_phone(tmp_path, pointer="absolute")
    asyncio.run(phone.swipe(0.5, 0.8, 0.5, 0.5, hold=False))
    ats = [c for c in device.sent if c.startswith("at")]
    assert device.sent[1] == "press" and device.sent[-2:] == ["release", PARK_AT]
    assert len(ats) == 1 + 10 + 1  # start, then 0.3 of the screen in 0.03 steps, then the park
    assert ats[-2] == f"at {round(0.5 * ABS_MAX)} {round(0.5 * ABS_MAX)}" and ats[-1] == PARK_AT
    assert not any(c.startswith(("move", "home")) for c in device.sent)


def test_pointer_mode_is_checked():
    with pytest.raises(ValueError):
        Phone(FakeDevice(), None, pointer="tablet")


def test_home_via_the_consumer_home_key(tmp_path):
    via = HomeVia.from_env({"HOME_METHOD": "key"})
    assert via == HomeVia(key=True) and via.path == []
    phone, device = make_phone(tmp_path, via)
    asyncio.run(phone.go_home())
    assert device.sent == ["consumer home"]  # no pointer moves at all
    assert _pointer_path({"action": "home"}, via.path) == []


def record_sleeps(monkeypatch, device):
    """Log every pause into the device's command list, so tests see when the button is held and for how long."""
    async def sleep(seconds):
        device.sent.append(f"sleep {seconds}")
    monkeypatch.setattr("hidagent.phone.asyncio.sleep", sleep)


def test_long_press_holds_the_button_down_on_the_target(tmp_path, monkeypatch):
    phone, device = make_phone(tmp_path)
    record_sleeps(monkeypatch, device)
    asyncio.run(phone.long_press(0.5, 0.25, seconds=1.2))
    assert device.sent[:2] == ["home", "move 250 250"]
    assert device.sent[2:5] == ["press", "sleep 1.2", "release"]  # down, held, up: no click
    assert "click" not in device.sent


def test_long_press_defaults_to_about_a_second(tmp_path, monkeypatch):
    phone, device = make_phone(tmp_path)
    record_sleeps(monkeypatch, device)
    asyncio.run(phone.long_press(0.5, 0.25))
    held = device.sent[device.sent.index("press") + 1]
    assert held == f"sleep {LONG_PRESS}" and 0.8 <= LONG_PRESS <= 1.5


def test_long_press_lets_go_even_when_interrupted(tmp_path, monkeypatch):
    phone, device = make_phone(tmp_path)

    async def stop(seconds):  # e.g. the user presses Stop while the button is down
        raise asyncio.CancelledError

    monkeypatch.setattr("hidagent.phone.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(phone.long_press(0.5, 0.25))
    assert device.sent[-1] == "release"  # never leave the button down


def test_drag_can_pick_up_first_and_rest_on_the_target_before_dropping(tmp_path, monkeypatch):
    """Moving a Home Screen icon: hold still until iOS lifts it, move, hover until the target reacts, let go."""
    phone, device = make_phone(tmp_path)
    record_sleeps(monkeypatch, device)
    asyncio.run(phone.drag([(0.2, 0.2), (0.6, 0.4)], pick=0.8, rest=1.0))
    i = device.sent.index("press")
    assert device.sent[i:] == ["press", "sleep 0.8", "move 200 200", "sleep 1.0", "release"]


def test_plain_drag_is_unchanged(tmp_path, monkeypatch):
    phone, device = make_phone(tmp_path)
    record_sleeps(monkeypatch, device)
    asyncio.run(phone.drag([(0.2, 0.2), (0.6, 0.4)]))
    i = device.sent.index("press")
    assert device.sent[i:] == ["press", "move 200 200", f"sleep {HOLD}", "release"]  # no pause before moving
    device.sent.clear()
    asyncio.run(phone.drag([(0.2, 0.2), (0.6, 0.4)], hold=False))
    assert device.sent[-2:] == ["move 200 200", "release"]  # a flick: no rest at the end either


def test_absolute_long_press_lands_then_holds(tmp_path, monkeypatch):
    phone, device = make_phone(tmp_path, pointer="absolute", calibrated=False)
    record_sleeps(monkeypatch, device)
    asyncio.run(phone.long_press(0.5, 0.25, seconds=1.0))
    assert device.sent[0] == f"at {round(0.5 * ABS_MAX)} {round(0.25 * ABS_MAX)}"
    i = device.sent.index("press")
    assert device.sent[i:i + 3] == ["press", "sleep 1.0", "release"]
    assert not any(c.startswith(("move", "home")) for c in device.sent)


def test_a_gesture_near_the_screen_edge_rehomes_before_the_next_one(tmp_path):
    # #36 device run: after a tap on the top-edge checkmark the next long press landed on the Dynamic Island
    # (screen sharing's stop prompt); the pointer stops at an edge, so dead reckoning is re-anchored there
    phone, device = make_phone(tmp_path)
    asyncio.run(phone.tap(0.5, 0.5))
    asyncio.run(phone.tap(0.83, 0.035))  # top edge
    device.sent.clear()
    asyncio.run(phone.tap(0.4, 0.13))
    assert device.sent[0] == "home" and device.sent[1] == "move 200 130"


def test_a_gesture_away_from_the_edges_keeps_dead_reckoning(tmp_path):
    phone, device = make_phone(tmp_path)
    asyncio.run(phone.tap(0.5, 0.5))
    device.sent.clear()
    asyncio.run(phone.tap(0.4, 0.13))
    assert "home" not in device.sent and device.sent[0] == "move -50 -370"


# --- moving icons across pages (#35, device run 2026-10-17) ------------------------------------------------------


def test_drag_rests_at_each_via_point_so_a_page_can_flip(tmp_path, monkeypatch):
    # hold an icon at the screen edge until iOS turns the page, then carry on to the target
    phone, device = make_phone(tmp_path)
    record_sleeps(monkeypatch, device)
    asyncio.run(phone.drag([(0.2, 0.2), (0.98, 0.4), (0.6, 0.4)], pick=0.8, rest=1.0, stops=[2.5]))
    i = device.sent.index("press")
    assert device.sent[i:] == ["press", "sleep 0.8", "move 390 200", "sleep 2.5", "move -190 0", "sleep 1.0",
                               "release"]


def test_drag_stops_must_match_the_via_points(tmp_path):
    phone, _ = make_phone(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(phone.drag([(0.2, 0.2), (0.6, 0.4)], stops=[1.0]))


def test_absolute_drag_can_lift_slowly_before_carrying(tmp_path, monkeypatch):
    # iOS 27 opened the item's menu instead of lifting it when the pointer raced off right after the hold
    from hidagent.phone import LIFT_DIST, LIFT_STEPS

    phone, device = make_phone(tmp_path, pointer="absolute")
    record_sleeps(monkeypatch, device)
    asyncio.run(phone.drag([(0.5, 0.5), (0.5, 0.2)], pick=0.9, rest=1.0, lift=0.6))
    i = device.sent.index("press")
    assert device.sent[i + 1] == "sleep 0.9"
    lifting = device.sent[i + 2:i + 2 + 2 * LIFT_STEPS]
    ats, sleeps = lifting[0::2], lifting[1::2]
    assert all(a.startswith("at ") for a in ats) and sleeps == [f"sleep {0.6 / LIFT_STEPS}"] * LIFT_STEPS
    y_after_lift = int(ats[-1].split()[2]) / ABS_MAX
    assert abs(y_after_lift - (0.5 - LIFT_DIST)) < 1e-3  # the first LIFT_DIST of the way, slowly
    assert device.sent[-3:] == ["sleep 1.0", "release", PARK_AT]


def test_absolute_pointer_is_woken_before_acting_after_a_pause(tmp_path, monkeypatch):
    # after the pointer idled, a tap clicked at its old spot (the AssistiveTouch button) and a swipe became a long
    # press: the first report only woke the pointer
    from hidagent.phone import ABS_WAKE_S

    phone, device = make_phone(tmp_path, pointer="absolute")
    record_sleeps(monkeypatch, device)
    phone._reported -= 10  # ten seconds idle
    asyncio.run(phone.tap(0.4, 0.6))
    at = f"at {round(0.4 * ABS_MAX)} {round(0.6 * ABS_MAX)}"
    assert device.sent[:3] == [at, f"sleep {ABS_WAKE_S}", at]
    assert device.sent.count("click") == 1
    device.sent.clear()
    asyncio.run(phone.tap(0.4, 0.6))  # right after: no extra wake-up
    assert device.sent[0] == at and f"sleep {ABS_WAKE_S}" not in device.sent


# --- carry: hold at the edge until the screen shows N page turns (#35) -------------------------------------------


def _jpeg(value, icon=None):
    """A solid page; ``icon`` (x, y fractions) draws a white icon there."""
    import io

    from PIL import Image, ImageDraw

    im = Image.new("RGB", (90, 195), (value, value, value))
    if icon:
        x, y = icon[0] * 90, icon[1] * 195
        ImageDraw.Draw(im).rectangle((x - 7, y - 7, x + 7, y + 7), fill=(255, 255, 255))
    out = io.BytesIO()
    im.save(out, "JPEG")
    return out.getvalue()


class FlipScreen:
    """A live screen whose frame follows a script: one entry per poll (each sleep of the phone)."""
    live = True

    def __init__(self, script):
        self.script, self.i = script, 0

    @property
    def frame(self):
        return self.script[min(self.i, len(self.script) - 1)]


def fresh(frame, n):
    """n newly pushed copies of a frame (new objects, as the screen app sends them)."""
    return [bytes(bytearray(frame)) for _ in range(n)]


def carry_phone(tmp_path, monkeypatch, script):
    phone, device = make_phone(tmp_path, pointer="absolute")
    phone.screen = FlipScreen(script)

    clock = [0.0]

    async def sleep(seconds):
        device.sent.append(f"sleep {seconds}")
        phone.screen.i += 1
        clock[0] += seconds
    monkeypatch.setattr("hidagent.phone.asyncio.sleep", sleep)
    monkeypatch.setattr("hidagent.phone._clock", lambda: clock[0])
    monkeypatch.setattr("hidagent.phone.ABS_STEP_S", 0)
    return phone, device


def test_carry_leaves_the_edge_as_soon_as_the_page_starts_to_turn(tmp_path, monkeypatch):
    # held on, iOS turns page after page with no still moment between (device frames 2026-10-17): leave at the first
    p1, p2, p3 = _jpeg(30), _jpeg(130), _jpeg(230)
    script = [_jpeg(30, (0.85, 0.13))] * 3 + [p1] * 37 + [_jpeg(80)] + fresh(p2, 8) + [_jpeg(170)] + [p3] * 40
    phone, device = carry_phone(tmp_path, monkeypatch, script)
    assert asyncio.run(phone.carry((0.85, 0.13), (0.5, 0.6), pages=1, pick=0.5, lift=0.6, rest=1.0)) == 1
    assert device.sent[-2:] == ["sleep 1.0", "release"]  # carry has its own release: no park
    polls = device.sent.count("sleep 0.05")  # FLIP_POLL checks at the edge
    assert 10 < polls < 30  # waited for the first turn (index 40) to settle, left before the second (index 49)
    edge = f"at {round(0.985 * ABS_MAX)} {round(0.80 * ABS_MAX)}"  # the empty band, beside no icon
    last_edge = max(i for i, c in enumerate(device.sent) if c == edge)
    after = [c for c in device.sent[last_edge + 1:] if c.startswith("at ")]
    lx, ly = map(int, after[-1].split()[1:])
    assert abs(lx - 0.5 * ABS_MAX) <= 2 and abs(ly - 0.6 * ABS_MAX) <= 2 and len(after) == 1  # jumped off (#54)
    assert any(abs(int(c.split()[1]) - 0.5 * ABS_MAX) <= 2 and abs(int(c.split()[2]) - 0.6 * ABS_MAX) <= 2
               for c in device.sent if c.startswith("at "))  # then carried to the target


def test_carry_turns_left_for_negative_pages(tmp_path, monkeypatch):
    p1, p2 = _jpeg(30), _jpeg(200)
    script = [_jpeg(30, (0.5, 0.5))] * 3 + [p1] * 37 + [_jpeg(120)] + [p2] * 30
    phone, device = carry_phone(tmp_path, monkeypatch, script)
    assert asyncio.run(phone.carry((0.5, 0.5), (0.3, 0.6), pages=-1)) == 1
    assert any(c.startswith(f"at {round(0.015 * ABS_MAX)} ") for c in device.sent)


def test_carry_puts_the_item_back_when_no_page_turns(tmp_path, monkeypatch):
    phone, device = carry_phone(tmp_path, monkeypatch, [_jpeg(30, (0.3, 0.3))] * 3 + [_jpeg(30)] * 400)
    with pytest.raises(RuntimeError, match="page did not turn"):
        asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1))
    assert device.sent[-1] == "release"
    back = f"at {round(0.3 * ABS_MAX)} {round(0.3 * ABS_MAX)}"
    assert back in device.sent[-6:]  # carried back to where it came from before letting go


def test_carry_needs_a_live_screen_and_a_page_count(tmp_path):
    phone, _ = make_phone(tmp_path, pointer="absolute")
    with pytest.raises(RuntimeError):
        asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1))  # no screen
    phone.screen = FlipScreen([_jpeg(30)])
    for pages in (0, 2, -2):
        with pytest.raises(ValueError):
            asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=pages))


def test_carry_stops_when_the_item_did_not_lift(tmp_path, monkeypatch):
    # device 2026-10-17: right after entering edit mode the icon stayed in its slot while the pointer went to the
    # edge, lifted late and formed a folder; now the start slot is checked once at the edge
    phone, device = carry_phone(tmp_path, monkeypatch, [_jpeg(30, (0.3, 0.3))] * 400)
    with pytest.raises(RuntimeError, match="did not lift"):
        asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1))
    assert device.sent[-1] == "release" and "sleep 0.05" in device.sent


def test_a_folder_forming_is_not_a_page_turn(tmp_path, monkeypatch):
    # a local change (one cell) must not count as a turn: no turn -> the item is put back
    start = (0.3, 0.3)
    local = _jpeg(30, (0.85, 0.7))  # something changed in one spot only (a folder forming beside the edge)
    phone, _ = carry_phone(tmp_path, monkeypatch, [_jpeg(30, start)] * 3 + [_jpeg(30)] * 37 + [local] * 400)
    with pytest.raises(RuntimeError, match="page did not turn"):
        asyncio.run(phone.carry(start, (0.5, 0.6), pages=1))


def test_carry_waits_for_the_turn_to_settle_before_leaving_the_edge(tmp_path, monkeypatch):
    # leaving mid-turn made iOS turn back to the first page (device 2026-10-17 23:21)
    p1, p2 = _jpeg(30), _jpeg(200)
    script = [_jpeg(30, (0.3, 0.3))] * 3 + [p1] * 37 + [_jpeg(90), _jpeg(140), _jpeg(180)] + fresh(p2, 60)
    phone, device = carry_phone(tmp_path, monkeypatch, script)
    edge = f"at {round(0.985 * ABS_MAX)} {round(0.80 * ABS_MAX)}"
    log = []
    real_send = device.send

    async def send(command):  # remember the frame index of every command
        log.append((command, phone.screen.i))
        await real_send(command)
    device.send = send
    asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1))
    last_edge = max(k for k, (c, _) in enumerate(log) if c == edge)
    left_at = next(i for c, i in log[last_edge + 1:] if c.startswith("at "))
    assert left_at >= 43  # left the edge only once the turn's frames (40-42) were over


def test_carry_does_not_take_a_repeated_frame_for_a_settled_turn(tmp_path, monkeypatch):
    # the same frame polled twice (no new push yet) said "still" mid-turn; iOS then turned back (#35, 23:33 KST)
    p1, mid, p2 = _jpeg(30), _jpeg(120), _jpeg(200)
    script = [_jpeg(30, (0.3, 0.3))] * 3 + [p1] * 37 + [mid] * 6 + fresh(p2, 40)  # the turn's frame repeats 6 polls
    phone, device = carry_phone(tmp_path, monkeypatch, script)
    edge = f"at {round(0.985 * ABS_MAX)} {round(0.80 * ABS_MAX)}"
    log = []
    real_send = device.send

    async def send(command):
        log.append((command, phone.screen.i))
        await real_send(command)
    device.send = send
    asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1))
    last_edge = max(k for k, (c, _) in enumerate(log) if c == edge)
    left_at = next(i for c, i in log[last_edge + 1:] if c.startswith("at "))
    assert left_at >= 46  # not while the turn's frame was merely repeated (indices 40-45)


def _edge_log(phone, device):
    log = []
    real_send = device.send

    async def send(command):  # remember the frame index of every command
        log.append((command, phone.screen.i))
        await real_send(command)
    device.send = send
    return log


def test_carry_sees_a_turn_between_look_alike_pages(tmp_path, monkeypatch):
    # pages of folders in the same slots: cells_changed stayed at 0.42 through the turn, the next turn (to a page
    # that looks different) was the first one seen, and two pages turned (#54, device 2026-10-18)
    a, b = _jpeg(30), _jpeg(33)  # look-alike pages: every cell under vision.cells_changed's level
    script = ([_jpeg(30, (0.3, 0.3))] * 3 + [a] * 37 + [_jpeg(42), _jpeg(31)]  # one turn: slides on 2 new frames
              + fresh(b, 9) + [_jpeg(200)] + [_jpeg(230)] * 40)  # index 51: the next turn, to another page
    phone, device = carry_phone(tmp_path, monkeypatch, script)
    log = _edge_log(phone, device)
    assert asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1)) == 1
    edge = f"at {round(0.985 * ABS_MAX)} {round(0.80 * ABS_MAX)}"
    last_edge = max(k for k, (c, _) in enumerate(log) if c == edge)
    left_at = next(i for c, i in log[last_edge + 1:] if c.startswith("at "))
    assert 42 <= left_at < 51  # after the first turn settled, before the second began


def test_carry_counts_the_settle_from_the_turn_start_not_from_its_detection(tmp_path, monkeypatch):
    # the screen app pushed a frame about every 0.17 s; a turn between look-alike pages was seen on its 2nd frame,
    # the 0.25 s settle was then counted from there, the first still frame (+0.35 s into the turn) was passed over
    # and the edge was left on the next one, too late: iOS had already begun the next turn (#54, device 07:20 KST)
    a, b = _jpeg(30), _jpeg(33)
    push = 4  # polls (0.05 s each) per pushed frame, repeated as the same object in between
    script = ([_jpeg(30, (0.3, 0.3))] * 3 + [a] * 37
              + [_jpeg(42)] * push + [_jpeg(31)] * push  # index 40: the turn begins, 44: its 2nd sliding frame
              + [_jpeg(33)] * push + [b] * push + [_jpeg(33)] * push  # 48, 52, 56: the new page holds still
              + [_jpeg(200)] + [_jpeg(230)] * 40)  # index 60: the next turn
    phone, device = carry_phone(tmp_path, monkeypatch, script)
    log = _edge_log(phone, device)
    assert asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1)) == 1
    edge = f"at {round(0.985 * ABS_MAX)} {round(0.80 * ABS_MAX)}"
    last_edge = max(k for k, (c, _) in enumerate(log) if c == edge)
    left_at = next(i for c, i in log[last_edge + 1:] if c.startswith("at "))
    assert 48 <= left_at < 52  # on the first still frame: the next turn's decision comes before its frames do


def test_carry_jumps_off_the_edge_once_the_turn_settled(tmp_path, monkeypatch):
    # gliding off took the item from x 0.03 to only 0.10 in the 0.2 s before iOS began the next turn (#54, device
    # 2026-10-18 09:32 KST); jumping right as a turn began made iOS turn back (#52): so wait, then jump
    a, b = _jpeg(30), _jpeg(33)
    push = 4
    script = ([_jpeg(30, (0.3, 0.3))] * 3 + [a] * 37 + [_jpeg(42)] * push + [_jpeg(31)] * push
              + [_jpeg(33)] * push + [b] * push + [_jpeg(33)] * push + [_jpeg(200)] + [_jpeg(230)] * 40)
    phone, device = carry_phone(tmp_path, monkeypatch, script)
    log = _edge_log(phone, device)
    asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1))
    edge = f"at {round(0.985 * ABS_MAX)} {round(0.80 * ABS_MAX)}"
    last_edge = max(k for k, (c, _) in enumerate(log) if c == edge)
    off = [c for c, _ in log[last_edge + 1:] if c.startswith("at ")]
    assert len(off) >= 1 and len(set(off)) == 1  # straight from the edge to the target: no steps in between
    assert log[last_edge + 1][1] >= 48  # and only once the turn had settled


def test_carry_sees_a_quick_turn_between_look_alike_pages(tmp_path, monkeypatch):
    # the turn slid 9.0, 15.0, 5.2 on three pushed frames: never twice in a row above FLIP_MOTION, so it was missed,
    # and the next turn (to page 1) was the first one seen (#54, device 2026-10-18 18:07 KST). The new page held
    # still 0.35 s though, 14-17 away from the frame before the turn: that counts as a turn now
    a = _jpeg(30)
    push = 4
    script = ([_jpeg(30, (0.3, 0.3))] * 3 + [a] * 37
              + [_jpeg(38)] * push + [_jpeg(49)] * push + [_jpeg(46)] * push  # 40, 44 (the one sliding frame), 48
              + [_jpeg(46)] * push + [_jpeg(46)] * push  # 52, 56: the new page holds still
              + [_jpeg(200)] + [_jpeg(230)] * 40)  # index 60: the next turn
    phone, device = carry_phone(tmp_path, monkeypatch, script)
    log = _edge_log(phone, device)
    assert asyncio.run(phone.carry((0.3, 0.3), (0.5, 0.6), pages=1)) == 1
    edge = f"at {round(0.985 * ABS_MAX)} {round(0.80 * ABS_MAX)}"
    last_edge = max(k for k, (c, _) in enumerate(log) if c == edge)
    left_at = next(i for c, i in log[last_edge + 1:] if c.startswith("at "))
    assert 48 <= left_at < 60  # after the first turn, before the next one


def test_a_single_jolt_at_the_edge_is_not_a_page_turn(tmp_path, monkeypatch):
    # on the last page iOS bounces at the edge: one frame slid 14.8, the way back 5.0 and less (device 2026-10-18)
    start = (0.3, 0.3)
    jolts = []
    for _ in range(30):
        jolts += [_jpeg(44), _jpeg(40), _jpeg(36), _jpeg(33)] + fresh(_jpeg(30), 2)
    phone, _ = carry_phone(tmp_path, monkeypatch, [_jpeg(30, start)] * 3 + [_jpeg(30)] * 37 + jolts)
    with pytest.raises(RuntimeError, match="page did not turn"):
        asyncio.run(phone.carry(start, (0.5, 0.6), pages=1))


def test_absolute_glide_step_sets_how_many_reports_a_stroke_takes(tmp_path):
    """Every absolute report is one board round trip (~170 ms on the device), so a page turn passes a bigger step."""
    from hidagent.phone import Phone

    def stroke(sent):  # the reports while the button is down
        return sent[sent.index("press") + 1:sent.index("release")]

    phone = Phone(FakeDevice(), tmp_path / "calibration.json", pointer="absolute")
    asyncio.run(phone.drag([(0.1, 0.5), (0.7, 0.5)], hold=False))
    default = stroke(phone.device.sent)
    phone.device.sent.clear()
    asyncio.run(phone.drag([(0.1, 0.5), (0.7, 0.5)], hold=False, step=0.15))
    wide = stroke(phone.device.sent)
    assert len(default) == 20 and len(wide) == 4  # 0.6 / 0.03 vs 0.6 / 0.15
    assert all(c.startswith("at ") for c in default + wide)


def test_a_flick_is_one_board_stroke_on_an_absolute_board(tmp_path):
    """#74 (device 2026-10-23, run a9294c662ca6): report by report, a page turn crawled at one link round trip per
    report and iOS snapped it back; the board plays the stroke itself: one command, then the pointer parks."""
    from hidagent.phone import Phone
    phone = Phone(FakeDevice(), tmp_path / "calibration.json", pointer="absolute")
    asyncio.run(phone.flick(0.7, 0.5, 0.1, 0.5, step=0.15))
    sent = phone.device.sent
    assert "stroke 22937 16384 3277 16384 12" in sent and "press" not in sent and "release" not in sent
    assert sent[sent.index("stroke 22937 16384 3277 16384 12") - 1] == "at 22937 16384"  # the pointer is there first
    assert sent[-1] == PARK_AT


class NoStrokeDevice(FakeDevice):
    def __init__(self, reply="err unknown command"):
        super().__init__()
        self.reply = reply

    async def send(self, command):
        self.sent.append(command)
        if command.startswith("stroke"):
            from hidagent.device import DeviceError
            raise DeviceError(self.reply)


def test_a_board_without_stroke_gets_the_flick_as_a_drag_until_the_next_link(tmp_path):
    """Older firmware answers "err unknown command": the stroke is dragged in the given steps, and the board is not
    asked again until it reconnects (a reflashed board comes back on a new link)."""
    from hidagent.phone import Phone
    phone = Phone(NoStrokeDevice(), tmp_path / "calibration.json", pointer="absolute")
    asyncio.run(phone.flick(0.7, 0.5, 0.1, 0.5, step=0.15))
    sent = phone.device.sent
    down = sent[sent.index("press") + 1:sent.index("release")]
    assert len(down) == 4 and all(c.startswith("at ") for c in down)  # 0.6 / 0.15, no rest
    phone.device.sent.clear()
    asyncio.run(phone.flick(0.7, 0.5, 0.1, 0.5, step=0.15))
    assert not any(c.startswith("stroke") for c in phone.device.sent) and "press" in phone.device.sent
    phone.device.links += 1
    phone.device.sent.clear()
    asyncio.run(phone.flick(0.7, 0.5, 0.1, 0.5, step=0.15))
    assert any(c.startswith("stroke") for c in phone.device.sent)


def test_a_stroke_that_fails_otherwise_is_not_retried_as_a_drag(tmp_path):
    """Only a missing command falls back: a stroke that failed on the board (USB busy, link lost) raises, as every
    gesture does, instead of pressing a second time."""
    from hidagent.device import DeviceError
    from hidagent.phone import Phone
    phone = Phone(NoStrokeDevice("err stroke release not sent (usb busy)"), tmp_path / "calibration.json",
                  pointer="absolute")
    with pytest.raises(DeviceError, match="usb busy"):
        asyncio.run(phone.flick(0.7, 0.5, 0.1, 0.5, step=0.15))
    assert "press" not in phone.device.sent


def test_a_relative_pointer_flicks_as_a_drag(tmp_path):
    phone, device = make_phone(tmp_path)
    asyncio.run(phone.flick(0.7, 0.5, 0.1, 0.5, step=0.15))
    assert not any(c.startswith("stroke") for c in device.sent)
    assert "press" in device.sent and device.sent[-1] == "release"
