"""The Claude CLI adapter (server/adapter/claude_adapter.py): model/effort selection, content blocks and the
`claude -p` command it builds. The adapter is one file deployed as-is to the node that is signed in to Claude Code
(see server/adapter/README.md); these tests pin what the app relies on."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).resolve().parents[1] / "adapter" / "claude_adapter.py"


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("t-test\n")
    monkeypatch.setenv("ADAPTER_TOKEN_FILE", str(token))
    monkeypatch.delenv("ADAPTER_EFFORT", raising=False)
    monkeypatch.delenv("ADAPTER_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("ADAPTER_SHOT_DIR", raising=False)
    monkeypatch.setenv("ADAPTER_PREWARM", "0")  # one fresh `claude -p` per request; prewarm has its own tests
    spec = importlib.util.spec_from_file_location("claude_adapter_under_test", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


def test_model_names_and_the_effort_suffix(adapter):
    """`claude-sonnet-5-5@low` names the effort in the model (the app has no effort field); unknown names fall back."""
    assert adapter.pick_model("claude-sonnet-5-5@low") == ("claude-sonnet-5-5", "low")
    assert adapter.pick_model("claude-opus-5-5") == ("claude-opus-5-5", "")
    assert adapter.pick_model("sonnet@LOW") == ("sonnet", "low")
    assert adapter.pick_model("gpt-4o@low") == ("sonnet", "low")  # not a Claude name: the default model, suffix kept
    assert adapter.pick_model(None) == ("sonnet", "")
    assert adapter.TOKEN == "t-test"


def test_effort_precedence_suffix_then_body_then_env(adapter, monkeypatch):
    assert adapter.pick_effort("", None, "") == ""
    assert adapter.pick_effort("bogus", "medium") == "medium"  # an unknown level is skipped, not sent
    assert adapter.pick_effort(None, {"x": 1}, "xhigh") == "xhigh"  # a non-string body value is skipped
    assert adapter.pick_effort("low", "high", "max") == "low"
    assert adapter.EFFORTS == ("low", "medium", "high", "xhigh", "max")


def test_blocks_of_text_and_data_url_images(adapter):
    assert adapter.blocks_of("hi") == [{"type": "text", "text": "hi"}]
    blocks = adapter.blocks_of([{"type": "text", "text": "Goal"},
                               {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}},
                               {"type": "image_url", "image_url": {"url": "https://example.invalid/x.jpg"}}])
    assert blocks == [{"type": "text", "text": "Goal"},
                      {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9j/4AAQ"}}]


def test_ask_builds_a_strict_headless_claude_command(adapter, monkeypatch):
    """Tools off, strict MCP config, project settings only, no session, the caller's system prompt, --effort only
    when one was chosen; the reply is the last `result` event."""
    seen = {}

    class Done:
        returncode = 0
        stdout = json.dumps({"type": "system"}) + "\n" + json.dumps(
            {"type": "result", "subtype": "success", "result": "THINK: a\nDO: tap 1 2",
             "usage": {"input_tokens": 12, "output_tokens": 7}}) + "\n"

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw["input"]
        return Done()

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)
    text, usage, info = adapter.ask("SYS", [{"type": "text", "text": "Goal: x"}], "claude-sonnet-5-5", "low")
    assert text == "THINK: a\nDO: tap 1 2" and usage == {"input_tokens": 12, "output_tokens": 7}
    assert info == {"warm": False} and adapter.WARM is None
    cmd = seen["cmd"]
    assert cmd[0] == adapter.CLAUDE and cmd[1] == "-p"
    for flag in ("--strict-mcp-config", "--no-session-persistence"):
        assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert json.loads(seen["input"]) == {"type": "user", "message": {"role": "user",
                                                                    "content": [{"type": "text", "text": "Goal: x"}]}}
    adapter.ask("", [], "sonnet", "")
    assert "--effort" not in seen["cmd"] and "--system-prompt" not in seen["cmd"]


def test_ask_reports_a_failed_or_missing_result(adapter, monkeypatch):
    class Failed:
        returncode = 1
        stdout = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True}) + "\n"

    monkeypatch.setattr(adapter.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(RuntimeError, match="claude failed: error_during_execution"):
        adapter.ask("S", [], "sonnet")

    class Silent:
        returncode = 3
        stdout = "not json\n"

    monkeypatch.setattr(adapter.subprocess, "run", lambda *a, **k: Silent())
    with pytest.raises(RuntimeError, match="claude failed: rc=3"):
        adapter.ask("S", [], "sonnet")


# Screen images kept on the node (owner decision 2026-10-23): off unless ADAPTER_SHOT_DIR is set, 0700/0600, a week.

JPEG = b"\xff\xd8\xff\xe0screen"


def image_blocks(adapter, n=2):
    import base64
    url = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    return adapter.blocks_of([{"type": "text", "text": "what is on the screen"}]
                             + [{"type": "image_url", "image_url": {"url": url}}] * n)


def test_screen_images_are_not_kept_by_default(adapter, tmp_path):
    assert adapter.SHOT_DIR == ""
    assert adapter.keep_images(image_blocks(adapter)) == []
    assert list(tmp_path.glob("**/shot-*")) == []


def test_screen_images_are_kept_private_byte_for_byte(adapter, tmp_path):
    import stat
    folder = tmp_path / "shots"
    paths = adapter.keep_images(image_blocks(adapter), folder=str(folder), now=1792760000.0)
    assert len(paths) == 2 and all(p.suffix == ".jpg" and p.name.startswith("shot-") for p in paths)
    assert [p.read_bytes() for p in paths] == [JPEG, JPEG]
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert {stat.S_IMODE(p.stat().st_mode) for p in paths} == {0o600}
    assert not list(folder.glob("*.txt"))  # images only: no prompt text, no replies


def test_images_older_than_the_limit_are_deleted_and_nothing_else(adapter, tmp_path):
    import os
    folder = tmp_path / "shots"
    folder.mkdir()
    now = 1792760000.0
    old, fresh, other = folder / "shot-old.jpg", folder / "shot-fresh.jpg", folder / "notes.txt"
    for f in (old, fresh, other):
        f.write_bytes(b"x")
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(fresh, (now - 6 * 86400, now - 6 * 86400))
    os.utime(other, (now - 30 * 86400, now - 30 * 86400))
    adapter.keep_images(image_blocks(adapter, 1), folder=str(folder), days=7, now=now)
    assert not old.exists() and fresh.exists() and other.exists()


def test_the_sweep_runs_at_most_once_an_hour(adapter, tmp_path):
    import os
    folder = tmp_path / "shots"
    now = 1792760000.0
    adapter.keep_images(image_blocks(adapter, 1), folder=str(folder), days=7, now=now)  # first call sweeps
    late = folder / "shot-late.jpg"
    late.write_bytes(b"x")
    os.utime(late, (now - 9 * 86400, now - 9 * 86400))
    adapter.keep_images(image_blocks(adapter, 1), folder=str(folder), days=7, now=now + 600)
    assert late.exists()  # 10 minutes later: no sweep yet
    adapter.keep_images(image_blocks(adapter, 1), folder=str(folder), days=7, now=now + 3600)
    assert not late.exists()


# Prewarm (iphone-agent-cast#136): the next `claude -p` for the same command is started ahead and left waiting.

RESULT = json.dumps({"type": "result", "subtype": "success", "result": "DO: home",
                     "usage": {"output_tokens": 3}, "duration_ms": 2100, "duration_api_ms": 1900}) + "\n"


class FakeStdin:
    def __init__(self, proc):
        self.proc, self.closed = proc, False

    def close(self):
        self.closed = True
        self.proc.events.append("stdin-close")


class FakeProc:
    def __init__(self, cmd, out=RESULT, rc=0, alive=True, hang=False, stubborn=False):
        self.cmd, self.out, self.returncode, self.alive, self.hang = cmd, out, rc, alive, hang
        self.stubborn, self.killed, self.sent, self.events = stubborn, False, None, []
        self.stdin = FakeStdin(self)

    def poll(self):
        return None if self.alive and not self.killed else self.returncode

    def communicate(self, input=None, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        self.sent = input
        return (self.out if self.alive else ""), ""

    def terminate(self):
        self.events.append("terminate")
        if not self.stubborn:
            self.killed = True

    def kill(self):
        self.events.append("kill")
        self.killed = True

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return self.returncode


import subprocess  # noqa: E402


@pytest.fixture
def spawned(adapter, monkeypatch):
    procs = []

    def fake_spawn(cmd):
        procs.append(FakeProc(cmd))
        return procs[-1]

    monkeypatch.setattr(adapter, "spawn", fake_spawn)
    monkeypatch.setattr(adapter, "retire", lambda ps: [adapter.end(p) for p in ps if p is not None])  # inline
    monkeypatch.setattr(adapter.subprocess, "run", lambda *a, **k: pytest.fail("prewarm must not use run"))
    return procs


def test_end_lets_the_cli_clean_up_before_it_kills(adapter):
    """SIGKILL alone leaves ~/.claude/sessions/<pid>.* behind: stdin is closed and SIGTERM sent first (#116 review)."""
    calm = FakeProc([])
    adapter.end(calm)
    assert calm.events == ["stdin-close", "terminate"] and calm.stdin.closed
    stubborn = FakeProc([], stubborn=True)
    adapter.end(stubborn)
    assert stubborn.events == ["stdin-close", "terminate", "kill"]


def test_prewarm_uses_the_waiting_process_and_starts_the_next(adapter, spawned):
    clock = [0.0]
    warm = adapter.Warm(start=adapter.spawn, most=4, max_age=300, clock=lambda: clock[0])
    text, usage, info = adapter.ask("SYS", [], "sonnet", "", warm=warm)
    assert (text, info) == ("DO: home", {"warm": False, "duration_ms": 2100, "duration_api_ms": 1900})
    assert len(spawned) == 2 and warm.size() == 1  # the cold one ran, the next one waits
    assert spawned[1].cmd == adapter.command("SYS", "sonnet") and spawned[1].sent is None
    clock[0] = 10
    _, _, info = adapter.ask("SYS", [{"type": "text", "text": "x"}], "sonnet", "", warm=warm)
    assert info["warm"] is True and json.loads(spawned[1].sent)["message"]["content"] == [{"type": "text", "text": "x"}]
    assert len(spawned) == 3 and warm.size() == 1
    _, _, info = adapter.ask("OTHER", [], "sonnet", "", warm=warm)  # another system prompt is another command
    assert info["warm"] is False and warm.size() == 2


def test_prewarm_drops_old_dead_and_extra_processes(adapter, spawned):
    clock = [0.0]
    warm = adapter.Warm(start=adapter.spawn, most=2, max_age=300, clock=lambda: clock[0])
    a, b, c = (adapter.command(s, "sonnet") for s in "ABC")
    warm.refill(a)
    clock[0] = 1
    warm.refill(b)
    clock[0] = 2
    warm.refill(c)  # more than `most`: the oldest (A) is ended
    assert spawned[0].killed and warm.size() == 2 and warm.take(a) is None
    clock[0] = 400
    assert warm.take(b) is None and spawned[1].killed  # waited too long
    warm.refill(b)
    spawned[-1].alive = False
    assert warm.take(b) is None and spawned[-1].killed  # died while waiting
    assert warm.reap() == 1 and spawned[2].killed and warm.size() == 0  # C is stale by now


def test_prewarm_timeout_ends_the_process_and_a_failed_start_is_not_fatal(adapter, spawned, monkeypatch):
    warm = adapter.Warm(start=adapter.spawn, most=4, max_age=300, clock=lambda: 0.0)
    warm.refill(adapter.command("S", "sonnet"))
    spawned[0].hang = True
    with pytest.raises(subprocess.TimeoutExpired):
        adapter.ask("S", [], "sonnet", "", warm=warm)
    assert spawned[0].killed and spawned[0].events[:2] == ["stdin-close", "terminate"]
    assert warm.size() == 1  # the next one is started even after a timeout

    def broken(cmd):
        raise OSError("no claude")

    warm2 = adapter.Warm(start=broken, most=4, max_age=300, clock=lambda: 0.0)
    _, _, info = adapter.ask("S", [], "sonnet", "", warm=warm2)  # the request runs cold all the same
    assert info["warm"] is False and warm2.size() == 0


def test_prewarm_is_on_by_default_and_env_switches_it_off(tmp_path, monkeypatch):
    import importlib.util
    token = tmp_path / "token"
    token.write_text("t\n")
    monkeypatch.setenv("ADAPTER_TOKEN_FILE", str(token))
    for value, on in ((None, True), ("0", False), ("off", False), ("1", True)):
        if value is None:
            monkeypatch.delenv("ADAPTER_PREWARM", raising=False)
        else:
            monkeypatch.setenv("ADAPTER_PREWARM", value)
        spec = importlib.util.spec_from_file_location("claude_adapter_prewarm_env", ADAPTER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert (mod.WARM is not None) is on


def test_a_waiting_process_that_died_is_retried_fresh(adapter, spawned):
    warm = adapter.Warm(start=adapter.spawn, most=4, max_age=300, clock=lambda: 0.0)
    warm.refill(adapter.command("S", "sonnet"))
    spawned[0].alive = False  # dies after take() saw it alive
    spawned[0].poll = lambda: None
    text, _, info = adapter.ask("S", [], "sonnet", "", warm=warm)
    assert text == "DO: home" and info["warm"] is False and len(spawned) == 3  # dead, fresh retry, next waiting


def test_prewarm_limits_are_guarded(adapter, tmp_path, monkeypatch):
    import importlib.util
    warm = adapter.Warm(start=lambda cmd: FakeProc(cmd), most=0, max_age=300, clock=lambda: 0.0)
    warm.refill(["a"])
    warm.refill(["b"])
    assert warm.size() == 1  # most=0 means one, not a crash
    token = tmp_path / "token"
    token.write_text("t\n")
    monkeypatch.setenv("ADAPTER_TOKEN_FILE", str(token))
    for env, on, most in (({"ADAPTER_PREWARM_MAX_AGE": "0"}, False, 4),
                          ({"ADAPTER_PREWARM_MAX": "lots", "ADAPTER_PREWARM_MAX_AGE": "x"}, True, 4),
                          ({"ADAPTER_PREWARM_MAX": "2"}, True, 2)):
        for k in ("ADAPTER_PREWARM", "ADAPTER_PREWARM_MAX", "ADAPTER_PREWARM_MAX_AGE"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        spec = importlib.util.spec_from_file_location("claude_adapter_prewarm_limits", ADAPTER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert (mod.WARM is not None) is on and mod.PREWARM_MAX == most
