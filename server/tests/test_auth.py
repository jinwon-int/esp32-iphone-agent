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
    monkeypatch.setenv("UI_TOKEN", "from-env")
    assert auth.ui_token(tmp_path) == "from-env"


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


@pytest.mark.parametrize("value", ["", "short", auth.EXAMPLE_DEVICE_KEY])
def test_unsafe_device_keys_are_rejected(monkeypatch, value):
    monkeypatch.setenv("DEVICE_KEY", value)
    assert auth.device_key() is None


def test_device_key_is_accepted(monkeypatch):
    monkeypatch.setenv("DEVICE_KEY", " " + "a" * 32 + " ")
    assert auth.device_key() == b"a" * 32


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
