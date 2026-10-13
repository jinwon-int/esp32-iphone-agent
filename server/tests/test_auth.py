import importlib
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from hidagent import auth
from hidagent.screen import Screen

TOKEN = "test-token-1234567890"


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HIDAGENT_DATA", str(tmp_path_factory.mktemp("data")))
        mp.setenv("UI_TOKEN", TOKEN)
        mp.setenv("DEVICE_KEY", "k" * 32)
        mp.setenv("AIRPLAY_PIN", "4321")
        sys.modules.pop("hidagent.app", None)
        yield importlib.import_module("hidagent.app").app  # no lifespan: UxPlay and the board port stay off
        sys.modules.pop("hidagent.app", None)


def client(app, **kw):
    return TestClient(app, follow_redirects=False, **kw)


@pytest.mark.parametrize("method,path", [("get", "/"), ("get", "/api/status"), ("get", "/api/frame.jpg"),
                                         ("post", "/api/home"), ("post", "/api/agent/stop")])
def test_everything_but_static_files_needs_the_token(app, method, path):
    r = getattr(client(app), method)(path)
    assert r.status_code == 401


def test_static_files_are_public(app):
    assert client(app).get("/static/app.js").status_code == 200


def test_wrong_token_is_refused(app):
    c = client(app)
    assert c.get("/", params={"token": "nope"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/", params={"token": "ünïcode"}).status_code == 401  # non-ASCII: no TypeError from compare_digest
    c.cookies.set("hidagent_token", "nope")
    assert c.get("/api/status").status_code == 401


def test_token_link_sets_a_strict_cookie_and_cleans_the_address(app):
    c = client(app)
    r = c.get("/", params={"token": TOKEN})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/").status_code == 200
    assert c.get("/api/status").json()["device"] is False


def test_bearer_header_works_for_scripts(app):
    r = client(app).get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_token_in_query_is_only_accepted_on_the_page(app):
    assert client(app).get("/api/status", params={"token": TOKEN}).status_code == 401


def test_stored_secret_is_created_once_private_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("UI_TOKEN", raising=False)
    first = auth.ui_token(tmp_path)
    assert len(first) >= 32 and auth.ui_token(tmp_path) == first
    assert stat.S_IMODE((tmp_path / "ui_token").stat().st_mode) == 0o600
    monkeypatch.setenv("UI_TOKEN", "from-env-0123456789")
    assert auth.ui_token(tmp_path) == "from-env-0123456789"


def test_stored_secret_tightens_existing_file_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("AIRPLAY_PIN", raising=False)
    path = tmp_path / "airplay_pin"
    path.write_text("")
    path.chmod(0o644)
    pin = auth.airplay_pin(tmp_path)
    assert len(pin) == 4 and pin.isdigit()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("value,expected", [("off", None), ("OFF", None), ("0042", "0042")])
def test_airplay_pin_from_env(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("AIRPLAY_PIN", value)
    assert auth.airplay_pin(tmp_path) == expected


@pytest.mark.parametrize("value", ["123", "12345", "abcd"])
def test_airplay_pin_must_be_four_digits(tmp_path, monkeypatch, value):
    monkeypatch.setenv("AIRPLAY_PIN", value)
    with pytest.raises(ValueError):
        auth.airplay_pin(tmp_path)


@pytest.mark.parametrize("value", ["", "short", auth.EXAMPLE_DEVICE_KEY, "a" * 32, "abab" * 8])
def test_unsafe_device_keys_are_rejected(monkeypatch, value):
    monkeypatch.setenv("DEVICE_KEY", value)
    assert auth.device_key() is None


def test_device_key_is_accepted(monkeypatch):
    monkeypatch.setenv("DEVICE_KEY", " 0123456789abcdef ")
    assert auth.device_key() == b"0123456789abcdef"


def test_mac_separates_the_two_sides():
    assert auth.mac(b"k", "server", "a", "b") != auth.mac(b"k", "board", "a", "b")
    assert auth.mac(b"k", "board", "a", "b") != auth.mac(b"k", "board", "b", "a")
    assert len(auth.nonce()) == 32 and auth.NONCE_RE.match(auth.nonce())


def test_uxplay_gets_pin_and_register(tmp_path):
    args = Screen("Agent", pin="4321", register=tmp_path / "reg").command(9)
    assert args[args.index("-pin") + 1] == "4321"
    assert args[args.index("-reg") + 1] == str(tmp_path / "reg")
    assert "fdsink fd=9" in args[args.index("-vs") + 1]


def test_uxplay_without_pin_has_no_pin_flags():
    args = Screen("Agent").command(9)
    assert "-pin" not in args and "-reg" not in args


# --- regressions from the adversarial review ---------------------------------------------------------------------


def test_existing_secret_file_is_tightened_to_0600(tmp_path, monkeypatch):
    """Review L3: a non-empty 0644 token file used to be reused as is."""
    monkeypatch.delenv("UI_TOKEN", raising=False)
    path = tmp_path / "ui_token"
    path.write_text("k" * 32)
    path.chmod(0o644)
    assert auth.ui_token(tmp_path) == "k" * 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secret_is_never_written_through_a_symlink(tmp_path, monkeypatch):
    """Review L3: a dangling symlink planted at the token path used to receive the new secret."""
    monkeypatch.delenv("UI_TOKEN", raising=False)
    victim = tmp_path / "elsewhere"
    (tmp_path / "ui_token").symlink_to(victim)
    with pytest.raises(OSError):
        auth.ui_token(tmp_path)
    assert not victim.exists()


def test_new_secret_file_is_never_readable_by_others(tmp_path, monkeypatch):
    """Review L3: the secret is written to a fresh 0600 file and moved in place (no write-then-chmod window)."""
    monkeypatch.delenv("UI_TOKEN", raising=False)
    modes = []
    real_replace = auth.os.replace

    def spy(src, dst):
        modes.append(stat.S_IMODE(auth.os.stat(src).st_mode))
        real_replace(src, dst)

    monkeypatch.setattr(auth.os, "replace", spy)
    auth.ui_token(tmp_path)
    assert modes == [0o600]
    assert [p.name for p in tmp_path.iterdir()] == ["ui_token"]  # no temp file left behind


def test_data_dir_is_private(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    auth.private_dir(data)
    assert stat.S_IMODE(data.stat().st_mode) == 0o700


def test_weak_ui_token_is_refused_from_env_and_replaced_in_file(tmp_path, monkeypatch):
    """Review L4."""
    monkeypatch.setenv("UI_TOKEN", "a")
    with pytest.raises(ValueError):
        auth.ui_token(tmp_path)
    monkeypatch.delenv("UI_TOKEN")
    (tmp_path / "ui_token").write_text("1")
    assert len(auth.ui_token(tmp_path)) >= auth.MIN_UI_TOKEN


@pytest.mark.parametrize("query", [f"token={TOKEN}", f"%74oken={TOKEN}", f"a=1&token={TOKEN}"])
def test_token_is_redacted_from_access_log_lines(query):
    """Review L5 (+ re-review: a percent-encoded name used to slip through)."""
    import logging

    from hidagent.app import _RedactToken

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:1", "GET", f"/?{query}", "1.1", 303), None)
    _RedactToken().filter(record)
    assert TOKEN not in record.getMessage() and "/?<redacted>" in record.getMessage()


def test_stale_cookie_does_not_block_a_good_bearer_header(app):
    """Review L6."""
    c = client(app)
    c.cookies.set("hidagent_token", "stale")
    assert c.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


@pytest.mark.parametrize("headers", [{"Origin": "http://testserver:9999"}, {"Origin": "null"},
                                     {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"}])
def test_cross_origin_changes_are_refused_even_with_the_cookie(app, headers):
    """Review L7: a page on another port of this host shares the SameSite cookie, so check the origin too."""
    c = client(app)
    c.get("/", params={"token": TOKEN})
    assert c.post("/api/agent/stop", headers=headers).status_code == 403


def test_same_origin_with_mixed_case_host_is_accepted(app):
    c = client(app, base_url="http://Hidagent.LOCAL:8890")
    c.get("/", params={"token": TOKEN})
    r = c.post("/api/agent/stop", headers={"Origin": "http://hidagent.local:8890"})
    assert r.status_code == 200


def test_same_origin_changes_are_accepted(app):
    c = client(app)
    c.get("/", params={"token": TOKEN})
    r = c.post("/api/agent/stop", headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


# --- regressions from the re-review ------------------------------------------------------------------------------


def test_fifo_in_place_of_a_secret_does_not_hang(tmp_path, monkeypatch):
    """Re-review N3: opening a planted FIFO used to block forever."""
    import os
    import signal

    monkeypatch.delenv("UI_TOKEN", raising=False)
    os.mkfifo(tmp_path / "ui_token")
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("hung on the FIFO")))
    signal.alarm(3)
    try:
        with pytest.raises(ValueError, match="not a regular file"):
            auth.ui_token(tmp_path)
    finally:
        signal.alarm(0)


def test_directory_in_place_of_a_secret_fails_without_leaking_a_descriptor(tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("UI_TOKEN", raising=False)
    (tmp_path / "ui_token").mkdir()
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ValueError, match="not a regular file"):
        auth.ui_token(tmp_path)
    assert len(os.listdir("/proc/self/fd")) == before


def test_data_dir_of_another_user_is_refused(tmp_path, monkeypatch):
    """Re-review N4: a directory owned by someone else (maybe 0777) used to be used as is."""
    data = tmp_path / "shared"
    data.mkdir()
    data.chmod(0o777)
    monkeypatch.setattr(auth.os, "getuid", lambda: data.stat().st_uid + 1)
    with pytest.raises(PermissionError, match="another user"):
        auth.private_dir(data)
