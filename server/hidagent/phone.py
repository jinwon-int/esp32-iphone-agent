"""Touch-like gestures on the phone, built from relative mouse moves (or absolute pointer positions).

The pointer is relative, so the server tracks where it is (in mouse units) and moves
from there; every few gestures it slams it into the top-left corner (`home`) again to
cancel drift. Coordinates are fractions of the screen (0..1, origin top-left), so they
work for any frame size.

With a board built as an absolute pointer (``ABSOLUTE_POINTER=1``, ``POINTER_MODE=absolute``, #14) the server
sends screen positions instead (``at x y``, 0..32767): no homing, no calibration, no pointer search.
"""

import asyncio
import json
import logging
import math
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from . import vision
from .device import Device, DeviceError
from .hangul import has_hangul, script_runs, to_dubeolsik

log = logging.getLogger(__name__)

CALIBRATION_MOVE = 200  # mouse units moved diagonally for a manual (one point) calibration; stays on screen
CALIBRATION_STOPS = (150, 350)  # automatic calibration finds the pointer at these distances from the corner
REHOME_EVERY = 10       # gestures between re-homing
EDGE = 0.06             # screen fraction; after a gesture this close to an edge the next one re-homes first: the
                        # pointer stops at the edge, so the tracked position may no longer be where it is (#36)
HOLD = 0.25             # seconds to rest before lifting, so iOS adds no momentum (the swipe stops where it ends)
TAP_GAP = 0.15          # seconds to stay still after a click, so iOS sees a finished tap, not the start of a drag
LONG_PRESS = 1.0        # seconds the button stays down for a long press (context menus, Home Screen edit mode)
PICK_UP = 0.5           # seconds a drag holds still after pressing, so iOS lifts the item (an icon, a list row)
LIFT = 0.6              # seconds the agent's drag then covers the first LIFT_DIST slowly (see Phone.drag ``lift``)
DROP = 1.2              # seconds a lifted item rests on its target before it is let go (icon on icon = a folder;
                        # icon on a folder = into it). Device 2026-10-17 (#35): with the absolute pointer, pick 0.5
                        # + lift 0.6 + rest 1.2 dropped into folders 9/9; the earlier 0.8 / no lift / 1.0 missed
                        # existing folders (the item was set down beside them: app 2026-10-21, 0/7)
NUDGE = 12              # mouse units the pointer is wiggled by to find it on screen
AIM_TOLERANCE = 0.008   # screen fraction; a bigger miss is corrected before pressing
MAX_MISS = 0.06         # bigger misses are still corrected, but not learned from (they may be misreadings)
POINTER_MODES = ("relative", "absolute")
ABS_MAX = 32767         # the absolute pointer's logical range on each axis (board: ABS_MAX)
ABS_STEP = 0.03         # screen fraction per absolute report while dragging, so iOS sees a moving finger, not a jump
ABS_STEP_S = 0.008      # seconds between those reports (the relative mouse's pace, STEP_MS on the board)
ABS_NUDGE = 0.025       # screen fraction the absolute pointer is nudged by to find it on screen
ABS_TAP_SETTLE = 0.03   # seconds the absolute pointer rests on the target before pressing, so iOS sees it arrive
ABS_WAKE_AFTER = 2.0    # seconds without a pointer report after which iOS may have hidden the pointer
ABS_WAKE_S = 0.3        # after such a pause: report the target once, wait this long, then act (#35: the first
                        # report only woke the pointer, so a tap clicked at the old spot and a swipe became a hold)
PAGE_SLIDE_SETTLE = 0.25  # a page turn (flick): the pointer rests on the start before the press (yoyicue/glassbox)
PAGE_SLIDE_PICK = 0.35    # ... the button rests down before the finger moves: without it the page stood half turned
PAGE_SLIDE_STEPS = 8      # ... reports along the way
PAGE_SLIDE_STEP_S = 0.03  # ... between them
PAGE_SLIDE_REST = 0.15    # ... the finger rests at the end before the release
LIFT_DIST = 0.03        # screen fraction of a drag's first stretch that ``lift`` slows down, so iOS lifts the item
LIFT_STEPS = 6          # reports (absolute) or moves (relative) over that stretch
EDGE_X = (0.015, 0.985) # where carry() holds an item so iOS turns the page (left, right)
CARRY_Y = 0.80          # ... at this height: below the last icon row's labels, above the page dots, so the held
                        # item never rests beside an icon (at 0.62 it sat next to column 4 and made a folder, #35)
FLIP_CELLS = 0.5        # vision.cells_changed against the page before above this: the page is turning (a turn
                        # 0.54-0.88; a folder forming or an icon lifting/dropping 0.04-0.33; device frames 2026-10-17)
FLIP_MOTION = 10.0      # vision.page_change between two newly pushed frames above this: the content is sliding
FLIP_MOTION_FRAMES = 2  # ... on this many new frames in a row: a turn even between look-alike pages. Pages of
                        # folders in the same slots kept cells_changed at 0.42 through a whole turn, so only the
                        # next turn was seen and two pages turned (#54); turns slid 12.7/15.8 and 26.9/28.0, the
                        # last page's bounce at the edge once 14.8 (device frames 2026-10-18)
FLIP_AWAY = 12.0        # vision.page_change against the last still frame before the turn above this on
                        # FLIP_MOTION_FRAMES new frames in a row: the page turned, even when it slid above
                        # FLIP_MOTION on one frame only (look-alike pages read 13.7-20 once turned, a held
                        # item at the edge stays under 6 against a still frame; 4 device runs, #54)
FLIP_STILL = 6.0        # vision.page_change between two polls below this: the turn's animation is over
FLIP_SETTLE_MIN = 0.25  # seconds after the turn began before it may count as over (the animation)
FLIP_SETTLE_MAX = 1.0   # seconds to wait for that before leaving the edge anyway
LIFT_GONE = 20.0        # vision.region_change at the start slot above this once at the edge: the item was lifted
FLIP_POLL = 0.05        # seconds between screen checks while holding at the edge
FLIP_SETTLE = 0.6       # seconds for the turn's animation before the item is carried on, off the edge
FLIP_TIMEOUT = 4.0      # seconds at the edge without a page turn: give up and put the item back
_clock = time.monotonic  # what carry() times a page turn by (tests replace it with the polls' clock)
HOME_SWIPE = ((0.5, 0.995), (0.5, 0.6))  # bottom-edge flick; ending higher would open the app switcher
SPOTLIGHT_SWIPE = ((0.5, 0.45), (0.5, 0.65))  # a short pull down from the middle of the Home Screen opens Spotlight
PARK = (0.015, 0.5)     # where the absolute pointer waits after a gesture: off the rows' text and controls, so the next
                        # screenshot shows the result (it sat on the switch it had just flipped, 2026-10-21, #74 M4)
MENU_OPEN_S = 0.6       # most the AssistiveTouch menu takes to open when the screen cannot be watched


def _fraction_point(name: str, value: str) -> tuple[float, float]:
    try:
        x, y = (float(v) for v in value.split(","))
    except ValueError:
        raise ValueError(f"{name} must be 'x,y' as screen fractions (e.g. 0.9,0.27), not {value!r}") from None
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError(f"{name} must be screen fractions between 0 and 1, not {value!r}")
    return x, y


@dataclass(frozen=True)
class HomeVia:
    """How the agent gets to the Home Screen.

    By default it flicks up from the bottom edge. Some iOS versions ignore that flick from a pointer (iOS 27 did,
    and the keyboard's cmd+h too; see #17), so there are two other ways:
    ``key``: the board's consumer Home key (usage 0x0040, Apple's "Home Button"; needs the firmware's
    ``consumer`` command). AssistiveTouch: ``button`` alone taps the button ("Single-Tap" set to Home); with
    ``menu_home`` the button opens the menu and that point is its Home item. Both are screen fractions, as seen in
    the live view.
    """
    button: tuple[float, float] | None = None
    menu_home: tuple[float, float] | None = None
    key: bool = False

    @classmethod
    def from_env(cls, env=os.environ) -> "HomeVia":
        """``HOME_METHOD`` = ``swipe`` (default), ``key`` (the board's consumer Home key) or ``assistivetouch``
        (needs ``ASSISTIVETOUCH_BUTTON``, and ``ASSISTIVETOUCH_MENU_HOME`` unless Single-Tap is set to Home)."""
        method = env.get("HOME_METHOD", "").strip().lower() or "swipe"
        if method == "swipe":
            return cls()
        if method == "key":
            return cls(key=True)
        if method != "assistivetouch":
            raise ValueError(f"HOME_METHOD must be 'swipe', 'key' or 'assistivetouch', not {method!r}")
        button = env.get("ASSISTIVETOUCH_BUTTON", "").strip()
        if not button:
            raise ValueError("HOME_METHOD=assistivetouch needs ASSISTIVETOUCH_BUTTON=x,y (where the button sits)")
        menu = env.get("ASSISTIVETOUCH_MENU_HOME", "").strip()
        return cls(_fraction_point("ASSISTIVETOUCH_BUTTON", button),
                   _fraction_point("ASSISTIVETOUCH_MENU_HOME", menu) if menu else None)

    @property
    def path(self) -> list[tuple[float, float]]:
        """Screen points the pointer visits on the way home (for drawing the step)."""
        if self.key:
            return []
        if self.button is None:
            return list(HOME_SWIPE)
        return [self.button] + ([self.menu_home] if self.menu_home else [])


class GuardStop(RuntimeError):
    """A drag's guard found a menu under the press (phone.drag guard): the item was never carried."""


class Phone:
    def __init__(self, device: Device, calibration_file: Path, screen=None, home: HomeVia | None = None,
                 pointer: str = "relative"):
        if pointer not in POINTER_MODES:
            raise ValueError(f"POINTER_MODE must be 'relative' or 'absolute', not {pointer!r}")
        self.device, self._file, self.screen = device, calibration_file, screen
        self.home = home or HomeVia()
        self.absolute = pointer == "absolute"
        self._reported = time.monotonic()  # last absolute pointer report (ABS_WAKE_AFTER)
        # mouse units needed to cross the full screen width / height
        saved = json.loads(calibration_file.read_text()) if calibration_file.exists() else None
        self.span = {"w": saved["w"], "h": saved["h"]} if saved else None
        self._pos = None  # pointer position in mouse units; None = unknown
        self._moves = 0
        self._link = 0  # device.links when the pointer was last homed
        # where the pointer really landed (seen on screen) for the screen fraction we aimed at, per axis;
        # a straight-line fit through them corrects every later move, so taps get better as the agent works
        self._seen = {axis: deque(map(tuple, (saved or {}).get("seen", {}).get(axis, [])), maxlen=12)
                      for axis in ("x", "y")}

    @property
    def calibrated(self) -> bool:
        return self.absolute or self.span is not None

    async def start_calibration(self) -> bool:
        """Find the pointer on screen at two known distances from the corner and fit the mapping from both.

        Two points give the scale and the offset, which one point cannot (iOS pointer motion is not exactly
        proportional). Returns True when it calibrated itself; otherwise the user clicks the pointer
        (finish_calibration). An absolute pointer needs no calibration.
        """
        if self.absolute:
            return True
        self._pos = None
        await self.device.send("home")
        seen = []
        for units in CALIBRATION_STOPS:
            step = units - (seen[-1][0] if seen else 0)
            await self.device.send(f"move {step} {step}")
            found = await self._find_pointer() if self.screen is not None and self.screen.live else None
            if not found:
                break
            seen.append((units, *found))
        if len(seen) < 2:
            log.info("calibration: pointer not found on screen, waiting for a click")
            if not seen:
                await self.device.send("home")
                await self.device.send(f"move {CALIBRATION_MOVE} {CALIBRATION_MOVE}")
            return False
        (u1, x1, y1), (u2, x2, y2) = seen
        self.span = {"w": round((u2 - u1) / (x2 - x1)), "h": round((u2 - u1) / (y2 - y1))}
        for samples in self._seen.values():
            samples.clear()
        for units, x, y in seen:  # where each stop landed, so moves are corrected for the offset too
            self._seen["x"].append((units / self.span["w"], x))
            self._seen["y"].append((units / self.span["h"], y))
        self._save()
        log.info("calibration: pointer at %s, span %s", [(round(x, 3), round(y, 3)) for _, x, y in seen], self.span)
        return True

    async def _find_pointer(self) -> tuple[float, float] | None:
        """Wiggle the pointer and find it anywhere on screen (the span may still be unknown)."""
        await self.screen.settled(quiet=0.2, least=0.3, most=1.5)
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {NUDGE} 0")
        after = await self.screen.fresh(sent, 1.0)
        if after:
            await self.screen.settled(quiet=0.15, least=0.1, most=0.6)
            after = self.screen.frame
        await self.device.send(f"move -{NUDGE} 0")
        found = before and after and vision.pointer(before, after, (0.5, 0.5), 0.025, reach=(0.5, 0.5))
        return found if found and 0.03 < found[0] < 0.97 and 0.03 < found[1] < 0.97 else None

    def finish_calibration(self, x: float, y: float) -> None:
        """x, y: where the pointer ended up after start_calibration() (one point, picked by the user)."""
        self.span = {"w": round(CALIBRATION_MOVE / x), "h": round(CALIBRATION_MOVE / y)}
        for samples in self._seen.values():
            samples.clear()
        self._save()

    def _save(self) -> None:
        """Calibration plus the observed landings, so the correction survives a restart."""
        self._file.write_text(json.dumps({**self.span, "seen": {k: list(v) for k, v in self._seen.items()}}))

    async def point(self, x: float, y: float, aim: bool = False) -> None:
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated")
        if self.absolute:  # lands where it is sent: nothing to aim
            await self._at(x, y)
            return
        await self._move_to(self._dx(x), self._dy(y))
        if aim and self.screen is not None:
            await self._aim(x, y)

    async def locate(self, x: float, y: float) -> tuple[float, float] | None:
        """Where the pointer really is, after pointing at x, y: nudge it sideways and compare two frames.

        None when it cannot be seen (no live screen, no new frame, or no pointer-sized change). Moves nothing for good:
        the pointer is put back where it was.
        """
        if self.screen is None or not self.screen.live:
            return None
        if self.absolute:
            nudge, shift = 0, ABS_NUDGE if x < 0.9 else -ABS_NUDGE  # stay clear of the screen edge
        else:
            nudge = NUDGE if x < 0.9 else -NUDGE
            shift = nudge / self.span["w"]
        await self.screen.settled(quiet=0.15, least=0.1, most=0.6)  # pointer at rest
        before, sent = self.screen.frame, time.time()
        await (self._at(x + shift, y) if self.absolute else self.device.send(f"move {nudge} 0"))
        after = await self.screen.fresh(sent, 0.6)
        if after:
            await self.screen.settled(quiet=0.1, least=0.05, most=0.4)
            after = self.screen.frame
        await (self._at(x, y) if self.absolute else self.device.send(f"move {-nudge} 0"))
        found = before and after and (vision.pointer(before, after, (x, y), shift)  # near the target first,
                                      or vision.pointer(before, after, (0.5, 0.5), shift, reach=(0.5, 0.5)))  # anywhere
        if not found:
            log.debug("locate (%.3f, %.3f): pointer not found (%s)", x, y,
                      "no new frame after the nudge" if not after else "no pointer-sized change near the target")
        return found or None

    async def _aim(self, x: float, y: float) -> None:
        """Closed loop: find the real pointer on screen, fix a miss, and learn the error for later moves."""
        found = await self.locate(x, y)
        if not found:
            return
        fx, fy = found
        log.debug("aim (%.3f, %.3f): pointer at (%.3f, %.3f)", x, y, fx, fy)
        if max(abs(fx - x), abs(fy - y)) <= MAX_MISS:  # learn from normal drift only, never from a stray reading
            self._seen["x"].append((self._fix(x, "x"), fx))
            self._seen["y"].append((self._fix(y, "y"), fy))
            self._save()
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            return
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])  # fix this miss right away
        await self.device.send(f"move {dx} {dy}")
        self._pos = (self._pos[0] + dx, self._pos[1] + dy)

    def _fix(self, t: float, axis: str) -> float:
        """The fraction to aim at so the pointer lands on t, from the line through the observed landings."""
        pts = self._seen[axis]
        if len(pts) < 2:
            return t
        n = len(pts)
        mc, mf = sum(c for c, _ in pts) / n, sum(f for _, f in pts) / n
        var = sum((c - mc) ** 2 for c, _ in pts)
        a = sum((c - mc) * (f - mf) for c, f in pts) / var if var > 1e-4 else 1.0  # landed = a * aimed + b
        a = a if 0.85 < a < 1.15 else 1.0
        b = mf - a * mc
        return (t - b) / a if abs(b) < 0.1 else t

    async def _move_to(self, tx: int, ty: int, rehome: bool = True) -> None:
        if self._link != self.device.links:  # new link: a dropped gesture may have left the button down
            await self.device.send("release")
            self._pos, self._link = None, self.device.links
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            await self.device.send("home")
            self._pos, self._moves = (0, 0), 0
        x, y = self._pos
        self._pos = None  # unknown until the move succeeds
        await self.device.send(f"move {tx - x} {ty - y}")
        self._pos, self._moves = (tx, ty), self._moves + 1
        fx, fy = tx / self.span["w"], ty / self.span["h"]
        if min(fx, fy, 1 - fx, 1 - fy) < EDGE:
            self._moves = REHOME_EVERY  # re-anchor before the next gesture (mid-drag moves never home)

    async def _at(self, x: float, y: float) -> None:
        ax, ay = (round(min(max(t, 0), 1) * ABS_MAX) for t in (x, y))
        if time.monotonic() - self._reported > ABS_WAKE_AFTER:  # wake the pointer first, then report for real
            await self.device.send(f"at {ax} {ay}")
            await asyncio.sleep(ABS_WAKE_S)
        await self.device.send(f"at {ax} {ay}")
        self._reported = time.monotonic()

    async def reset(self) -> None:
        """Let go of the button and re-home the pointer, whatever state a gesture left."""
        self._pos = None
        await self.device.send("release")
        await self.device.send("home")
        self._pos, self._moves = (0, 0), 0

    async def tap(self, x: float, y: float, aim: bool = False) -> None:
        """aim=True finds the pointer on screen first (slower); worth it when a tap just missed."""
        await self.point(x, y, aim)
        if self.absolute:
            await asyncio.sleep(ABS_TAP_SETTLE)
        await self.device.send("click")
        await asyncio.sleep(TAP_GAP)
        await self._park()

    async def _park(self) -> None:
        """Move the absolute pointer to PARK after a gesture, so the screenshot shows what the gesture did (a relative
        pointer stays: moving it would cost accuracy)."""
        if self.absolute:
            await self._at(*PARK)

    async def long_press(self, x: float, y: float, seconds: float = LONG_PRESS, aim: bool = False) -> None:
        """Press and keep the button down, then let go where it was pressed: a touch-and-hold.

        Opens context menus; on the Home Screen, holding an app (or an empty spot) long enough enters edit mode.
        The button is always released, even when the hold is interrupted.
        """
        await self.point(x, y, aim)
        if self.absolute:
            await asyncio.sleep(ABS_TAP_SETTLE)
        await self.device.send("press")
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.device.send("release")
        await asyncio.sleep(TAP_GAP)
        await self._park()

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True,
                    step: float | None = None) -> None:
        await self.drag([(x1, y1), (x2, y2)], hold, step=step)

    async def flick(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """A Home Screen page turn: the press rests before the finger moves and before it lifts.

        Device 2026-10-23 (#74): every turn that moved right after the press — report by report (0.6.8), played by
        the board in 0.1-0.3 s ("stroke", 0.6.9-0.6.11), released twice — left the page standing two thirds turned
        until the board was unplugged (iOS, AssistiveTouch pointer). The recipe of yoyicue/glassbox (PicoKVM, MIT)
        turned it both ways (runs 9d10720c0f04, 91268ce870d6): the pointer reported on the start twice and resting
        PAGE_SLIDE_SETTLE, press, PAGE_SLIDE_PICK still, PAGE_SLIDE_STEPS reports PAGE_SLIDE_STEP_S apart, then
        PAGE_SLIDE_REST still before the release. A relative pointer drags with the same rests.

        The pointer stays where the finger lifted — no park (iphone-agent-cast#59, handoff §2-2): moving it right
        after the release left the page standing again (run 6311bfbe0388 parked at once: stuck 4/5 turned;
        bd118718a80a parked 0.8 s later: stuck half turned). Not parking is needed, not enough: the same recipe
        without a park also stood half turned later that day (b333e9f0a2c5, 6bab9e2d7aeb).
        """
        if not self.absolute:
            await self.drag([(x1, y1), (x2, y2)], pick=PAGE_SLIDE_PICK, rest=PAGE_SLIDE_REST)
            return
        await self.point(x1, y1)
        await self._at(x1, y1)
        await asyncio.sleep(PAGE_SLIDE_SETTLE)
        await self.device.send("press")
        try:
            await asyncio.sleep(PAGE_SLIDE_PICK)
            for i in range(1, PAGE_SLIDE_STEPS + 1):
                await self._at(x1 + (x2 - x1) * i / PAGE_SLIDE_STEPS, y1 + (y2 - y1) * i / PAGE_SLIDE_STEPS)
                await asyncio.sleep(PAGE_SLIDE_STEP_S)
            await asyncio.sleep(PAGE_SLIDE_REST)
        finally:
            await self.device.send("release")

    async def drag(self, points: list[tuple[float, float]], hold: bool = True, pick: float = 0.0,
                   rest: float | None = None, aim: bool = False, stops: list[float] | None = None,
                   lift: float = 0.0, guard: Callable[[], Awaitable[bool]] | None = None,
                   step: float | None = None) -> None:
        """Press at the first point, move through the rest, release: one continuous stroke.

        hold=True rests at the end first, so lists and pickers stop exactly there instead of flinging on.
        System gestures (home, back) need the flick, so they pass hold=False.
        To move an item rather than the content under it (a Home Screen icon, a list row by its handle), pass
        ``pick`` (seconds to hold still after pressing, so iOS lifts the item) and ``rest`` (seconds to stay on the
        target before letting go; an icon resting on another makes a folder). aim=True locates the pointer on
        the first point before pressing, so the right item is picked up.
        ``stops``: seconds to rest at each point between the first and the last (one per via point), e.g. at the
        screen edge until iOS turns the page while an icon is held. ``lift``: seconds over which the first
        LIFT_DIST of the way is covered slowly, right after ``pick`` (iOS 27 kept the item's menu open when the
        pointer raced off at once, #35). ``guard``: awaited after ``pick``; True means a menu opened under the press
        (#35, #74 M8): the button is released where it is, nothing is carried, and GuardStop is raised.
        ``step``: screen fraction per absolute report while gliding (default ABS_STEP). Every report is one board
        round trip (about 170 ms on the device, 2026-10-22), so a long stroke with small steps is a slow drag; a page
        turn passes a bigger step to stay a stroke.
        """
        if stops is not None and len(stops) != len(points) - 2:
            raise ValueError(f"stops needs one rest per via point: {len(points) - 2}, got {len(stops)}")
        await self.point(*points[0], aim=aim)
        await self.device.send("press")
        try:
            if pick:
                await asyncio.sleep(pick)
            if guard is not None and await guard():
                raise GuardStop("a menu opened under the press: released in place, nothing moved")
            last = points[0]
            if lift and len(points) > 1:
                last = await self._lift(points[0], points[1], lift)
            for n_point, (x, y) in enumerate(points[1:]):
                last = await self._glide(last, (x, y), step)
                if stops and n_point < len(stops) and stops[n_point]:
                    await asyncio.sleep(stops[n_point])
            pause = rest if rest is not None else (HOLD if hold else 0)
            if pause:
                await asyncio.sleep(pause)
        finally:
            await self.device.send("release")
        await self._park()

    async def _lift(self, start: tuple[float, float], toward: tuple[float, float],
                    seconds: float) -> tuple[float, float]:
        """Cover the first LIFT_DIST of the way slowly (button already down); returns where the pointer is."""
        (x0, y0), (x1, y1) = start, toward
        d = math.dist(start, toward)
        if not d:
            return start
        k = min(LIFT_DIST, d) / d
        x, y = start
        for i in range(1, LIFT_STEPS + 1):
            x, y = x0 + (x1 - x0) * k * i / LIFT_STEPS, y0 + (y1 - y0) * k * i / LIFT_STEPS
            if self.absolute:
                await self._at(x, y)
            else:
                await self._move_to(self._dx(x), self._dy(y), rehome=False)
            await asyncio.sleep(seconds / LIFT_STEPS)
        return x, y

    async def _glide(self, last: tuple[float, float], to: tuple[float, float],
                     step: float | None = None) -> tuple[float, float]:
        """Move with the button down: small absolute steps (one jump would be a teleport, not a finger), or one
        relative move (never re-homing mid-gesture)."""
        x, y = to
        if self.absolute:
            n = max(1, math.ceil(math.dist(last, to) / (step or ABS_STEP) - 1e-9))  # 0.3 / 0.03 is 10.000…02
            for i in range(1, n + 1):
                await self._at(last[0] + (x - last[0]) * i / n, last[1] + (y - last[1]) * i / n)
                await asyncio.sleep(ABS_STEP_S)
        else:
            await self._move_to(self._dx(x), self._dy(y), rehome=False)
        return to

    async def carry(self, start: tuple[float, float], target: tuple[float, float], pages: int,
                    pick: float = 0.5, lift: float = 0.6, rest: float = DROP) -> int:
        """Move a Home Screen item to the next page (``pages`` = 1, right) or the previous one (-1, left), dropping it
        on ``target`` there. Edit mode only.

        Picks the item up (``pick`` + slow ``lift``), holds it at the screen edge and watches the live screen: as soon
        as the page starts to turn (see _turn_started) and the turn has settled it jumps off the edge to ``target`` in
        one report, so exactly one page turns. Timing the hold instead turned one to three pages for the same wait
        (#35); while held on, iOS turns page after page with no still moment between them, so only the first turn can
        be told apart reliably.
        For two pages, carry twice with an empty spot on the page between. Returns 1. When the page does not turn
        within FLIP_TIMEOUT, the item is carried back to ``start`` and let go there, and RuntimeError is raised.
        """
        if pages not in (1, -1):
            raise ValueError("pages must be 1 (next page, right) or -1 (previous page, left)")
        if self.screen is None or not self.screen.live:
            raise RuntimeError("carrying across pages needs the live screen to see the page turn")
        edge = (EDGE_X[1] if pages > 0 else EDGE_X[0], CARRY_Y)
        await self.point(*start)
        resting = self.screen.frame  # the start slot with the item in it
        await self.device.send("press")
        try:
            if pick:
                await asyncio.sleep(pick)
            last = await self._lift(start, edge, lift) if lift else start
            await self._glide(last, edge)
            await asyncio.sleep(FLIP_POLL)
            if vision.region_change(resting, self.screen.frame, start) <= LIFT_GONE:  # still in its slot (#35)
                raise RuntimeError("the item did not lift (is the Home Screen in edit mode, and settled?)")
            began = await self._turn_started(self.screen.frame)
            if began is None:
                await self._glide(edge, start)
                await asyncio.sleep(rest)
                raise RuntimeError(f"page did not turn within {FLIP_TIMEOUT} s at the screen edge")
            await self._turn_settled(began)  # leaving mid-turn made iOS turn back (device 2026-10-17 23:21, #35)
            # then leave at once: gliding off left the item in iOS's edge zone into the next turn (#54, 09:32 KST)
            await self._jump(target)
            await asyncio.sleep(max(rest, FLIP_SETTLE))
        finally:
            await self.device.send("release")
        return 1

    async def _jump(self, to: tuple[float, float]) -> None:
        """Leave the screen edge with the button down in one step (absolute: a single report; relative: one move)."""
        if self.absolute:
            await self._at(*to)
        else:
            await self._move_to(self._dx(to[0]), self._dy(to[1]), rehome=False)

    async def _turn_settled(self, began: float) -> None:
        """Wait until the turn that began at ``began`` (_clock) is over, at most FLIP_SETTLE_MAX after it began; the
        next turn follows about 0.45 s later, so this returns on the first still frame once FLIP_SETTLE_MIN is past.

        Timed from the turn's start, in real time: counted from its detection (a frame later between look-alike
        pages) and in polls (each also decodes frames), the first still frame was passed over and the edge was left
        after iOS had begun the next turn (#54, device 2026-10-18 07:20 KST).
        Only newly pushed frames count: the screen app sends a frame when something changed, so two polls often see
        the same frame, which said "still" in the middle of the turn; leaving then made iOS turn back (#35).
        """
        prev = self.screen.frame
        while _clock() - began < FLIP_SETTLE_MAX:
            await asyncio.sleep(FLIP_POLL)
            now = self.screen.frame
            if now is prev:  # no new frame yet: no evidence either way
                continue
            if _clock() - began >= FLIP_SETTLE_MIN and vision.page_change(prev, now) < FLIP_STILL:
                return
            prev = now

    async def _turn_started(self, before: bytes | None) -> float | None:
        """Wait at the edge until a page turn has begun: most of the content differs from ``before``, or the content
        slides on FLIP_MOTION_FRAMES newly pushed frames in a row (look-alike pages differ in few cells, #54), or it
        stays FLIP_AWAY from the last still frame on as many new frames (a quick turn slides on one frame only).
        Returns when (_clock) the turn was first seen: the first of those sliding frames, if any; None if no turn."""
        waited, prev, still, sliding, slid_at, away, away_at = 0.0, before, before, 0, 0.0, 0, 0.0
        while waited < FLIP_TIMEOUT:
            await asyncio.sleep(FLIP_POLL)
            waited += FLIP_POLL
            now = self.screen.frame
            if vision.cells_changed(before, now) > FLIP_CELLS:
                return slid_at if sliding else away_at if away else _clock()
            if now is prev:  # no new frame yet
                continue
            step = vision.page_change(prev, now)
            if step > FLIP_MOTION:
                sliding += 1
                if sliding == 1:
                    slid_at = _clock()
            else:
                sliding = 0
            if vision.page_change(still, now) > FLIP_AWAY:  # moved away from the page as it last held still
                away += 1
                if away == 1:
                    away_at = _clock()
            else:
                away = 0
                if step < FLIP_STILL:  # still and not turned: measure from here (the item settling at the edge)
                    still = now
            if sliding >= FLIP_MOTION_FRAMES:
                return slid_at
            if away >= FLIP_MOTION_FRAMES:
                return away_at
            prev = now
        return None

    async def scroll(self, x: float, y: float, lines: int) -> None:
        await self.point(x, y)
        await self.device.send(f"scroll {lines} 0")

    async def type(self, text: str, piece: int = 200) -> None:
        """ASCII as it is; Korean as 2-set layout keys. Mixed text is typed run by run, the input language switched
        (key ctrl+space) between a Korean and an English run and switched back at the end, so the phone is left in
        the language it started in; which language that is remains the phone's state (the agent checks the field)."""
        runs = script_runs(text) if has_hangul(text) else [(False, text)]
        keys = []
        for korean, run in runs:
            try:
                part = to_dubeolsik(run) if korean else run
            except ValueError as e:
                raise DeviceError(str(e)) from e
            if not part.isascii():  # the USB keyboard can only type ASCII; fail clearly instead of typing garbage
                raise DeviceError("only ASCII or Korean text can be typed")
            keys.append(part)
        for n, part in enumerate(keys):
            if n:
                await self.device.send("key ctrl+space")
            for i in range(0, len(part), piece):  # the board takes lines of limited length
                await self.device.send(f"type {part[i:i + piece]}")
        if len(keys) > 1 and len(keys) % 2 == 0:  # an odd number of switches: back to the language we started in
            await self.device.send("key ctrl+space")

    async def key(self, combo: str) -> None:
        await self.device.send(f"key {combo}")

    async def go_home(self) -> None:
        if self.home.key:
            await self.device.send("consumer home")
            return
        if self.home.button is None:
            await self.swipe(*HOME_SWIPE[0], *HOME_SWIPE[1], hold=False)  # resting there would open the app switcher
            return
        # Plain taps: aiming wiggles the pointer over the translucent button and tends to miss it (#17-B).
        await self.tap(*self.home.button)
        if self.home.menu_home:
            await self._menu_open()
            await self.tap(*self.home.menu_home)

    async def _menu_open(self) -> None:
        """Wait until the AssistiveTouch menu has finished opening."""
        if self.screen is not None and self.screen.live:
            await self.screen.settled(quiet=0.2, least=0.3, most=1.5)
        else:
            await asyncio.sleep(MENU_OPEN_S)

    async def go_back(self) -> None:
        await self.swipe(0.005, 0.45, 0.6, 0.45, hold=False)

    def _dx(self, x: float) -> int:
        return round(min(max(self._fix(x, "x"), 0), 1) * self.span["w"])

    def _dy(self, y: float) -> int:
        return round(min(max(self._fix(y, "y"), 0), 1) * self.span["h"])
