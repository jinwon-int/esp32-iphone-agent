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
import stat
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# the placeholder shipped in hardware/hid_bridge/secrets.example.h: never accept it as a real key
EXAMPLE_DEVICE_KEY = "change-me-to-a-long-random-string"
MIN_DEVICE_KEY = 16
MIN_UI_TOKEN = 16
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def redact_query(text: str) -> str:
    """Replace every URL query string (?…) with ?<redacted>."""
    return re.sub(r"\?[^\s\"]+", "?<redacted>", text)


def same(a: str, b: str) -> bool:
    """Constant-time comparison (also safe for non-ASCII input, which hmac.compare_digest rejects for str)."""
    return hmac.compare_digest(a.encode(), b.encode())


def mac(key: bytes, label: str, *parts: str) -> str:
    """HMAC-SHA256 over "label:part:part…", as lowercase hex. The label keeps the two sides' proofs apart."""
    return hmac.new(key, ":".join((label, *parts)).encode(), hashlib.sha256).hexdigest()


def tag(session: bytes, direction: str, seq: int, line: str) -> str:
    """Per-message tag: HMAC(session, "<direction>:<seq>:<line>"), 128 bits as hex."""
    return mac(session, direction, str(seq), line)[:32]


def nonce() -> str:
    return secrets.token_hex(16)


def private_dir(path: Path) -> Path:
    """Create the data directory readable by this user only (and tighten an existing one we own).

    Refuses a directory owned by someone else or writable by others: files in it (secrets, calibration, the AirPlay
    register) could be swapped under us.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid():
        raise PermissionError(f"{path} belongs to another user: set HIDAGENT_DATA to a directory of your own")
    path.chmod(0o700)
    return path


def _read_private(path: Path) -> str:
    """The file's text, after making sure it is a regular file only we can read. "" if it does not exist."""
    try:
        # O_NOFOLLOW: a planted symlink fails here (ELOOP); O_NONBLOCK: a planted FIFO cannot hang us
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"{path} is not a regular file")
        os.fchmod(fd, 0o600)
        f = os.fdopen(fd)
    except BaseException:
        os.close(fd)
        raise
    with f:
        return f.read().strip()


def _write_private(path: Path, value: str) -> None:
    """Write to a new 0600 temp file (never through a symlink, never briefly readable), then move it in place."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def stored(env: str, path: Path, make: Callable[[], str], valid: Callable[[str], bool] = bool) -> str:
    """$env if set, else the value kept in `path`, else a new one from make() saved there (mode 0600).

    A kept value that fails valid() (e.g. emptied or too short) is replaced by a new one; $env is returned as is.
    """
    if value := os.environ.get(env, "").strip():
        return value
    if (value := _read_private(path)) and valid(value):
        return value
    value = make()
    _write_private(path, value)
    return value


def ui_token(data_dir: Path) -> str:
    token = stored("UI_TOKEN", data_dir / "ui_token", lambda: secrets.token_urlsafe(24),
                   lambda t: len(t) >= MIN_UI_TOKEN)
    if len(token) < MIN_UI_TOKEN:
        raise ValueError(f"UI_TOKEN must be at least {MIN_UI_TOKEN} characters")
    return token


def device_key() -> bytes | None:
    """DEVICE_KEY from the environment, or None (with the reason logged) when it is missing or unsafe."""
    key = os.environ.get("DEVICE_KEY", "").strip()
    if not key:
        log.error("DEVICE_KEY is not set: the board will be refused. Put the same value in server/.env and "
                  "hardware/hid_bridge/secrets.h, e.g. from: "
                  "python3 -c 'import secrets; print(secrets.token_hex(16))'")
        return None
    if key == EXAMPLE_DEVICE_KEY or len(key) < MIN_DEVICE_KEY or len(set(key)) < 6:
        log.error("DEVICE_KEY is the example value, shorter than %d characters or too repetitive: "
                  "the board will be refused", MIN_DEVICE_KEY)
        return None
    return key.encode()


def airplay_pin(data_dir: Path) -> str | None:
    """A fixed 4-digit AirPlay pin, or None when AIRPLAY_PIN=off."""
    if os.environ.get("AIRPLAY_PIN", "").strip().lower() == "off":
        return None
    pin = stored("AIRPLAY_PIN", data_dir / "airplay_pin", lambda: f"{secrets.randbelow(10000):04d}",
                 lambda p: re.fullmatch(r"\d{4}", p) is not None)
    if not re.fullmatch(r"\d{4}", pin):
        raise ValueError("AIRPLAY_PIN must be 4 digits (or 'off')")
    return pin
