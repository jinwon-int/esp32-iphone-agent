"""See-think-act loop: a planner model writes the plan, an actor model picks each step."""

import asyncio
import json
import logging
import math
import re
import time

from . import vision
from .debug import Trace
from .device import DeviceError
from .gate import Gate
from .gate import describe as gate_describe
from .llm import LLM
from .phone import HOME_SWIPE, Phone
from .screen import Screen

log = logging.getLogger(__name__)

PLANNER_PROMPT = """You plan tasks on an iPhone. Given the goal and the current screen,
write a short numbered plan (at most 6 steps) of what to do on screen. Plain text only, in English.
Plan only what the goal needs, from what you can see; never invent accounts, emails, logins or
screens you have not seen. Later steps can stay general (e.g. "Draw the face on the canvas").
To open an app, prefer search over paging through home screens: tap the "Search" pill above
the dock (or swipe down from the middle of the home screen), type the app name, tap the result.
If the goal involves paying or buying, sending a message or email, deleting the user's own items
(messages, notes, photos, cart items), or changing account or security settings, plan up to the
final review screen and end the plan with "stop for the user to confirm": the user does the last tap."""

ACTOR_PROMPT = """You operate an iPhone through a mouse pointer. You see the current screen.
Reply in this format, nothing else:
THINK: <plain English a non-technical user understands: first check your last action against the
       screen (did it do what you intended?), then what you do now and why>
DO: <action>

Give several DO lines (up to 6, run in order) whenever the next actions do not depend on seeing the
result: e.g. ticking several visible checkboxes or days, several drawing strokes, typing then pressing enter.
Give ONE DO line when the next action depends on what this one opens or changes.

Coordinates are integers 0-1000 relative to the screenshot (x left->right, y top->bottom).
Aim at the visual center of the element you want, not its edge or its label.
Look before you act: describe what is really on the screen now, not what you expect.
Each recent action says whether the screen changed after it. If it did NOT change, the action
missed or had no effect: do not assume it worked; aim again more precisely or try another way.
Only type when a text field is visibly focused (the keyboard is showing).
Swipes have no momentum: content moves exactly as far as the finger and stops. Prefer short swipes
(100-250 units) and check the result; repeat if needed rather than one big swipe.
Time or number wheels (e.g. an alarm's time): first tap the number in the wheel's center row and type
the digits (e.g. type 0700); check the result. Only if typing does not work, swipe inside that column:
measure the row spacing from two neighbouring numbers and swipe k times that spacing to move k rows.
Looking for an item in a list: scroll less than one screen at a time so nothing is skipped, and read every
visible item. If you reached both ends of the list and the item is not there, stop with fail and name the
closest options you saw; do not keep scrolling.
Only tap things you can actually see on this screenshot; never guess where a button might be.
If you are unsure what an element does, tap it alone (one DO line) and check the result next round.
Stop before anything that cannot be undone: never tap a final Pay, Buy, Place order, Send, Delete,
Confirm or Sign out button, and never change account, password or security settings. Do everything
up to that point, then reply "confirm" with what is ready; the user does the last tap.
Actions:
  tap X Y [precise]         (precise: the pointer is located and corrected before tapping, about 1 s slower;
                             use it for small or tightly packed targets: list rows, checkmarks, toggles, small icons)
  swipe X1 Y1 X2 Y2         (finger moves from X1,Y1 to X2,Y2: "swipe down" means Y2 > Y1)
  path X1 Y1 X2 Y2 X3 Y3 …  (one continuous stroke through 2-20 points, for drawing)
  circle X Y R              (draw a circle centered at X Y; R is the radius in x units)
  type TEXT                 (ASCII only; tap the input field first. If typed letters come out in another
                             alphabet, e.g. Korean, the keyboard language is wrong: delete them, switch with
                             key ctrl+space, check the keyboard, then type again)
  key COMBO                 (enter, backspace, esc, cmd+a, ctrl+space = next keyboard language, ...)
  home | back | wait
  replan                    (the plan no longer fits the screen)
  done ANSWER               (the goal is reached; ANSWER is the result for the user)
  confirm REASON            (the next step would pay, send, delete or change account settings: stop here
                             and say what is ready for the user to confirm; never do that step yourself)
  fail REASON

Example:
THINK: My last tap opened the home screen as intended. The App Store icon is visible, so I tap it.
DO: tap 152 362"""

META = ("t", "thought", "thought_zh", "think_s", "act_s", "shot", "_shot", "changed", "settle_s", "stable",
        "ocr_s", "gate")  # not the action

# action name -> argument names; numeric arguments are 0-1000 screen coordinates
ACTIONS = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"), "circle": ("x", "y", "r"), "path": ("points",),
           "type": ("text",), "key": ("combo",), "done": ("answer",), "fail": ("reason",), "confirm": ("reason",),
           "home": (), "back": (), "wait": (), "replan": ()}
TERMINAL = ("done", "fail", "confirm")  # the run ends here; confirm hands the last step to the user
TEXT_ARGS = ("text", "combo", "answer", "reason")


class OffScreen(ValueError):
    """A coordinate outside 0-1000: the actor misjudged the screen; ask it again instead of acting off-screen."""


def parse_command(line: str) -> dict:
    """One DO line, e.g. "tap 152 362" -> {"action": "tap", "x": 152, "y": 362}."""
    name, _, rest = line.strip().partition(" ")
    name = name.lower()
    if name not in ACTIONS:
        raise ValueError(f"unknown action: {line[:100]}")
    keys = ACTIONS[name]
    if keys and keys[0] in TEXT_ARGS:
        return {"action": name, keys[0]: rest.strip().strip('"')}
    numbers = [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", rest)]
    if any(not 0 <= n <= 1000 for n in numbers):  # e.g. "tap 63 1218" (#17-C): never act off the screen
        raise OffScreen(f"{name} coordinates must be 0-1000: {line[:100]}")
    if keys == ("points",):
        if len(numbers) < 4 or len(numbers) % 2:
            raise ValueError(f"path needs 2+ x y pairs: {line[:100]}")
        return {"action": name, "points": [numbers[i:i + 2] for i in range(0, len(numbers), 2)][:20]}
    if len(numbers) != len(keys):
        raise ValueError(f"{name} needs {len(keys)} numbers: {line[:100]}")
    action = {"action": name, **dict(zip(keys, numbers, strict=True))}
    if name == "tap" and "precise" in rest.lower():
        action["precise"] = True
    return action


def parse_actions(text: str) -> list[dict]:
    """Parse a THINK / DO… reply (an optional ZH line is kept); the thought goes on the first action."""
    thought = re.search(r"^\s*THINK\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    zh = re.search(r"^\s*ZH\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    acts = [parse_command(line) for line in re.findall(r"^\s*DO\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)][:6]
    if not acts:
        raise ValueError(f"no DO line in reply: {text[:200]}")
    acts[0] = {"thought": thought.group(1) if thought else "", "thought_zh": zh.group(1) if zh else "", **acts[0]}
    return acts


def parse_plan(text: str) -> tuple[str, str]:
    """Split an optional EN: / ZH: plan reply; a plain reply is used for both."""
    m = re.search(r"EN\s*[:：](.*?)ZH\s*[:：](.*)", text, re.S | re.I)
    return (m.group(1).strip(), m.group(2).strip()) if m else (text.strip(), text.strip())


def _key(step: dict) -> dict:
    """The action itself, without thoughts, timings and other bookkeeping."""
    return {k: v for k, v in step.items() if k not in META}


class BlindError(RuntimeError):
    """The screen feed is gone; acting now would be guessing."""


class Agent:
    def __init__(self, phone: Phone, screen: Screen, actor: LLM, planner: LLM, trace: Trace,
                 max_steps: int = 40, max_failures: int = 3, gate: Gate | None = None, ocr=None):
        self.phone, self.screen, self.actor, self.planner = phone, screen, actor, planner
        self.trace = trace
        self.gate = gate  # optional second opinion on each action (observe only: never changes what is done)
        self.ocr = ocr  # optional: with OCR_MODE=actor the actor also gets the screen's text with coordinates
        self._run_id = 0  # bumped per run, so a late gate answer never lands in a newer run's steps
        # (not `_run`: that name is the coroutine method below; a counter there made start() raise TypeError)
        self.max_steps, self.max_failures = max_steps, max_failures
        self.task, self.plan, self.steps = "", "", []
        self.shots: dict[int, bytes] = {}  # step index -> screenshot the model decided on
        self._pointer = (0.0, 0.0)  # last pointer position (screen fractions)
        self._job: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._job is not None and not self._job.done()

    def start(self, task: str) -> None:
        if self.running:
            raise RuntimeError("agent already running")
        if not self.screen.live:
            raise RuntimeError("no phone screen: start Screen Mirroring first")
        self.task, self.plan, self.steps, self.shots = task, "", [], {}
        self._pointer = (0.0, 0.0)
        self._run_id += 1
        if self.gate:
            self.gate.new_session()
        self.trace.start(task)
        self._job = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self.running:
            self._job.cancel()

    def clear(self) -> None:
        if self.running:
            raise RuntimeError("stop the agent first")
        self.task, self.plan, self.steps, self.shots = "", "", [], {}
        self._run_id += 1

    def _record(self, shot: bytes | None = None, **step) -> None:
        if shot:
            self.shots[len(self.steps)] = shot
        self.steps.append({"t": time.time(), "shot": bool(shot), **step})
        self.trace.write(kind="step", **step)
        log.info("agent: %s", step)

    async def _ask(self, role: str, llm: LLM, system: str, prompt: str,
                   temperature: float = 0) -> tuple[str, float, bytes | None]:
        if not self.screen.live:  # never let the model act blind
            raise BlindError("lost the phone screen: reconnect Screen Mirroring")
        frame, started = self.screen.frame, time.time()
        reply = await llm.chat(system, prompt, frame, temperature=temperature)
        seconds = round(time.time() - started, 2)
        self.trace.write(frame, kind="llm", role=role, model=llm.model, prompt=prompt,
                         reply=reply, seconds=seconds)
        return reply, seconds, frame

    async def _run(self) -> None:
        failures = 0
        try:
            await self._replan()
            for _ in range(self.max_steps):
                try:
                    if await self._step():
                        return
                    failures = 0
                except BlindError:
                    raise
                except Exception as e:  # a bad reply or a dropped link: recover, don't crash
                    self._record(action="hiccup", reason=str(e)[:300])
                    board_drop = isinstance(e, DeviceError) and await self.phone.device.wait_connected(15)
                    failures += 0 if board_drop else 1  # the board came back: not the agent's fault
                    if failures >= self.max_failures:
                        self._record(action="fail", reason=f"{failures} errors in a row")
                        return
                    await self.screen.settled()
                    await self._replan(stuck=True)
            self._record(action="fail", reason="step limit reached")
        except asyncio.CancelledError:
            self._record(action="stopped")
        except Exception as e:  # e.g. the planner itself is unreachable
            self._record(action="error", reason=repr(e))

    async def _step(self) -> bool:
        """One see-think-act round (one or more actions). Returns True when the task is over."""
        acts = await self._next_actions()
        seen = acts[0]["_shot"]
        spots = [self._pointer]  # where the pointer was and goes: pointer moves are not screen changes
        stuck = self._repeating(acts[0])
        thought = acts[0].get("thought", "")
        for i, act in enumerate(acts):
            name = act["action"]
            if name == "replan" or stuck:
                self._record(act.pop("_shot"), **act)
                await self._replan(stuck=stuck)
                return False
            check = None
            if name not in TERMINAL:
                check = self._gate_check(thought, act)  # runs alongside the action, never waited for
                started = time.time()
                await self._do(act)
                act["act_s"] = round(time.time() - started, 2)
                spots += _pointer_path(act, getattr(getattr(self.phone, "home", None), "path", None))
                self._pointer = spots[-1]
            self._record(act.pop("_shot"), **act)
            if check:
                self._attach(check, len(self.steps) - 1)
            if name in TERMINAL:
                return True
            following = acts[i + 1]["action"] if i < len(acts) - 1 else None
            if name == "tap" and following in ("type", "key"):  # typing into a field that a missed tap never opened
                await self._settle()
                if not vision.changed(seen, self.screen.frame, ignore=tuple(spots)):
                    self.steps[-1]["changed"] = False
                    return False  # skip the rest; the actor looks again
        await self._settle()
        # feedback for the next round
        self.steps[-1]["changed"] = vision.changed(seen, self.screen.frame, ignore=tuple(spots))
        return False

    async def _settle(self) -> None:
        """Wait for the screen to stop changing and note on the last step how long that took and whether it did.

        Measurement only (never shown to the actor or the gate): ``settle_s`` is the wait, ``stable`` False when
        the screen was still changing when the wait gave up, e.g. a loading screen the next decision was made on.
        """
        result = await self.screen.settled()
        if isinstance(result, tuple) and self.steps:
            self.steps[-1]["settle_s"], self.steps[-1]["stable"] = result

    def _gate_check(self, thought: str, act: dict) -> "asyncio.Task | None":
        if not self.gate:
            return None
        try:
            recent = [gate_describe(s) for s in self.steps if s.get("action") in ACTIONS and "plan" not in s][-4:]
            return self.gate.check(self.task, self.plan, thought, _key(act), recent)
        except Exception as e:  # the gate must never break a run
            log.warning("action gate: check not started: %s", e)
            return None

    def _attach(self, check: asyncio.Task, index: int) -> None:
        """Put the gate's answer on the step when it arrives (the page shows it on its next poll)."""
        run = self._run_id

        def done(task: asyncio.Task) -> None:
            if task.cancelled() or task.exception() or run != self._run_id or index >= len(self.steps):
                return
            self.steps[index]["gate"] = task.result()

        check.add_done_callback(done)

    def _missed(self) -> bool:
        """The previous round changed nothing on screen, so a tap may have landed off target."""
        return bool(self.steps) and self.steps[-1].get("changed") is False

    def _repeating(self, act: dict, limit: int = 3) -> bool:
        """True when the actor picks the same action as its last limit-1 moves and the last one did nothing.

        Repeating a move that still changes the screen (e.g. scrolling down a long list) is progress, not a loop.
        """
        moves = [s for s in self.steps if "plan" not in s][-(limit - 1):]
        return (len(moves) == limit - 1 and all(_key(m) == _key(act) for m in moves)
                and moves[-1].get("changed") is not True)

    async def _replan(self, stuck: bool = False) -> None:
        prompt = f"Goal: {self.task}"
        if stuck:
            last = self.steps[-1] if self.steps else {}
            prompt += (f"\n\nThe previous plan got stuck (last event: {last.get('action')} {last.get('reason', '')}):"
                       f"\n{self.plan}\nLook at the current screen and plan from here, another way if needed.")
        reply, seconds, frame = await self._ask("planner", self.planner, PLANNER_PROMPT, prompt)
        self.plan, plan_zh = parse_plan(reply)
        self._record(frame, action="replan (stuck)" if stuck else "plan", plan=self.plan, plan_zh=plan_zh,
                     think_s=seconds)

    async def _next_actions(self) -> list[dict]:
        # actions and their effect only: echoing earlier thoughts makes the model repeat them instead of looking
        # ("gate" too: in observe mode the gate's opinion must not reach the actor and change what it does)
        hidden = ("t", "plan", "plan_zh", "think_s", "act_s", "shot", "thought", "thought_zh", "changed", "settle_s",
                  "stable", "ocr_s", "gate")
        effect = {True: " -> screen changed", False: " -> screen did NOT change"}
        history = "\n".join(json.dumps({k: v for k, v in s.items() if k not in hidden})[:300]
                            + effect.get(s.get("changed"), "")
                            for s in self.steps[-8:] if "plan" not in s) or "(none)"
        prompt = f"Goal: {self.task}\n\nPlan:\n{self.plan}\n\nRecent actions:\n{history}"
        ocr_s = None
        if self.ocr is not None and getattr(self.ocr, "mode", "off") == "actor":
            text = await self._screen_text()
            ocr_s = self.ocr.last_s
            if text:
                prompt += ("\n\nText read from the screen (OCR; x y = centre of each line in the same 0-1000 "
                           "coordinates; when you tap a word or a row, aim with these instead of estimating):\n" + text)
        prompt += "\n\nNext action?"
        for attempt in range(3):  # retry unparseable replies, a little less deterministically
            reply, seconds, frame = await self._ask("actor", self.actor, ACTOR_PROMPT, prompt, 0.4 * attempt)
            try:
                acts = parse_actions(reply)
            except ValueError as e:
                log.warning("unparseable actor reply (%s): %s", e, reply[:200])
                if isinstance(e, OffScreen):
                    prompt += (f"\n\nYour last reply was not used: {e}. The screenshot spans 0-1000 on both axes "
                               "(0,0 top-left, 1000,1000 bottom-right); give positions inside it.")
                continue
            if self._missed() and _key(acts[0]) == _key(self.steps[-1]):  # same move that just did nothing
                log.info("actor repeated an action that changed nothing; asking for another")
                prompt += (f"\n\nYou just proposed {json.dumps(_key(acts[0]))} again, but it changed nothing last "
                           "time. Choose a different action (another position, a shorter swipe, or another way).")
                continue
            acts[0]["think_s"] = seconds
            if ocr_s is not None:
                acts[0]["ocr_s"] = ocr_s
            return [{**a, "_shot": frame} for a in acts]  # every action is drawn on the same screenshot
        raise ValueError(f"actor gave no valid action: {reply[:200]}")

    async def _screen_text(self) -> str:
        """OCR lines for the actor's prompt, or "" when OCR fails: the actor still has the screenshot."""
        try:
            return self.ocr.lines(await self.ocr.read(self.screen.frame))
        except Exception as e:  # noqa: BLE001 - an OCR hiccup must never cost the step
            log.warning("OCR skipped: %s", str(e)[:120])
            return ""

    async def _do(self, a: dict) -> None:
        p = lambda k: a[k] / 1000  # noqa: E731
        match a["action"]:
            case "tap": await self.phone.tap(p("x"), p("y"), aim=a.get("precise", False) or self._missed())
            case "swipe": await self.phone.swipe(p("x1"), p("y1"), p("x2"), p("y2"))
            case "path": await self.phone.drag([(x / 1000, y / 1000) for x, y in a["points"]])
            case "circle": await self.phone.drag(circle(p("x"), p("y"), p("r"), self.screen.aspect))
            case "type": await self.phone.type(a["text"])
            case "key": await self.phone.key(a["combo"])
            case "home": await self.phone.go_home()
            case "back": await self.phone.go_back()
            case "wait": pass
            case other: raise ValueError(f"unknown action {other!r}")


def _pointer_path(act: dict, home: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    """Screen points (fractions) the pointer passes through for one action; home: the phone's way home."""
    f = lambda x, y: (x / 1000, y / 1000)  # noqa: E731
    match act["action"]:
        case "tap": return [f(act["x"], act["y"])]
        case "swipe": return [f(act["x1"], act["y1"]), f(act["x2"], act["y2"])]
        case "path": return [f(*p) for p in act["points"]]
        case "circle": return [f(act["x"] + act["r"], act["y"])]
        case "home": return list(HOME_SWIPE if home is None else home)
        case "back": return [(0.005, 0.45), (0.6, 0.45)]
        case _: return []


def circle(x: float, y: float, r: float, aspect: float, n: int = 24) -> list[tuple[float, float]]:
    """Points around a circle; r is a fraction of the width, aspect = width / height."""
    return [(x + r * math.cos(2 * math.pi * i / n), y + r * aspect * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)]
