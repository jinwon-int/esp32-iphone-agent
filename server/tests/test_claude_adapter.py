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
    text, usage = adapter.ask("SYS", [{"type": "text", "text": "Goal: x"}], "claude-sonnet-5-5", "low")
    assert text == "THINK: a\nDO: tap 1 2" and usage == {"input_tokens": 12, "output_tokens": 7}
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
