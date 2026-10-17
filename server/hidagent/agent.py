"""See-think-act loop: a planner model writes the plan, an actor model picks each step."""

import asyncio
import json
import logging
import math
import os
import re
import time

from . import vision
from .debug import Trace
from .device import DeviceError
from .gate import Gate
from .gate import describe as gate_describe
from .llm import LLM, image_size, shrink
from .phone import DROP, HOME_SWIPE, PICK_UP, Phone
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
result: e.g. ticking several visible checkboxes or days, several drawing strokes, clearing a field then typing.
Give ONE DO line when the next action depends on what this one opens or changes.
A type action always ends the reply (anything after it is not run): read the typed text on the next screenshot
before pressing enter. done is always alone in its reply, after a screenshot that shows the result.
The screenshot is always the current screen: never assume it is outdated. If it does not show the goal reached,
the goal is not reached yet.
Never tap or long press the Dynamic Island or the status bar at the very top (the top 6 % of the screenshot):
the red recording indicator there asks to stop screen sharing, and without the screen you can no longer see
the phone.

Positions are integer pixels on the screenshot exactly as you see it (x from the left edge, y from the top
edge); the request gives its size. Read them off the image; never rescale them to another unit.
Aim at the visual center of the element you want, not its edge or its label.
Look before you act: describe what is really on the screen now, not what you expect.
Each recent action says whether the screen changed after it. If it did NOT change, the action
missed or had no effect: do not assume it worked; aim again more precisely or try another way.
Only type when a text field is visibly focused (a text cursor or highlighted field; the on-screen keyboard
may stay hidden because the board is a hardware keyboard).
Swipes have no momentum: content moves exactly as far as the finger and stops. Prefer short swipes
(10-25 % of the screen height) and check the result; repeat if needed rather than one big swipe.
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
In Home Screen edit mode (icons wiggle) never tap the small minus badge on an icon: it leads to Remove App and
Delete App. To leave edit mode, tap Done.
In an item's menu (after a long press) choose only the entry the goal needs; never Remove App, Delete App,
Remove Folder or Remove from Home Screen. Read the entry's text before tapping it: entries sit close together.
If a dialog asks to remove or delete something you did not mean to, tap Cancel.
To rename a Home Screen folder: long press the folder and choose Rename, or in edit mode tap the folder to open it
and tap its name at the top. When the name shows a text cursor, clear it (key cmd+a, then key backspace), type the
new name, then key enter. Check the new name in the open folder's title: the label under the icon may be shortened.
Actions:
  tap X Y [precise]         (precise: the pointer is located and corrected before tapping, about 1 s slower;
                             use it for small or tightly packed targets: list rows, checkmarks, toggles, small icons)
  swipe X1 Y1 X2 Y2         (finger moves from X1,Y1 to X2,Y2: "swipe down" means Y2 > Y1)
  longpress X Y [precise]   (touch and hold about 1 s: opens an item's menu; on the Home Screen, holding an app or
                             an empty spot until the icons wiggle enters edit mode)
  drag X1 Y1 X2 Y2          (pick up the item at X1,Y1, carry it to X2,Y2 and drop it there: moves a Home Screen
                             icon in edit mode (dropping it onto another app makes a folder) or a list row by its
                             handle; to scroll, use swipe, not drag)
  path X1 Y1 X2 Y2 X3 Y3 …  (one continuous stroke through 2-20 points, for drawing)
  circle X Y R              (draw a circle centered at X Y; R is the radius in pixels)
  type TEXT                 (English or Korean, not both in one action; tap the input field first. Look at the
                             field afterwards: if the text came out in the other alphabet (e.g. "ㅁㅠㅊ" for abc,
                             or "tjfwjd" for 설정), the input language is wrong: clear the field (key cmd+a, then
                             key backspace), switch with key ctrl+space, then type again)
  key COMBO                 (enter, backspace, esc, cmd+a, ctrl+space = switch the input language between
                             English and Korean, ...). To clear a field, use key cmd+a then key backspace in one
                             reply, not backspace again and again
  home | back | wait
  replan                    (the plan no longer fits the screen)
  done ANSWER               (the goal is reached; ANSWER is the result for the user. Before done, read the
                             screen's title or name and check it is exactly the item the goal names, not a
                             neighbouring row; if it is another item, go back and open the right one instead)
  confirm REASON            (the next step would pay, send, delete or change account settings: stop here
                             and say what is ready for the user to confirm; never do that step yourself)
  fail REASON

Example:
THINK: My last tap opened the home screen as intended. The App Store icon is visible, so I tap it.
DO: tap 152 362"""

META = ("t", "thought", "thought_zh", "think_s", "act_s", "shot", "_shot", "changed", "settle_s", "stable",
        "ocr_s", "gate")  # not the action

# action name -> argument names; numeric arguments are stored as 0-1000 of each screen axis (the actor gives pixels)
ACTIONS = {"tap": ("x", "y"), "swipe": ("x1", "y1", "x2", "y2"), "circle": ("x", "y", "r"), "path": ("points",),
           "longpress": ("x", "y"), "drag": ("x1", "y1", "x2", "y2"),
           "type": ("text",), "key": ("combo",), "done": ("answer",), "fail": ("reason",), "confirm": ("reason",),
           "home": (), "back": (), "wait": (), "replan": ()}
TERMINAL = ("done", "fail", "confirm")  # the run ends here; confirm hands the last step to the user
TEXT_ARGS = ("text", "combo", "answer", "reason")


class OffScreen(ValueError):
    """A position outside the screenshot: the actor misjudged the screen; ask it again instead of acting off-screen."""


def parse_command(line: str, size: tuple[int, int] | None = None) -> dict:
    """One DO line, e.g. "tap 152 362" -> {"action": "tap", "x": 258, "y": 283} on a 590x1280 screenshot.

    The actor gives pixel positions on the screenshot it saw (``size`` = its width, height): models fall back to
    pixels however often they are asked for another unit (#17, the 2026-10-17 re-test of #38). They are stored as
    0-1000 of each axis, the unit everything after the actor uses. Without a size the screenshot counts as
    1000x1000, so positions are taken as they are.
    """
    name, _, rest = line.strip().partition(" ")
    name = name.lower()
    if name not in ACTIONS:
        raise ValueError(f"unknown action: {line[:100]}")
    keys = ACTIONS[name]
    if keys and keys[0] in TEXT_ARGS:
        return {"action": name, keys[0]: rest.strip().strip('"')}
    w, h = size or (1000, 1000)
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", rest)]
    axes = ["y" if i % 2 else "x" for i in range(len(numbers))] if keys == ("points",) else \
        ["y" if k.startswith("y") else "x" for k in keys[:len(numbers)]]  # circle's r is measured along x
    span = {"x": w, "y": h}
    if any(not 0 <= n <= span[a] for n, a in zip(numbers, axes, strict=False)):  # never act off the screen
        raise OffScreen(f"{name} positions must be inside the {w}x{h} screenshot: {line[:100]}")
    numbers = [round(n * 1000 / span[a]) for n, a in zip(numbers, axes, strict=False)] + \
        [int(n) for n in numbers[len(axes):]]
    if keys == ("points",):
        if len(numbers) < 4 or len(numbers) % 2:
            raise ValueError(f"path needs 2+ x y pairs: {line[:100]}")
        return {"action": name, "points": [numbers[i:i + 2] for i in range(0, len(numbers), 2)][:20]}
    if len(numbers) != len(keys):
        raise ValueError(f"{name} needs {len(keys)} numbers: {line[:100]}")
    action = {"action": name, **dict(zip(keys, numbers, strict=True))}
    if name in ("tap", "longpress") and "precise" in rest.lower():
        action["precise"] = True
    return action


def parse_actions(text: str, size: tuple[int, int] | None = None) -> list[dict]:
    """Parse a THINK / DO… reply (an optional ZH line is kept); the thought goes on the first action."""
    thought = re.search(r"^\s*THINK\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    zh = re.search(r"^\s*ZH\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)
    acts = [parse_command(line, size)
            for line in re.findall(r"^\s*DO\s*[:：]\s*(.*?)\s*$", text, re.M | re.I)][:6]
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


def _in_pixels(step: dict, size: tuple[int, int] | None) -> dict:
    """A recorded step with its 0-1000 positions back in the actor's unit, pixels of a w x h screenshot."""
    w, h = size or (1000, 1000)
    out = dict(step)
    for k in ("x", "x1", "x2", "r", "y", "y1", "y2"):
        if isinstance(out.get(k), (int, float)):
            out[k] = round(out[k] * (h if k.startswith("y") else w) / 1000)
    if "points" in out:
        out["points"] = [[round(x * w / 1000), round(y * h / 1000)] for x, y in out["points"]]
    return out


def _as_line(act: dict, size: tuple[int, int] | None) -> str:
    """An action as the actor would write it, e.g. "longpress 226 169"."""
    px = _in_pixels(_key(act), size)
    args = [str(px[k]) for k in ACTIONS.get(act["action"], ()) if isinstance(px.get(k), (int, float))]
    return " ".join([act["action"], *args])


class BlindError(RuntimeError):
    """The screen feed is gone; acting now would be guessing."""


SETTLE_ENV = {"quiet": "SETTLE_QUIET", "least": "SETTLE_LEAST", "most": "SETTLE_MOST", "first": "SETTLE_FIRST"}


def settle_options(env=os.environ) -> dict[str, float]:
    """The wait after each step, from SETTLE_* (seconds); unset ones keep the screen's own defaults.

    For measuring on a device (#17 speed): shorter waits save time per step but risk deciding on a screen that
    is still moving, which the step's ``stable`` field shows.
    """
    return {key: float(env[name]) for key, name in SETTLE_ENV.items() if env.get(name, "").strip()}


class Agent:
    def __init__(self, phone: Phone, screen: Screen, actor: LLM, planner: LLM, trace: Trace,
                 max_steps: int = 40, max_failures: int = 3, gate: Gate | None = None, ocr=None,
                 settle: dict[str, float] | None = None):
        self.phone, self.screen, self.actor, self.planner = phone, screen, actor, planner
        self.trace = trace
        self.gate = gate  # optional second opinion on each action (observe only: never changes what is done)
        self.ocr = ocr  # optional: with OCR_MODE=actor the actor also gets the screen's text with coordinates
        self._run_id = 0  # bumped per run, so a late gate answer never lands in a newer run's steps
        # (not `_run`: that name is the coroutine method below; a counter there made start() raise TypeError)
        self.max_steps, self.max_failures = max_steps, max_failures
        self.settle = settle or {}  # keyword overrides for screen.settled() after a step (SETTLE_* variables)
        self.task, self.plan, self.steps = "", "", []
        self.shots: dict[int, bytes] = {}  # step index -> screenshot the model decided on
        self._pointer = (0.0, 0.0)  # last pointer position (screen fractions)
        self._acted_at: float | None = None  # when the last action began: the settle waits for a frame after it
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
            if name in TERMINAL and i > 0:  # done must rest on a screenshot taken after the last action (#36)
                log.info("agent: %s after actions in the same reply not taken; looking again", name)
                break
            if name == "replan" or stuck:
                self._record(act.pop("_shot"), **act)
                await self._replan(stuck=stuck)
                return False
            check = None
            if name not in TERMINAL:
                check = self._gate_check(thought, act)  # runs alongside the action, never waited for
                started = self._acted_at = time.time()
                await self._do(act)
                act["act_s"] = round(time.time() - started, 2)
                spots += _pointer_path(act, getattr(getattr(self.phone, "home", None), "path", None))
                self._pointer = spots[-1]
            self._record(act.pop("_shot"), **act)
            if check:
                self._attach(check, len(self.steps) - 1)
            if name in TERMINAL:
                return True
            if name == "type":  # the text may come out in the other alphabet: look before enter (#36, "skfTl")
                break
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
        result = await self.screen.settled(since=self._acted_at, **self.settle)
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

    def _tried_twice(self, act: dict, window: int = 6) -> bool:
        """True when this tap or long press, at the same spot, is already twice among the last window moves.

        Catches "miss -> close the wrong menu -> the same miss" loops, which the last-action checks never see
        because the screen did change each time. A goal that really needs the same tap again can batch it.
        """
        if act["action"] not in ("tap", "longpress"):
            return False
        spot = (act["action"], act.get("x"), act.get("y"))
        moves = [s for s in self.steps if "plan" not in s][-window:]
        return sum((m.get("action"), m.get("x"), m.get("y")) == spot for m in moves) >= 2

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

    def _shown_size(self) -> tuple[int, int] | None:
        """Pixel size of the screenshot as the actor will see it (after any IMAGE_MAX_SIDE shrinking)."""
        frame = self.screen.frame
        return image_size(shrink(frame, getattr(self.actor, "image_max", 0)) if frame else None)

    async def _next_actions(self) -> list[dict]:
        # actions and their effect only: echoing earlier thoughts makes the model repeat them instead of looking
        # ("gate" too: in observe mode the gate's opinion must not reach the actor and change what it does)
        hidden = ("t", "plan", "plan_zh", "think_s", "act_s", "shot", "thought", "thought_zh", "changed", "settle_s",
                  "stable", "ocr_s", "gate")
        effect = {True: " -> screen changed", False: " -> screen did NOT change"}
        size = self._shown_size()  # the actor's unit is pixels of this; None (unreadable frame): taken as 1000x1000
        history = "\n".join(json.dumps(_in_pixels({k: v for k, v in s.items() if k not in hidden}, size))[:300]
                            + effect.get(s.get("changed"), "")
                            for s in self.steps[-8:] if "plan" not in s) or "(none)"
        prompt = f"Goal: {self.task}\n\nPlan:\n{self.plan}\n\nRecent actions:\n{history}"
        ocr_s = None
        if self.ocr is not None and getattr(self.ocr, "mode", "off") == "actor":
            text = await self._screen_text(size)
            ocr_s = self.ocr.last_s
            if text:
                prompt += ("\n\nText read from the screen (OCR; x y = centre of each line in pixels of the "
                           "screenshot; when you tap a word or a row, aim with these instead of estimating):\n" + text)
        if size:
            w, h = size
            prompt += (f"\n\nThe screenshot is {w}x{h} pixels. Give positions as pixels on it "
                       f"(0,0 top-left, {w},{h} bottom-right).")
        prompt += "\n\nNext action?"
        for attempt in range(3):  # retry unparseable replies, a little less deterministically
            reply, seconds, frame = await self._ask("actor", self.actor, ACTOR_PROMPT, prompt, 0.4 * attempt)
            try:
                acts = parse_actions(reply, size)
            except ValueError as e:
                log.warning("unparseable actor reply (%s): %s", e, reply[:200])
                if isinstance(e, OffScreen):
                    prompt += f"\n\nYour last reply was not used: {e}. Give positions inside the screenshot."
                continue
            if self._missed() and _key(acts[0]) == _key(self.steps[-1]):  # same move that just did nothing
                log.info("actor repeated an action that changed nothing; asking for another")
                prompt += (f"\n\nYou just proposed {_as_line(acts[0], size)} again, but it changed nothing last "
                           "time. Choose a different action (another position, a shorter swipe, or another way).")
                continue
            if self._tried_twice(acts[0]):  # e.g. a long press that keeps opening the wrong item's menu (#38 re-test)
                log.info("actor proposed the same tap or long press a third time; asking for another")
                prompt += (f"\n\nYou already did {_as_line(acts[0], size)} twice in your recent actions and the goal "
                           "is still not reached. Measure the target's position on this screenshot again (in pixels) "
                           "and choose another position or another way.")
                continue
            acts[0]["think_s"] = seconds
            if ocr_s is not None:
                acts[0]["ocr_s"] = ocr_s
            return [{**a, "_shot": frame} for a in acts]  # every action is drawn on the same screenshot
        raise ValueError(f"actor gave no valid action: {reply[:200]}")

    async def _screen_text(self, size: tuple[int, int] | None = None) -> str:
        """OCR lines for the actor's prompt, or "" when OCR fails: the actor still has the screenshot."""
        try:
            return self.ocr.lines(await self.ocr.read(self.screen.frame), size or 1000)
        except Exception as e:  # noqa: BLE001 - an OCR hiccup must never cost the step
            log.warning("OCR skipped: %s", str(e)[:120])
            return ""

    async def _do(self, a: dict) -> None:
        p = lambda k: a[k] / 1000  # noqa: E731
        match a["action"]:
            case "tap": await self.phone.tap(p("x"), p("y"), aim=a.get("precise", False) or self._missed())
            case "swipe": await self.phone.swipe(p("x1"), p("y1"), p("x2"), p("y2"))
            case "longpress": await self.phone.long_press(p("x"), p("y"), aim=a.get("precise", False) or self._missed())
            case "drag":  # lift the item, carry it, rest on the target; aim first: picking the wrong icon costs more
                await self.phone.drag([(p("x1"), p("y1")), (p("x2"), p("y2"))], pick=PICK_UP, rest=DROP, aim=True)
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
        case "tap" | "longpress": return [f(act["x"], act["y"])]
        case "swipe" | "drag": return [f(act["x1"], act["y1"]), f(act["x2"], act["y2"])]
        case "path": return [f(*p) for p in act["points"]]
        case "circle": return [f(act["x"] + act["r"], act["y"])]
        case "home": return list(HOME_SWIPE if home is None else home)
        case "back": return [(0.005, 0.45), (0.6, 0.45)]
        case _: return []


def circle(x: float, y: float, r: float, aspect: float, n: int = 24) -> list[tuple[float, float]]:
    """Points around a circle; r is a fraction of the width, aspect = width / height."""
    return [(x + r * math.cos(2 * math.pi * i / n), y + r * aspect * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)]
