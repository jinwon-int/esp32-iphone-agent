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
        async def settled(self):
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
        async def settled(self):
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
        async def settled(self):
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
    assert "0-1000" in actor.prompts[-1] and "0-1000" not in actor.prompts[0].split("Next action?")[-1]
