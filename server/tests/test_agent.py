import math

import pytest

from hidagent.agent import circle, parse_actions, parse_command, parse_plan


def test_parse_single_action():
    acts = parse_actions("THINK: The Search pill is visible, so I tap it.\nDO: tap 500 814")
    assert acts == [{"thought": "The Search pill is visible, so I tap it.", "thought_zh": "",
                     "action": "tap", "x": 500, "y": 814}]


def test_parse_several_actions_keeps_thought_on_first():
    acts = parse_actions("THINK: Type and search.\nDO: type Whiteboard\nDO: key enter")
    assert [a["action"] for a in acts] == ["type", "key"]
    assert acts[0]["text"] == "Whiteboard" and acts[1]["combo"] == "enter"
    assert "thought" not in acts[1]


def test_parse_caps_at_six_actions():
    reply = "THINK: draw\n" + "\n".join(f"DO: tap {i} {i}" for i in range(10))
    assert len(parse_actions(reply)) == 6


def test_parse_path_and_circle():
    assert parse_command("path 100 200 300 400 500 600")["points"] == [[100, 200], [300, 400], [500, 600]]
    assert parse_command("circle 500 450 200") == {"action": "circle", "x": 500, "y": 450, "r": 200}


@pytest.mark.parametrize("line", ["fly 1 2", "tap 500", "path 1 2 3"])
def test_parse_rejects_bad_commands(line):
    with pytest.raises(ValueError):
        parse_command(line)


def test_parse_requires_a_do_line():
    with pytest.raises(ValueError):
        parse_actions("THINK: I am not sure what to do.")


def test_parse_plan_with_and_without_languages():
    assert parse_plan("EN:\n1. Tap Search\nZH:\n1. 点搜索") == ("1. Tap Search", "1. 点搜索")
    assert parse_plan("1. Tap Search") == ("1. Tap Search", "1. Tap Search")


def test_circle_is_closed_and_round_on_a_tall_screen():
    aspect = 0.46  # width / height of a portrait iPhone frame
    pts = circle(0.5, 0.5, 0.2, aspect)
    assert pts[0] == pytest.approx(pts[-1])
    for x, y in pts:  # equal radius in pixels: y offsets shrink by the aspect ratio
        assert math.hypot(x - 0.5, (y - 0.5) / aspect) == pytest.approx(0.2)


def test_actor_may_not_repeat_a_move_that_changed_nothing():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46

    class Actor:
        model = "fake"
        def __init__(self):
            self.prompts = []
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            self.prompts.append(text)
            return "THINK: again\nDO: swipe 382 253 382 150" if len(self.prompts) == 1 else "THINK: other\nDO: tap 1 2"

    actor = Actor()
    agent = Agent(phone=None, screen=Screen(), actor=actor, planner=actor, trace=Trace(None))
    agent.steps = [{"action": "swipe", "x1": 382, "y1": 253, "x2": 382, "y2": 150, "changed": False}]
    acts = asyncio.run(agent._next_actions())
    assert acts[0]["action"] == "tap"
    assert "changed nothing" in actor.prompts[1]


def test_repeating_a_move_that_keeps_changing_the_screen_is_not_stuck():
    from hidagent.agent import Agent
    from hidagent.debug import Trace

    agent = Agent(phone=None, screen=None, actor=None, planner=None, trace=Trace(None))
    swipe = {"action": "swipe", "x1": 500, "y1": 800, "x2": 500, "y2": 400}
    agent.steps = [{**swipe, "changed": True}, {**swipe, "changed": True}]
    assert not agent._repeating(dict(swipe))
    agent.steps[-1]["changed"] = False  # reached the end of the list
    assert agent._repeating(dict(swipe))


def test_precise_tap_flag():
    assert parse_command("tap 120 640 precise") == {"action": "tap", "x": 120, "y": 640, "precise": True}
    assert "precise" not in parse_command("tap 120 640")


# --- borrowed from iCTRL's agent rules (#9): stop before anything irreversible; settle telemetry -------------


def test_parse_confirm_is_a_terminal_action_with_a_reason():
    assert parse_command("confirm Cart holds 2 items; the Pay button is next") == {
        "action": "confirm", "reason": "Cart holds 2 items; the Pay button is next"}
    from hidagent.agent import ACTIONS, ACTOR_PROMPT, PLANNER_PROMPT, TERMINAL
    assert "confirm" in TERMINAL and ACTIONS["confirm"] == ("reason",)
    assert "never tap a final Pay" in ACTOR_PROMPT and "stop for the user to confirm" in PLANNER_PROMPT


def test_confirm_ends_the_run_without_acting_or_asking_the_gate():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46
        async def settled(self, **_):
            return 0.3, True

    class Phone:
        taps = []
        async def tap(self, x, y, aim=False):
            self.taps.append((x, y))

    class Actor:
        model = "fake"
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            return ("THINK: Everything is in the cart; Pay is next.\n"
                    "DO: confirm 2 items in the cart, total 31.90, Pay is the next tap")

    class Gate:
        mode, asked = "observe", []
        def check(self, *a, **k):
            self.asked.append(a)
            return None

    gate = Gate()
    agent = Agent(phone=Phone(), screen=Screen(), actor=Actor(), planner=Actor(), trace=Trace(None), gate=gate)
    agent.task = "buy the two items"
    assert asyncio.run(agent._step()) is True
    assert agent.steps[-1]["action"] == "confirm" and agent.steps[-1]["reason"].startswith("2 items")
    assert Phone.taps == [] and gate.asked == []  # nothing was tapped and the gate was not asked about a non-action


def test_settle_telemetry_is_recorded_but_hidden_from_the_actor_and_the_action_key(monkeypatch):
    import asyncio

    from hidagent.agent import Agent, _key
    from hidagent.debug import Trace

    monkeypatch.setattr("hidagent.agent.vision.changed", lambda *a, **k: True)  # frames here are not real JPEGs

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46
        async def settled(self, **_):
            return 1.2, False  # still changing when the wait gave up

    class Phone:
        async def tap(self, x, y, aim=False):
            pass

    class Actor:
        model = "fake"
        prompts = []
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            self.prompts.append(text)
            return "THINK: tap it\nDO: tap 100 200"

    agent = Agent(phone=Phone(), screen=Screen(), actor=Actor(), planner=Actor(), trace=Trace(None))
    agent.task = "t"
    asyncio.run(agent._step())
    step = agent.steps[-1]
    assert step["settle_s"] == 1.2 and step["stable"] is False and "changed" in step
    assert _key(step) == {"action": "tap", "x": 100, "y": 200}
    asyncio.run(agent._next_actions())
    assert "settle_s" not in Actor.prompts[-1] and "stable" not in Actor.prompts[-1]


def test_settle_tolerates_a_screen_that_returns_nothing():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46
        async def settled(self, **_):
            return None

    agent = Agent(phone=None, screen=Screen(), actor=None, planner=None, trace=Trace(None))
    agent.steps = [{"action": "tap", "x": 1, "y": 2}]
    asyncio.run(agent._settle())
    assert "settle_s" not in agent.steps[-1]


def test_frames_settled_reports_wait_and_stability():
    import asyncio
    import time

    from hidagent.screen import Screen

    quiet = Screen("Agent")  # never updated: already still
    waited, stable = asyncio.run(quiet.settled(quiet=0.05, least=0.0, most=0.5))
    assert stable is True and waited < 0.3
    busy = Screen("Agent")

    async def keep_changing():
        task = asyncio.ensure_future(busy.settled(quiet=0.2, least=0.0, most=0.3))
        for _ in range(5):
            busy.updated = time.time()
            await asyncio.sleep(0.05)
        return await task

    waited, stable = asyncio.run(keep_changing())
    assert stable is False and waited >= 0.3


def test_frames_settled_waits_for_the_first_frame_after_the_action():
    """#17-F on the device: the change reached the server after `least`, so the wait ended on the old frame."""
    import asyncio
    import time

    from hidagent.screen import Screen

    s = Screen("Agent")
    s.frame, s.updated = b"old", time.time() - 5  # still for a while before the action

    async def late_change():
        since = time.time()
        task = asyncio.ensure_future(s.settled(since=since, quiet=0.05, least=0.0, first=1.0))
        await asyncio.sleep(0.4)
        s.frame, s.updated = b"new", time.time()
        return await task, s.frame

    (waited, stable), seen = asyncio.run(late_change())
    assert waited >= 0.4 and stable is True and seen == b"new"


def test_frames_settled_gives_up_waiting_for_a_change_after_first():
    import asyncio
    import time

    from hidagent.screen import Screen

    s = Screen("Agent")
    s.frame, s.updated = b"old", time.time() - 5
    waited, stable = asyncio.run(s.settled(since=time.time(), quiet=0.05, least=0.0, first=0.3))
    assert 0.3 <= waited < 0.6 and stable is True  # the action changed nothing: no longer than `first`


def test_frames_settled_does_not_wait_when_the_change_already_arrived():
    import asyncio
    import time

    from hidagent.screen import Screen

    s = Screen("Agent")
    since = time.time() - 1
    s.frame, s.updated = b"new", time.time() - 0.5  # arrived during the action, before the wait began
    waited, stable = asyncio.run(s.settled(since=since, quiet=0.05, least=0.0, first=1.0))
    assert waited < 0.2 and stable is True


def test_actor_gets_screen_text_only_in_ocr_actor_mode(monkeypatch):
    import asyncio

    from hidagent.agent import Agent, _key
    from hidagent.debug import Trace
    from hidagent.ocr import OCR

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46

    class Actor:
        model = "fake"
        prompts = []
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            self.prompts.append(text)
            return "THINK: tap the row\nDO: tap 173 485"

    class FakeOCR:
        mode, last_s = "actor", 0.41
        async def read(self, jpeg):
            return [{"text": "Wi-Fi", "x": 0.173, "y": 0.485, "w": 0.1, "h": 0.02, "conf": 0.99}]
        lines = staticmethod(OCR.lines)

    agent = Agent(phone=None, screen=Screen(), actor=Actor(), planner=Actor(), trace=Trace(None), ocr=FakeOCR())
    agent.task = "turn on Wi-Fi"
    acts = asyncio.run(agent._next_actions())
    prompt = Actor.prompts[-1]
    assert '"Wi-Fi" @ 173 485' in prompt and "Text read from the screen" in prompt
    assert prompt.rstrip().endswith("Next action?")
    assert acts[0]["ocr_s"] == 0.41
    assert _key(acts[0]) == {"action": "tap", "x": 173, "y": 485}  # ocr_s and the shot are bookkeeping, not the action

    # api mode (or no OCR at all): the actor prompt is untouched
    FakeOCR.mode = "api"
    Actor.prompts.clear()
    asyncio.run(agent._next_actions())
    assert "Text read from the screen" not in Actor.prompts[-1]
    agent.steps = [{"action": "tap", "x": 1, "y": 2, "ocr_s": 0.4, "changed": True}]
    asyncio.run(agent._next_actions())
    assert "ocr_s" not in Actor.prompts[-1]  # bookkeeping never reaches the actor's history


def test_ocr_failure_never_costs_the_step(monkeypatch):
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46

    class Actor:
        model = "fake"
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            return "THINK: ok\nDO: home"

    class BrokenOCR:
        mode, last_s = "actor", 0.0
        async def read(self, jpeg):
            raise RuntimeError("model file missing")

    agent = Agent(phone=None, screen=Screen(), actor=Actor(), planner=Actor(), trace=Trace(None), ocr=BrokenOCR())
    agent.task = "t"
    assert asyncio.run(agent._next_actions())[0]["action"] == "home"


def test_start_launches_the_run_and_the_run_id_counts_runs():
    """Regression: a run counter once shared the name of the `_run` coroutine method, so start() raised
    "TypeError: 'int' object is not callable" and no task could ever start (found in the first device E2E)."""
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live = True

    async def go():
        agent = Agent(phone=None, screen=Screen(), actor=None, planner=None, trace=Trace(None))
        assert callable(agent._run)
        before = agent._run_id
        agent.start("noop")
        assert agent.running and agent._run_id == before + 1
        agent.stop()
        try:
            await agent._job
        except asyncio.CancelledError:
            pass
        assert not agent.running
        agent.clear()
        assert agent._run_id == before + 2

    asyncio.run(go())


@pytest.mark.parametrize("line", ["tap 63 1218", "tap 295 1046", "tap -5 100", "swipe 0 0 1001 5",
                                  "path 1 2 3 1200", "circle 500 500 1500"])
def test_parse_rejects_off_screen_coordinates(line):
    from hidagent.agent import OffScreen

    with pytest.raises(OffScreen):
        parse_command(line)


def test_parse_accepts_the_screen_edges():
    assert parse_command("tap 0 1000") == {"action": "tap", "x": 0, "y": 1000}


def test_actor_is_asked_again_after_an_off_screen_reply():
    """#17-C: the actor answered "tap 63 1218"; it must be told why and asked again, never acted on."""
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46

    class Actor:
        model = "fake"

        def __init__(self):
            self.prompts = []
            self.replies = ["THINK: the globe key\nDO: tap 63 1218", "THINK: the globe key\nDO: tap 63 975"]

        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            self.prompts.append(text)
            return self.replies.pop(0)

    actor = Actor()
    agent = Agent(phone=None, screen=Screen(), actor=actor, planner=actor, trace=Trace(None))
    agent.task = "t"
    acts = asyncio.run(agent._next_actions())
    assert [(a["action"], a["x"], a["y"]) for a in acts] == [("tap", 63, 975)]
    assert "inside the 1000x1000 screenshot" in actor.prompts[-1]  # no frame size here: taken as 1000x1000
    assert "not used" not in actor.prompts[0]


def test_actor_prompt_asks_to_check_the_item_name_before_done():
    """#17 on-device: a mis-tap opened the neighbouring TestFlight row and the actor reported that app's version."""
    from hidagent.agent import ACTOR_PROMPT

    done = ACTOR_PROMPT[ACTOR_PROMPT.index("  done ANSWER"):ACTOR_PROMPT.index("  confirm REASON")]
    assert "exactly the item the goal names" in done and "go back" in done


def test_agent_settle_waits_for_a_frame_after_its_own_action(monkeypatch):
    import asyncio
    import time

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    monkeypatch.setattr("hidagent.agent.vision.changed", lambda *a, **k: True)
    seen = {}

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46
        async def settled(self, **kw):
            seen.update(kw)
            return 0.3, True

    class Phone:
        async def tap(self, x, y, aim=False):
            seen["tapped_at"] = time.time()

    class Actor:
        model = "fake"
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            return "THINK: tap it\nDO: tap 100 200"

    agent = Agent(phone=Phone(), screen=Screen(), actor=Actor(), planner=Actor(), trace=Trace(None))
    agent.task = "t"
    asyncio.run(agent._step())
    assert seen["since"] is not None and seen["since"] <= seen["tapped_at"]  # from before the action, not after


def test_settle_options_read_only_the_set_variables():
    from hidagent.agent import settle_options

    assert settle_options({}) == {}
    assert settle_options({"SETTLE_FIRST": "0.6", "SETTLE_QUIET": " ", "SETTLE_MOST": "2"}) == {"first": 0.6,
                                                                                                "most": 2.0}


def test_agent_settle_passes_the_configured_waits_to_the_screen():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    seen = {}

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46
        async def settled(self, **kw):
            seen.update(kw)
            return 0.2, True

    agent = Agent(phone=None, screen=Screen(), actor=None, planner=None, trace=Trace(None),
                  settle={"first": 0.6, "quiet": 0.2})
    agent._acted_at = 123.0
    asyncio.run(agent._settle())
    assert seen == {"since": 123.0, "first": 0.6, "quiet": 0.2}


def _actor_prompt_for(frame: bytes, image_max: int = 0) -> str:
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live, aspect = True, 0.46

    Screen.frame = frame

    class Actor:
        model = "fake"

        def __init__(self):
            self.image_max, self.prompts = image_max, []

        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            self.prompts.append(text)
            return "THINK: tap it\nDO: tap 100 100"

    actor = Actor()
    agent = Agent(phone=None, screen=Screen(), actor=actor, planner=actor, trace=Trace(None))
    agent.task = "t"
    asyncio.run(agent._next_actions())
    return actor.prompts[0]


def _jpeg(w: int, h: int) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (w, h), (10, 10, 10)).save(out, "JPEG")
    return out.getvalue()


def test_actor_is_told_the_pixel_size_it_sees_and_answers_in_it():
    """#17 on-device: the actor tapped (57, 121), the back button's centre in pixels of a 590x1280 frame.

    Pixels are now the actor's unit; the server converts them."""
    prompt = _actor_prompt_for(_jpeg(590, 1280))
    assert "The screenshot is 590x1280 pixels. Give positions as pixels on it" in prompt
    assert prompt.rstrip().endswith("Next action?")


def test_pixel_size_is_the_shrunk_one_when_image_max_side_is_set():
    assert "The screenshot is 354x768 pixels" in _actor_prompt_for(_jpeg(590, 1280), image_max=768)


def test_no_pixel_size_line_when_the_frame_cannot_be_read():
    assert "pixels. Give positions" not in _actor_prompt_for(b"jpeg")


def test_actor_prompt_never_asks_for_another_unit_than_pixels():
    from hidagent.agent import ACTOR_PROMPT

    assert "NOT pixels" not in ACTOR_PROMPT and "never rescale" in ACTOR_PROMPT


def test_parse_longpress_and_drag():
    assert parse_command("longpress 500 300") == {"action": "longpress", "x": 500, "y": 300}
    assert parse_command("longpress 500 300 precise")["precise"] is True
    assert parse_command("drag 150 350 400 350") == {"action": "drag", "x1": 150, "y1": 350, "x2": 400, "y2": 350}
    with pytest.raises(ValueError):
        parse_command("drag 150 350 400")  # needs both points


def test_longpress_and_drag_reach_the_phone():
    import asyncio

    from hidagent.agent import Agent
    from hidagent.debug import Trace
    from hidagent.phone import DROP, PICK_UP

    calls = []

    class Phone:
        async def long_press(self, x, y, aim=False):
            calls.append(("long_press", x, y, aim))

        async def drag(self, points, hold=True, pick=0.0, rest=None, aim=False):
            calls.append(("drag", points, pick, rest, aim))

    agent = Agent(phone=Phone(), screen=None, actor=None, planner=None, trace=Trace(None))
    asyncio.run(agent._do({"action": "longpress", "x": 500, "y": 300}))
    asyncio.run(agent._do({"action": "drag", "x1": 150, "y1": 350, "x2": 400, "y2": 350}))
    assert calls[0] == ("long_press", 0.5, 0.3, False)
    # an item is lifted, carried and rested on its target; the pick-up point is aimed, the wrong icon costs more
    assert calls[1] == ("drag", [(0.15, 0.35), (0.4, 0.35)], PICK_UP, DROP, True)


def test_longpress_and_drag_pointer_paths():
    from hidagent.agent import _pointer_path

    assert _pointer_path({"action": "longpress", "x": 500, "y": 300}) == [(0.5, 0.3)]
    assert _pointer_path({"action": "drag", "x1": 150, "y1": 350, "x2": 400, "y2": 350}) == [(0.15, 0.35),
                                                                                              (0.4, 0.35)]


def test_every_screen_action_is_checked_by_the_gate_and_taught_to_the_actor():
    from hidagent.agent import ACTIONS, ACTOR_PROMPT
    from hidagent.gate import CHECKED, describe

    assert {"longpress", "drag"} <= set(CHECKED)
    assert "longpress X Y" in ACTOR_PROMPT and "drag X1 Y1 X2 Y2" in ACTOR_PROMPT
    assert {"longpress", "drag"} <= set(ACTIONS)
    assert "gesture" in describe({"action": "drag"}) and "long press" in describe({"action": "longpress"})


def test_actor_is_told_never_to_touch_the_delete_badge_in_edit_mode():
    from hidagent.agent import ACTOR_PROMPT

    text = " ".join(ACTOR_PROMPT.split()).lower()
    assert "minus" in text and "remove app" in text


def test_actor_knows_how_to_rename_a_folder_and_which_menu_entries_to_avoid():
    from hidagent.agent import ACTOR_PROMPT

    text = " ".join(ACTOR_PROMPT.split()).lower()
    assert "rename a home screen folder" in text and "choose rename" in text
    assert "key cmd+a" in text and "key enter" in text and "folder's title" in text
    assert "remove from home screen" in text


def _batch_agent(monkeypatch, reply):
    """An agent whose actor answers one fixed reply; returns (agent, calls the phone received)."""
    from hidagent.agent import Agent
    from hidagent.debug import Trace

    monkeypatch.setattr("hidagent.agent.vision.changed", lambda *a, **k: True)  # frames here are not real JPEGs
    calls = []

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46
        async def settled(self, **_):
            return 0.3, True

    class Phone:
        async def tap(self, x, y, aim=False):
            calls.append(("tap", x, y))
        async def key(self, combo):
            calls.append(("key", combo))
        async def type(self, text):
            calls.append(("type", text))

    class Actor:
        model = "fake"
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            return reply

    agent = Agent(phone=Phone(), screen=Screen(), actor=Actor(), planner=Actor(), trace=Trace(None))
    agent.task = "t"
    return agent, calls


def test_typing_ends_the_batch_so_the_actor_sees_the_text_before_enter(monkeypatch):
    # #36 device run: cmd+a, backspace, type 날씨, enter in one reply typed "skfTl" (English input language)
    # and confirmed it unseen; the "switch with ctrl+space" rule never had a chance
    import asyncio

    agent, calls = _batch_agent(monkeypatch, "THINK: rename\nDO: key cmd+a\nDO: key backspace\nDO: type 날씨\n"
                                             "DO: key enter")
    assert asyncio.run(agent._step()) is False
    assert calls == [("key", "cmd+a"), ("key", "backspace"), ("type", "날씨")]  # enter waits for a look
    assert agent.steps[-1]["action"] == "type" and agent.steps[-1]["changed"] is True


def test_done_after_actions_in_the_same_reply_is_not_taken(monkeypatch):
    # done must rest on a screenshot taken after the last action, not on the one the batch was planned on
    import asyncio

    agent, calls = _batch_agent(monkeypatch, "THINK: close it\nDO: tap 500 950\nDO: done Renamed")
    assert asyncio.run(agent._step()) is False
    assert calls == [("tap", 0.5, 0.95)]
    assert [s["action"] for s in agent.steps] == ["tap"]


def test_done_alone_still_ends_the_run(monkeypatch):
    import asyncio

    agent, calls = _batch_agent(monkeypatch, "THINK: the label reads 날씨\nDO: done Renamed")
    assert asyncio.run(agent._step()) is True and calls == [] and agent.steps[-1]["action"] == "done"


def test_actor_is_told_to_trust_the_screenshot_and_to_avoid_the_dynamic_island():
    from hidagent.agent import ACTOR_PROMPT

    text = " ".join(ACTOR_PROMPT.split()).lower()
    assert "never assume it is outdated" in text
    assert "dynamic island" in text and "stop screen sharing" in text


# --- pixel positions (#38 device re-test): the actor answers in pixels of the screenshot it sees ---------------


def test_pixel_positions_are_stored_as_0_1000():
    # the 2026-10-17 run: the 날씨 folder at pixel (226, 169) of a 590x1280 frame is (383, 132) in 0-1000
    assert parse_command("longpress 226 169", (590, 1280)) == {"action": "longpress", "x": 383, "y": 132}
    assert parse_command("swipe 295 1100 295 900", (590, 1280)) == {"action": "swipe", "x1": 500, "y1": 859,
                                                                     "x2": 500, "y2": 703}
    assert parse_command("path 0 0 590 1280", (590, 1280))["points"] == [[0, 0], [1000, 1000]]
    assert parse_command("circle 295 640 59", (590, 1280)) == {"action": "circle", "x": 500, "y": 500, "r": 100}
    assert parse_command("tap 590 1280", (590, 1280)) == {"action": "tap", "x": 1000, "y": 1000}  # the edges


@pytest.mark.parametrize("line", ["tap 591 100", "tap 100 1281", "tap -1 5", "swipe 0 0 600 5", "path 1 2 3 1300"])
def test_pixel_positions_outside_the_screenshot_are_refused(line):
    from hidagent.agent import OffScreen

    with pytest.raises(OffScreen, match="590x1280"):
        parse_command(line, (590, 1280))


def test_a_y_beyond_1000_is_fine_on_a_tall_screenshot():
    # "tap 63 1218" (#17-C) was refused as off-screen in 0-1000 units; in pixels of 590x1280 it is on screen
    assert parse_command("tap 63 1218", (590, 1280)) == {"action": "tap", "x": 107, "y": 952}


def _pixel_agent(monkeypatch, replies, steps=()):
    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Screen:
        live, frame, aspect = True, b"jpeg", 0.46

    class Actor:
        model = "fake"
        prompts = []
        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            self.prompts.append(text)
            return replies.pop(0)

    agent = Agent(phone=None, screen=Screen(), actor=Actor(), planner=Actor(), trace=Trace(None))
    monkeypatch.setattr(agent, "_shown_size", lambda: (590, 1280))
    agent.task, agent.steps = "t", list(steps)
    return agent, Actor.prompts


def test_actor_answers_are_read_as_pixels_and_its_history_is_shown_in_pixels(monkeypatch):
    import asyncio

    agent, prompts = _pixel_agent(monkeypatch, ["THINK: the folder\nDO: longpress 226 169"],
                                  [{"action": "tap", "x": 500, "y": 500, "changed": True}])
    acts = asyncio.run(agent._next_actions())
    assert (acts[0]["x"], acts[0]["y"]) == (383, 132)
    assert '"x": 295, "y": 640' in prompts[-1]  # its own unit, never the internal 0-1000


def test_actor_screen_text_is_given_in_pixels(monkeypatch):
    from hidagent.ocr import OCR

    items = [{"text": "Wi-Fi", "x": 0.5, "y": 0.25}]
    assert OCR.lines(items, (590, 1280)) == '"Wi-Fi" @ 295 320'
    assert OCR.lines(items) == '"Wi-Fi" @ 500 250'  # the old single scale still works


def test_a_third_identical_tap_or_long_press_is_sent_back(monkeypatch):
    # the 2026-10-17 run: longpress (226,169) opened the wrong app's menu, a tap closed it, and the same long press
    # came again and again; the guard only compared with the very last action
    import asyncio

    same = {"action": "longpress", "x": 383, "y": 132, "changed": True}
    close = {"action": "tap", "x": 500, "y": 600, "changed": True}
    agent, prompts = _pixel_agent(monkeypatch, ["THINK: again\nDO: longpress 226 169",
                                                "THINK: measure again\nDO: longpress 226 300"],
                                  [same, close, same, close])
    acts = asyncio.run(agent._next_actions())
    assert (acts[0]["x"], acts[0]["y"]) == (383, 234)
    assert "twice" in prompts[-1] and "longpress 226 169" in prompts[-1]


def test_actor_prompt_asks_for_pixel_positions():
    from hidagent.agent import ACTOR_PROMPT

    text = " ".join(ACTOR_PROMPT.split()).lower()
    assert "pixels on the screenshot" in text and "0-1000" not in text
