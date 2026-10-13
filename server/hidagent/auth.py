"""Shared secrets: the web UI token, the board key and the AirPlay pin.

Nothing here is clever on purpose. Secrets come from the environment or, if unset, are generated once and kept in
the data directory (readable by this user only), so they survive restarts.
"""

import hashlib
import hmac
import logging
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# the placeholder shipped in hardware/hid_bridge/secrets.example.h: never accept it as a real key
EXAMPLE_DEVICE_KEY = "change-me-to-a-long-random-string"
MIN_DEVICE_KEY = 16
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def same(a: str, b: str) -> bool:
    """Constant-time comparison (also safe for non-ASCII input, which hmac.compare_digest rejects for str)."""
    return hmac.compare_digest(a.encode(), b.encode())


def mac(key: bytes, label: str, *parts: str) -> str:
    """HMAC-SHA256 over "label:part:part…", as lowercase hex. The label keeps the two sides' proofs apart."""
    return hmac.new(key, ":".join((label, *parts)).encode(), hashlib.sha256).hexdigest()


def nonce() -> str:
    return secrets.token_hex(16)


def stored(env: str, path: Path, make: Callable[[], str]) -> str:
    """The value of $env if set, else the one kept in `path`, else a new one from make() saved there (mode 0600)."""
    if value := os.environ.get(env, "").strip():
        return value
    try:
        if value := path.read_text().strip():
            return value
    except FileNotFoundError:
        pass
    value = make()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    os.chmod(path, 0o600)  # in case the file already existed with wider permissions
    return value


def ui_token(data_dir: Path) -> str:
    return stored("UI_TOKEN", data_dir / "ui_token", lambda: secrets.token_urlsafe(24))


def device_key() -> bytes | None:
    """DEVICE_KEY from the environment, or None (with the reason logged) when it is missing or unsafe."""
    key = os.environ.get("DEVICE_KEY", "").strip()
    if not key:
        log.error("DEVICE_KEY is not set: the board will be refused. Put the same value in server/.env and "
                  "hardware/hid_bridge/secrets.h, e.g. from: "
                  "python3 -c 'import secrets; print(secrets.token_hex(16))'")
        return None
    if key == EXAMPLE_DEVICE_KEY or len(key) < MIN_DEVICE_KEY:
        log.error("DEVICE_KEY is the example value or shorter than %d characters: the board will be refused",
                  MIN_DEVICE_KEY)
        return None
    return key.encode()


def airplay_pin(data_dir: Path) -> str | None:
    """A fixed 4-digit AirPlay pin, or None when AIRPLAY_PIN=off."""
    if os.environ.get("AIRPLAY_PIN", "").strip().lower() == "off":
        return None
    pin = stored("AIRPLAY_PIN", data_dir / "airplay_pin", lambda: f"{secrets.randbelow(10000):04d}")
    if not re.fullmatch(r"\d{4}", pin):
        raise ValueError("AIRPLAY_PIN must be 4 digits (or 'off')")
    return pin
