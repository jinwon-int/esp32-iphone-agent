#!/usr/bin/env python3
"""OpenAI-compatible POST /v1/chat/completions -> Claude Code CLI (headless `claude -p`), loopback only.

Why: esp32-iphone-agent talks to any OpenAI-compatible vision endpoint; this node is signed in to Claude Code
(subscription OAuth). The OAuth token is never read or reused here: every request runs the official `claude`
CLI in print mode, with tools disabled, user hooks/settings skipped (--setting-sources project, empty cwd),
no session persistence, and the callers system prompt replacing Claude Codes.
Logs carry timing and counts only, never prompt text or images.

Prewarm (iphone-agent-cast#136, owner approval 2026-10-31): starting `claude -p` costs ~0.77 s before it sends its
first API request; a process started ahead of time and left waiting on stdin sends it ~0.05 s after the input arrives.
So after each request the adapter starts the next process for the same command (model, effort, system prompt) and
leaves it waiting. At most ADAPTER_PREWARM_MAX wait at once; one waiting longer than ADAPTER_PREWARM_MAX_AGE seconds
is ended, so an idle phone leaves nothing running. ADAPTER_PREWARM=0 restores one fresh process per request.

Screen images are kept on this node only when ADAPTER_SHOT_DIR is set (owner decision 2026-10-23: kept here, where
nobody else can read them, so a run can be looked at afterwards, and deleted after ADAPTER_SHOT_DAYS, 7 by default).
The folder is 0700 and every image 0600; nothing else of the request (prompt text, replies) is written.
"""
import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("ADAPTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("ADAPTER_PORT", "18765"))
TOKEN = Path(os.environ["ADAPTER_TOKEN_FILE"]).read_text().strip()
WORKDIR = Path(os.environ.get("ADAPTER_WORKDIR", Path(__file__).with_name("empty")))
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
TIMEOUT = float(os.environ.get("ADAPTER_TIMEOUT", "110"))
DEFAULT_MODEL = os.environ.get("ADAPTER_DEFAULT_MODEL", "sonnet")
SLOTS = threading.BoundedSemaphore(int(os.environ.get("ADAPTER_CONCURRENCY", "2")))
SHOT_DIR = os.environ.get("ADAPTER_SHOT_DIR", "").strip()  # "" = screen images are not kept (the default)
SHOT_DAYS = float(os.environ.get("ADAPTER_SHOT_DAYS", "7"))
SHOT_PRUNE_EVERY = 3600.0  # seconds between sweeps for images older than SHOT_DAYS
SHOT_SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def env_number(name, default, cast):
    """A numeric setting; a malformed value falls back to the default instead of stopping the adapter."""
    try:
        return cast(os.environ.get(name, default))
    except ValueError:
        return cast(default)


PREWARM_MAX = max(1, env_number("ADAPTER_PREWARM_MAX", "4", int))
PREWARM_MAX_AGE = env_number("ADAPTER_PREWARM_MAX_AGE", "300", float)
PREWARM = (os.environ.get("ADAPTER_PREWARM", "1").strip().lower() not in ("0", "off", "no", "false", "")
           and PREWARM_MAX_AGE > 0)
_shots = threading.Lock()
_last_prune = [0.0]


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=sys.stderr, flush=True)


EFFORTS = ("low", "medium", "high", "xhigh", "max")  # claude --effort levels (2.1.305)
DEFAULT_EFFORT = os.environ.get("ADAPTER_EFFORT", "").strip().lower()  # "" = the CLI's own default


def pick_model(name):
    """The model, and the effort named in it: "claude-sonnet-5-5@low" (an app needs no new field for an A/B)."""
    name = (name or "").strip()
    effort = ""
    if "@" in name:
        name, effort = name.rsplit("@", 1)
    model = name if name in ("sonnet", "opus", "haiku") or name.startswith("claude-") else DEFAULT_MODEL
    return model, effort.strip().lower()


def pick_effort(*given):
    """The first valid effort of: the model suffix, the request's reasoning_effort, ADAPTER_EFFORT; else ""."""
    for e in given:
        e = (e or "").strip().lower() if isinstance(e, str) else ""
        if e in EFFORTS:
            return e
    return ""


def blocks_of(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for part in content or []:
        kind = part.get("type")
        if kind == "text":
            out.append({"type": "text", "text": part.get("text", "")})
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                media, data = url[5:].split(";base64,", 1)
                base64.b64decode(data[:64] + "==", validate=False)  # cheap sanity check
                out.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    return out


def prune_images(folder, now, days):
    """Delete kept images older than `days`; only files this adapter writes (shot-*), nothing else."""
    cutoff, gone = now - days * 86400, 0
    for f in Path(folder).glob("shot-*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def keep_images(blocks, folder=None, days=None, now=None):
    """Write each image block to `folder` (SHOT_DIR) as shot-<local time>-<id>-<n>.<ext>, 0600, and sweep out
    images older than `days` at most once an hour. Returns the paths written; [] when keeping is off."""
    folder = SHOT_DIR if folder is None else folder
    if not folder:
        return []
    days = SHOT_DAYS if days is None else days
    now = time.time() if now is None else now
    d = Path(folder)
    with _shots:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        rid, written = uuid.uuid4().hex[:6], []
        for n, b in enumerate(b for b in blocks if b["type"] == "image"):
            src = b["source"]
            path = d / f"shot-{stamp}-{rid}-{n}{SHOT_SUFFIX.get(src['media_type'], '.img')}"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(base64.b64decode(src["data"]))
            written.append(path)
        if now - _last_prune[0] >= SHOT_PRUNE_EVERY:
            _last_prune[0] = now
            gone = prune_images(d, now, days)
            if gone:
                log(f"pruned {gone} screen images older than {days:g} days")
    return written


def command(system, model, effort=""):
    cmd = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--tools", "", "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence",
           "--model", model]  # strict: no claude.ai connector tools/instructions in the phone model (2026-10-17)
    if effort:
        cmd += ["--effort", effort]  # thinking budget; "" leaves the CLI default (2026-10-22)
    if system:
        cmd += ["--system-prompt", system]
    return cmd


def spawn(cmd):
    """Start `claude -p`; it boots and then waits for the one user line on stdin."""
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=WORKDIR)


def end(proc):
    """Stop a `claude -p` so it can clean up after itself: close its stdin and send SIGTERM, then SIGKILL only if it
    is still there after 5 s. SIGKILL alone leaves ~/.claude/sessions/<pid>.* and a cc-socks socket behind (#116)."""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:  # noqa: BLE001 - a broken pipe means it is already going
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
        return
    except Exception:  # noqa: BLE001 - gone already, or did not stop in time
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - will be reaped by the unit's cgroup
        pass


def retire(procs):
    """End processes off the request path (end() may wait up to 5 s each)."""
    procs = [p for p in procs if p is not None]
    if procs:
        threading.Thread(target=lambda: [end(p) for p in procs], daemon=True).start()


class Warm:
    """Idle `claude -p` processes started ahead of the request that will use them, one per command."""

    def __init__(self, start=None, most=PREWARM_MAX, max_age=PREWARM_MAX_AGE, clock=time.monotonic):
        self._start, self._most, self._max_age, self._clock = start or spawn, max(1, most), max_age, clock
        self._lock, self._idle = threading.Lock(), {}  # tuple(cmd) -> (process, started at)

    def take(self, cmd):
        """The waiting process for `cmd`, or None when there is none that is still alive and young enough."""
        with self._lock:
            entry = self._idle.pop(tuple(cmd), None)
        if entry is None:
            return None
        proc, born = entry
        if proc.poll() is None and self._clock() - born < self._max_age:
            return proc
        retire([proc])
        return None

    def refill(self, cmd):
        """Start the next process for `cmd`; the oldest waiting one goes when more than `most` would wait."""
        gone = []
        try:
            proc = self._start(cmd)
        except Exception as e:  # noqa: BLE001 - prewarm is an optimisation: a request never fails for it
            log(f"prewarm start failed: {type(e).__name__}")
            return
        with self._lock:
            old = self._idle.pop(tuple(cmd), None)
            if old:
                gone.append(old[0])
            while len(self._idle) >= self._most:
                oldest = min(self._idle, key=lambda k: self._idle[k][1])
                gone.append(self._idle.pop(oldest)[0])
            self._idle[tuple(cmd)] = (proc, self._clock())
        retire(gone)

    def reap(self):
        """End processes that waited longer than `max_age` (or died); returns how many went."""
        now = self._clock()
        with self._lock:
            stale = [k for k, (p, born) in self._idle.items() if now - born >= self._max_age or p.poll() is not None]
            gone = [self._idle.pop(k)[0] for k in stale]
        retire(gone)
        return len(gone)

    def size(self):
        with self._lock:
            return len(self._idle)


WARM = Warm() if PREWARM else None


def reap_forever(warm, every=30.0):
    while True:
        time.sleep(every)
        try:
            warm.reap()
        except Exception as e:  # noqa: BLE001 - keep sweeping; take() refuses stale processes meanwhile
            log(f"prewarm sweep failed: {type(e).__name__}")


def result_of(stdout, returncode):
    result = None
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "result":
            result = event
    if result is None or result.get("is_error"):
        reason = (result or {}).get("subtype") or f"rc={returncode}"
        raise RuntimeError(f"claude failed: {reason}")
    info = {k: result[k] for k in ("duration_ms", "duration_api_ms") if isinstance(result.get(k), (int, float))}
    return result.get("result") or "", result.get("usage") or {}, info


def ask(system, blocks, model, effort="", warm=None):
    """Run one request; returns (text, usage, info). info: warm (bool) and the CLI's duration_ms/duration_api_ms."""
    warm = WARM if warm is None else warm
    cmd = command(system, model, effort)
    line = json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}) + "\n"
    with SLOTS:
        if not warm:
            p = subprocess.run(cmd, input=line, capture_output=True, text=True, cwd=WORKDIR, timeout=TIMEOUT)
            text, usage, info = result_of(p.stdout, p.returncode)
            return text, usage, dict(info, warm=False)
        proc = warm.take(cmd)
        was_warm = proc is not None
        try:
            out, rc = send(proc or spawn(cmd), line)
            if was_warm and not out.strip():  # it died while waiting (killed, OOM): run this one fresh
                log("prewarmed process gave no output; running fresh")
                was_warm = False
                out, rc = send(spawn(cmd), line)
            text, usage, info = result_of(out, rc)
        finally:
            warm.refill(cmd)  # after the reply is in hand, so it does not compete with a cold start
        return text, usage, dict(info, warm=was_warm)


def send(proc, line):
    """Give a started process its one input line; returns (stdout, return code)."""
    try:
        out, _ = proc.communicate(input=line, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        retire([proc])
        raise
    return out, proc.returncode


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # no request lines (they would only add noise)
        pass

    def reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/health"):
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self.reply(404, {"error": {"message": "not found"}})
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(given, TOKEN):
            return self.reply(401, {"error": {"message": "unauthorized"}})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except ValueError:
            return self.reply(400, {"error": {"message": "bad json"}})
        system, blocks = [], []
        for message in body.get("messages", []):
            if message.get("role") == "system":
                system += [b["text"] for b in blocks_of(message.get("content")) if b["type"] == "text"]
            else:
                blocks += blocks_of(message.get("content"))
        model, suffix = pick_model(body.get("model"))
        effort = pick_effort(suffix, body.get("reasoning_effort"), DEFAULT_EFFORT)
        tag = f"model={model} effort={effort or 'default'}"
        images = sum(b["type"] == "image" for b in blocks)
        try:
            kept = len(keep_images(blocks))
        except Exception as e:  # noqa: BLE001 - keeping is a by-product: never fail the request for it
            kept = 0
            log(f"error keeping images: {type(e).__name__}")
        started = time.time()
        try:
            text, usage, info = ask("\n\n".join(system), blocks, model, effort)
        except subprocess.TimeoutExpired:
            log(f"timeout {tag} images={images} after {TIMEOUT:.0f}s")
            return self.reply(504, {"error": {"message": "claude timed out"}})
        except Exception as e:  # noqa: BLE001 - report, do not crash the server
            log(f"error {tag} images={images}: {e}")
            return self.reply(502, {"error": {"message": str(e)[:200]}})
        ms = int((time.time() - started) * 1000)
        timing = "".join(f" {name}={int(info[key])}" for key, name in (("duration_ms", "cli_ms"),
                                                                       ("duration_api_ms", "api_ms")) if key in info)
        log(f"ok {tag} images={images} kept={kept} ms={ms} out_chars={len(text)} warm={int(info.get('warm', False))}"
            + timing)
        self.reply(200, {"id": "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion",
                         "created": int(time.time()), "model": model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                                   "completion_tokens": usage.get("output_tokens", 0)}})


if __name__ == "__main__":
    log(f"listening on {HOST}:{PORT}, default model {DEFAULT_MODEL}, "
        + (f"screen images kept {SHOT_DAYS:g} days" if SHOT_DIR else "screen images not kept")
        + (f", prewarm up to {PREWARM_MAX} for {PREWARM_MAX_AGE:g}s" if WARM else ", prewarm off"))
    if WARM:
        threading.Thread(target=reap_forever, args=(WARM,), daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
