"""Web API + control page. Run with `python -m hidagent`."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, bench, vision
from .agent import Agent
from .debug import Trace
from .device import Device, DeviceError
from .gate import Gate
from .llm import LLM
from .ocr import OCR
from .phone import HomeVia, Phone
from .screen import Screen
from .screen_push import DEFAULT_PORT as PUSH_DEFAULT_PORT
from .screen_push import PushScreen

DATA_DIR = auth.private_dir(Path(os.environ.get("HIDAGENT_DATA", Path.home() / ".hidagent")))
DEBUG = os.environ.get("HIDAGENT_DEBUG") == "1"
log = logging.getLogger(__name__)

UI_TOKEN = auth.ui_token(DATA_DIR)
UI_TOKEN_FROM_ENV = bool(os.environ.get("UI_TOKEN", "").strip())
COOKIE = "hidagent_token"


class _RedactToken(logging.Filter):
    """Keep the token out of uvicorn's access log ("GET /?token=… HTTP/1.1"), however it is spelled or encoded:
    query strings are dropped from access log lines altogether (they carry nothing else worth keeping)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(auth.redact_query(a) if isinstance(a, str) else a for a in record.args)
        record.msg = auth.redact_query(str(record.msg))
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactToken())
AIRPLAY_PIN = auth.airplay_pin(DATA_DIR)
# airplay (default): the phone mirrors to UxPlay on this network; push: the companion app sends JPEGs over the VPN
SCREEN_SOURCE = os.environ.get("SCREEN_SOURCE", "airplay").strip().lower() or "airplay"
if SCREEN_SOURCE not in ("airplay", "push"):
    raise SystemExit(f"SCREEN_SOURCE must be 'airplay' or 'push', not {SCREEN_SOURCE!r}")
PUSH_TOKEN_FROM_ENV = bool(os.environ.get("PUSH_TOKEN", "").strip())

device = Device(auth.device_key())
if SCREEN_SOURCE == "push":
    screen = PushScreen(os.environ.get("PUSH_HOST", ""), int(os.environ.get("PUSH_PORT", PUSH_DEFAULT_PORT)),
                        auth.push_token(DATA_DIR))
else:
    screen = Screen(os.environ.get("AIRPLAY_NAME", "iPhone Agent"), pin=AIRPLAY_PIN,
                    register=DATA_DIR / "uxplay.register")
try:
    # how to reach the Home Screen: bottom flick (default) or AssistiveTouch (#17); relative mouse or absolute
    # pointer, which must match how the board was built (ABSOLUTE_POINTER, #14)
    phone = Phone(device, DATA_DIR / "calibration.json", screen, HomeVia.from_env(),
                  os.environ.get("POINTER_MODE", "").strip().lower() or "relative")
except ValueError as e:
    raise SystemExit(str(e)) from None
ocr = OCR.from_env()  # optional: text on the screen with coordinates (pip install -e '.[ocr]')
agent = Agent(phone, screen, LLM.from_env("ACTOR"), LLM.from_env("PLANNER"),
              Trace(DATA_DIR / "traces" if DEBUG else None), gate=Gate.from_env(DATA_DIR), ocr=ocr)


@asynccontextmanager
async def lifespan(_):
    if device.key is None:
        raise SystemExit("refusing to start without a valid DEVICE_KEY (see the error above)")
    server = await device.serve(os.environ.get("DEVICE_HOST", "0.0.0.0"), int(os.environ.get("DEVICE_PORT", 7300)))
    port = os.environ.get("PORT", 8890)
    if UI_TOKEN_FROM_ENV:  # the user chose it: no need to print it
        log.info("web UI: http://<this computer>:%s/?token=<your UI_TOKEN>", port)
    else:
        log.info("web UI: http://<this computer>:%s/?token=%s  (kept in %s)", port, UI_TOKEN, DATA_DIR / "ui_token")
    if SCREEN_SOURCE == "push":
        where = "<your PUSH_TOKEN>" if PUSH_TOKEN_FROM_ENV else f"{screen.token}  (kept in {DATA_DIR / 'push_token'})"
        log.info("screen source: push -- the app connects to ws://%s:%d/ with token %s",
                 screen.host, screen.port, where)
    else:
        log.info("AirPlay pin: %s", AIRPLAY_PIN or "off (any phone on the network may mirror to the receiver)")
    screen.start()
    awake = asyncio.create_task(device.keep_awake(screen=screen))
    yield
    awake.cancel()
    screen.stop()
    server.close()


app = FastAPI(lifespan=lifespan)
class Static(StaticFiles):
    """Static files that browsers re-check on every load, so a new version shows up on refresh."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", Static(directory=Path(__file__).parent / "static"), name="static")

LOGIN = """<!doctype html><meta charset="utf-8"><title>iPhone Agent</title>
<body style="font:16px system-ui;max-width:36em;margin:4em auto;padding:0 1em">
<h1>Token required</h1><p>Open the address with <code>?token=…</code> that the server printed when it started
(<code>web UI: http://…/?token=…</code>). The token is also kept in <code>~/.hidagent/ui_token</code>.</p>"""


def _cross_origin(request: Request) -> bool:
    """A state-changing request a browser sent on behalf of another page (even one on this host, another port)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    return origin is not None and origin.split("://", 1)[-1].lower() != request.headers.get("host", "").lower()


@app.middleware("http")
async def require_token(request: Request, call_next):
    """Every page and API call needs the UI token: a cookie (set by opening /?token=…) or an Authorization header.

    The cookie is SameSite=Strict and changes are only accepted from this page's own origin, so other pages
    cannot drive the phone through the user's browser (no CSRF, no DNS rebinding).
    """
    path = request.url.path
    if path.startswith("/static/"):  # the page's own scripts and styles: nothing secret
        return await call_next(request)
    if path == "/" and (given := request.query_params.get("token")) is not None and auth.same(given, UI_TOKEN):
        response = RedirectResponse("/", status_code=303)  # drop the token from the address bar
        response.set_cookie(COOKIE, UI_TOKEN, max_age=30 * 86400, httponly=True, samesite="strict")
        return response
    if _cross_origin(request):
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    header = request.headers.get("authorization", "")
    candidates = (request.cookies.get(COOKIE, ""), header[7:] if header[:7].lower() == "bearer " else "")
    if any(c and auth.same(c, UI_TOKEN) for c in candidates):  # either may be right (e.g. a stale cookie + header)
        return await call_next(request)
    if path == "/":
        return HTMLResponse(LOGIN, status_code=401)
    return JSONResponse({"detail": "unauthorized: open the web UI address with ?token=… first"}, status_code=401)


class Point(BaseModel):
    x: float
    y: float


class Swipe(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class Scroll(Point):
    lines: int


class Text(BaseModel):
    text: str


async def act(coro, *spots: tuple[float, float]):
    """Run one phone action, wait for the screen to settle and report what happened (#9, after iCTRL).

    ``spots`` are the screen fractions the pointer visits for this action: ignored in the change check, so the
    pointer's own movement does not count as the screen changing. ``screen_changed`` is None without a live
    frame before and after.
    """
    before, since = screen.frame, time.time()
    try:
        await coro
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e
    settled = await screen.settled(since=since)  # the change may reach us later than the default wait (#17-F)
    waited, stable = settled if isinstance(settled, tuple) else (None, None)
    after = screen.frame
    changed = vision.changed(before, after, ignore=spots) if before and after else None
    return {"ok": True, "settle": {"waited_s": waited, "stable": stable}, "screen_changed": changed}


SKILL = Path(__file__).parent / "skill.md"


@app.get("/api/skill")
def skill():
    """The operating manual for an agent driving this server over HTTP (same token as every other call)."""
    return PlainTextResponse(SKILL.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/status")
def status():
    return {"device": device.connected, "screen": screen.live, "source": SCREEN_SOURCE, "calibrated": phone.calibrated,
            "pointer": "absolute" if phone.absolute else "relative",
            "debug": DEBUG, "trace": str(agent.trace.dir or ""), "aspect": round(screen.aspect, 4),
            "gate": agent.gate.mode if agent.gate else "off", "ocr": ocr.mode,
            "models": {"planner": agent.planner.model, "actor": agent.actor.model},
            "agent": {"running": agent.running, "task": agent.task, "steps": agent.steps}}


@app.get("/api/frame.jpg")
def frame():
    if not screen.live:
        raise HTTPException(404, "no live screen: start screen mirroring on the phone")
    return Response(screen.frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/screen_text")
async def screen_text(min_conf: float = 0.5):
    """Text on the current screen with the centre of each line as fractions of the frame (OCR, optional extra)."""
    if not ocr.enabled:
        from .ocr import import_problem

        why = import_problem() or "OCR_MODE=off"
        raise HTTPException(503, f"OCR is off ({why}); install the extra with pip install -e '.[ocr]', then set "
                                 "OCR_MODE=api or actor")
    if not screen.live:
        raise HTTPException(404, "no live screen: start screen mirroring on the phone")
    frame, updated = screen.frame, screen.updated
    items = [r for r in await ocr.read(frame) if r["conf"] >= min_conf]
    return {"items": items, "count": len(items), "took_s": ocr.last_s, "frame_updated": updated,
            "aspect": round(screen.aspect, 4)}


@app.get("/api/steps/{index}.jpg")
def step_shot(index: int):
    if index not in agent.shots:
        raise HTTPException(404)
    return Response(agent.shots[index], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/tap")
async def tap(p: Point):
    return await act(phone.tap(p.x, p.y), (p.x, p.y))


@app.post("/api/swipe")
async def swipe(s: Swipe):
    return await act(phone.swipe(s.x1, s.y1, s.x2, s.y2), (s.x1, s.y1), (s.x2, s.y2))


@app.post("/api/scroll")
async def scroll(s: Scroll):
    return await act(phone.scroll(s.x, s.y, s.lines), (s.x, s.y))


@app.post("/api/type")
async def type_text(t: Text):
    return await act(phone.type(t.text))


@app.post("/api/key")
async def key(t: Text):
    return await act(phone.key(t.text))


@app.post("/api/home")
async def home():
    return await act(phone.go_home())


@app.post("/api/back")
async def back():
    return await act(phone.go_back())


@app.post("/api/raw")
async def raw(t: Text):
    """Debug only: send one protocol line to the board as-is."""
    if not DEBUG:
        raise HTTPException(404)
    return await act(device.send(t.text))


@app.post("/api/bench/pointer")
async def bench_pointer(rounds: int = 1):
    """Pointer accuracy and move time at nine spots, nothing clicked (#14). Returns a Markdown table too."""
    if agent.running:
        raise HTTPException(409, "the agent is running: stop it first")
    if not screen.live:
        raise HTTPException(404, "no live screen: start screen mirroring on the phone")
    try:
        return await bench.pointer(phone, rounds=max(1, min(rounds, 5)))
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/calibrate/start")
async def calibrate_start():
    """Calibrate the pointer. {"auto": true}: done by itself; otherwise the user clicks the pointer next."""
    try:
        return {"ok": True, "auto": await phone.start_calibration(), "span": phone.span}
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/calibrate/finish")
def calibrate_finish(p: Point):
    phone.finish_calibration(p.x, p.y)
    return {"ok": True, "span": phone.span}


@app.post("/api/agent")
async def agent_start(t: Text):
    try:
        agent.start(t.text)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/api/agent/stop")
async def agent_stop():
    """Emergency stop: cancel the agent and release the mouse button."""
    agent.stop()
    await asyncio.sleep(0.1)  # let a gesture in flight finish its own release
    return await act(phone.reset()) if device.connected else {"ok": True}


@app.post("/api/agent/clear")
def agent_clear():
    """Forget the last run (steps, screenshots, plan) so the page starts clean."""
    try:
        agent.clear()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}
