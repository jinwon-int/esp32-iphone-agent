#!/usr/bin/env python3
"""OpenAI-compatible POST /v1/chat/completions -> Claude Code CLI (headless `claude -p`), loopback only.

Why: esp32-iphone-agent talks to any OpenAI-compatible vision endpoint; this node is signed in to Claude Code
(subscription OAuth). The OAuth token is never read or reused here: every request runs the official `claude`
CLI in print mode, with tools disabled, user hooks/settings skipped (--setting-sources project, empty cwd),
no session persistence, and the callers system prompt replacing Claude Codes.
Logs carry timing and counts only, never prompt text or images.

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


def ask(system, blocks, model, effort=""):
    cmd = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--tools", "", "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence",
           "--model", model]  # strict: no claude.ai connector tools/instructions in the phone model (2026-10-17)
    if effort:
        cmd += ["--effort", effort]  # thinking budget; "" leaves the CLI default (2026-10-22)
    if system:
        cmd += ["--system-prompt", system]
    line = json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}) + "\n"
    with SLOTS:
        p = subprocess.run(cmd, input=line, capture_output=True, text=True, cwd=WORKDIR, timeout=TIMEOUT)
    result = None
    for raw in p.stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "result":
            result = event
    if result is None or result.get("is_error"):
        reason = (result or {}).get("subtype") or f"rc={p.returncode}"
        raise RuntimeError(f"claude failed: {reason}")
    return result.get("result") or "", result.get("usage") or {}


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
            text, usage = ask("\n\n".join(system), blocks, model, effort)
        except subprocess.TimeoutExpired:
            log(f"timeout {tag} images={images} after {TIMEOUT:.0f}s")
            return self.reply(504, {"error": {"message": "claude timed out"}})
        except Exception as e:  # noqa: BLE001 - report, do not crash the server
            log(f"error {tag} images={images}: {e}")
            return self.reply(502, {"error": {"message": str(e)[:200]}})
        ms = int((time.time() - started) * 1000)
        log(f"ok {tag} images={images} kept={kept} ms={ms} out_chars={len(text)}")
        self.reply(200, {"id": "chatcmpl-" + uuid.uuid4().hex[:12], "object": "chat.completion",
                         "created": int(time.time()), "model": model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                                   "completion_tokens": usage.get("output_tokens", 0)}})


if __name__ == "__main__":
    log(f"listening on {HOST}:{PORT}, default model {DEFAULT_MODEL}, "
        + (f"screen images kept {SHOT_DAYS:g} days" if SHOT_DIR else "screen images not kept"))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
