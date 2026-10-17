import asyncio
import json
import time

import pytest

from hidagent import gate as gate_mod
from hidagent.agent import Agent, _key
from hidagent.debug import Trace
from hidagent.gate import QUESTIONS, Gate, describe, summarize

ANSWERS = {"risk": {"type": "choice", "choice": "confirm", "probabilities": {"safe": 0.1, "confirm": 0.8,
                                                                            "block": 0.1}, "confidence": 0.7},
           "on_goal": {"type": "noul", "noul": 0.9}}


class FakeClient:
    def __init__(self, answers=ANSWERS, delay=0.0, error=None):
        self.answers, self.delay, self.error, self.calls = answers, delay, error, []

    def ask(self, state, questions):  # jevlib.JevClient.ask is synchronous (urllib)
        self.calls.append((state, questions))
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answers, {"latency_ms": 42, "model": "jev-latest", "attempts": 1, "error": None}


class FakeLedger:
    def __init__(self):
        self.records = []

    def append(self, **record):
        self.records.append(record)


def make_gate(**kw):
    g = Gate(FakeClient(**kw), FakeLedger())
    g.new_session()
    return g


# --- the questions and the summary ------------------------------------------------------------------------------


def test_questions_follow_the_jev_shape():
    assert QUESTIONS["risk"]["type"] == "choice" and set(QUESTIONS["risk"]["criteria"]) == {"safe", "confirm",
                                                                                              "block"}
    assert QUESTIONS["on_goal"]["type"] == "noul" and QUESTIONS["on_goal"]["instructions"]


@pytest.mark.parametrize("answers,would", [
    (ANSWERS, "confirm"),
    ({**ANSWERS, "risk": {"choice": "block", "probabilities": {"block": 0.9}}}, "block"),
    ({**ANSWERS, "risk": {"choice": "confirm", "probabilities": {"confirm": 0.4}}}, "allow"),  # under MIN_CONFIDENCE
    ({"risk": {"choice": "safe", "probabilities": {"safe": 0.9}}, "on_goal": {"noul": 0.1}}, "confirm"),  # off goal
    ({"risk": {"choice": "safe", "probabilities": {"safe": 0.9}}, "on_goal": {"noul": 0.8}}, "allow"),
    ({}, "allow"),  # an empty answer is not a reason to stop anything
])
def test_summary_says_what_an_apply_gate_would_do(answers, would):
    assert summarize(answers, {"latency_ms": 1}, None)["would"] == would


def test_failure_summary():
    assert summarize({}, {}, "http 401") == {"would": "unavailable", "error": "http 401"}


def test_action_is_described_in_words():
    assert describe({"action": "type", "text": 'say "hi"'}) == 'type the text "say \\"hi\\""'
    assert "THOUGHT" in describe({"action": "tap", "x": 1, "y": 2})
    assert describe({"action": "key", "combo": "cmd+h"}) == "press the keys cmd+h"


# --- the gate on its own ----------------------------------------------------------------------------------------


def test_check_asks_jev_and_records_two_observe_rows():
    g = make_gate()

    async def scenario():
        task = g.check("Send hi to Mom", "1. open Messages", "The Send button is visible", {"action": "tap",
                       "x": 900, "y": 950}, ["type the text \"hi\""])
        return await task

    result = asyncio.run(scenario())
    assert result == {"would": "confirm", "risk": "confirm", "p": 0.8, "on_goal": 0.9, "ms": 42}
    state, questions = g.client.calls[0]
    assert questions is QUESTIONS
    assert state["GOAL"] == "Send hi to Mom" and state["THOUGHT"] == "The Send button is visible"
    assert state["RECENT_ACTIONS"] == ['type the text "hi"']
    rows = g.ledger.records
    assert [r["question_id"] for r in rows] == ["risk", "on_goal"]
    assert all(r["gate_action"] == "observe" and r["domain"] == gate_mod.DOMAIN and r["session_id"] == g.session
               and r["decision_point"] == "action.tap" and r["prompt_version"] == gate_mod.PROMPT_VERSION
               for r in rows)
    assert rows[0]["decision"] == "confirm" and rows[0]["probability"] == 0.8 and rows[1]["decision"] == 0.9


@pytest.mark.parametrize("action", ["wait", "done", "fail", "replan"])
def test_non_actions_are_not_checked(action):
    async def scenario():
        return make_gate().check("g", "p", "t", {"action": action}, [])

    assert asyncio.run(scenario()) is None


def test_jev_failure_is_recorded_not_raised():
    g = make_gate(error=RuntimeError("jev ask failed after 1 attempt(s): http 401"))
    result = asyncio.run(g._check({"ACTION": "x"}, "action.tap", "s"))
    assert result["would"] == "unavailable" and "401" in result["error"]
    assert [r["error"] for r in g.ledger.records] == [result["error"]] * 2


def test_slow_jev_times_out_quickly():
    g = Gate(FakeClient(delay=2), FakeLedger(), timeout=0.2)
    started = time.time()
    result = asyncio.run(g._check({"ACTION": "x"}, "action.tap", "s"))
    assert result["would"] == "unavailable" and time.time() - started < 2.5


def test_ledger_failure_does_not_break_the_check():
    class Broken:
        def append(self, **_):
            raise OSError("disk full")

    result = asyncio.run(Gate(FakeClient(), Broken())._check({"ACTION": "x"}, "action.tap", "s"))
    assert result["would"] == "confirm"


def test_gate_is_off_unless_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("GATE_MODE", raising=False)
    assert Gate.from_env(tmp_path) is None
    monkeypatch.setenv("GATE_MODE", "apply")
    with pytest.raises(ValueError, match="not implemented"):
        Gate.from_env(tmp_path)


def test_gate_stays_off_without_jevlib(tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_MODE", "observe")
    monkeypatch.setattr(gate_mod, "load_jevlib", lambda path=None: None)
    assert Gate.from_env(tmp_path) is None


def test_with_the_real_jevlib_when_present(tmp_path, monkeypatch):
    """Fleet nodes have the shared jevlib: drive it with a fake transport (no network, no key)."""
    jevlib = gate_mod.load_jevlib()
    if jevlib is None:
        pytest.skip("jevlib not installed here")
    body = json.dumps({"answers": ANSWERS}).encode()
    client = jevlib.JevClient(key="test-key-not-real", transport=lambda _: (200, body), retries=1)
    ledger = jevlib.DecisionLedger(str(tmp_path / "decisions.jsonl"))
    g = Gate(client, ledger)
    g.new_session()
    result = asyncio.run(g._check({"GOAL": "g", "ACTION": "tap"}, "action.tap", g.session))
    assert result["would"] == "confirm"
    rows = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert [r["schema"] for r in rows] == ["jev.decision.v1"] * 2
    assert rows[0]["decision"] == "confirm" and rows[0]["gate_action"] == "observe"
    assert (tmp_path / "decisions.jsonl").stat().st_mode & 0o777 == 0o600
    assert "test-key-not-real" not in (tmp_path / "decisions.jsonl").read_text()


# --- the gate inside the agent: observe means "changes nothing" ----------------------------------------------


class Phone:
    def __init__(self):
        self.done = []

    async def tap(self, x, y, aim=False):
        self.done.append(("tap", round(x, 2), round(y, 2)))

    async def type(self, text):
        self.done.append(("type", text))


class Screen:
    live, frame, aspect = True, b"jpeg", 0.46

    async def settled(self, **_):
        pass


class Actor:
    model = "fake"

    def __init__(self):
        self.prompts = []

    async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
        self.prompts.append(text)
        # (type ends a reply, #36: the field tap comes first so each round still runs two actions)
        return "THINK: I tap the message field and type.\nDO: tap 500 900\nDO: type hello"


def run_steps(gate, monkeypatch, rounds=2, pause=0.0):
    monkeypatch.setattr("hidagent.agent.vision.changed", lambda *a, **k: True)
    phone, actor = Phone(), Actor()
    agent = Agent(phone=phone, screen=Screen(), actor=actor, planner=actor, trace=Trace(None), gate=gate)
    agent.task, agent.plan = "Send hello to Mom", "1. open Messages 2. send"
    checks = []
    if gate:
        real = gate.check
        gate.check = lambda *a: checks.append(real(*a)) or checks[-1]

    async def scenario():
        timings = []
        for _ in range(rounds):
            started = time.time()
            await agent._step()
            timings.append(time.time() - started)
            await asyncio.sleep(pause)  # pause > 0: the gate has answered before the next round
        if checks:
            await asyncio.gather(*checks)
        return timings

    timings = asyncio.run(scenario())
    return agent, phone, actor, timings


def test_observe_gate_changes_nothing_the_agent_does(monkeypatch):
    without, phone0, actor0, _ = run_steps(None, monkeypatch)
    g = make_gate(delay=0.5)  # a slow gate: the agent must not wait for it
    with_gate, phone1, actor1, timings = run_steps(g, monkeypatch)
    assert phone1.done == phone0.done  # same actions on the phone
    strip = [{k: v for k, v in s.items() if k not in ("t", "gate", "act_s", "think_s")} for s in with_gate.steps]
    assert strip == [{k: v for k, v in s.items() if k not in ("t", "act_s", "think_s")} for s in without.steps]
    assert actor1.prompts == actor0.prompts  # the gate's opinion never reaches the actor
    assert max(timings) < 0.4  # not delayed by the 0.5 s gate
    assert [s["gate"]["would"] for s in with_gate.steps] == ["confirm"] * 4  # attached once it answered
    assert len(g.client.calls) == 4 and len(g.ledger.records) == 8


def test_gate_answer_never_reaches_the_actor(monkeypatch):
    """The gate answers BEFORE the next round: its opinion must still not appear in the actor's prompt."""
    _, _, actor0, _ = run_steps(None, monkeypatch, pause=0.1)
    with_gate, _, actor1, _ = run_steps(make_gate(), monkeypatch, pause=0.1)
    assert "gate" in with_gate.steps[0]  # it had answered in time
    assert actor1.prompts == actor0.prompts and "would" not in actor1.prompts[1]


def test_gate_answer_from_an_old_run_is_dropped(monkeypatch):
    g = make_gate(delay=0.3)
    agent = Agent(phone=Phone(), screen=Screen(), actor=None, planner=None, trace=Trace(None), gate=g)

    async def scenario():
        agent.steps = [{"action": "tap", "x": 1, "y": 2}]
        task = g.check("g", "p", "t", {"action": "tap", "x": 1, "y": 2}, [])
        agent._attach(task, 0)
        agent.clear()  # the user cleared the run (or started a new one) before the answer came
        agent.steps = [{"action": "tap", "x": 5, "y": 5}]
        await task
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert "gate" not in agent.steps[0]


def test_gate_field_is_not_part_of_the_action():
    assert _key({"action": "tap", "x": 1, "y": 2, "gate": {"would": "allow"}}) == {"action": "tap", "x": 1, "y": 2}
