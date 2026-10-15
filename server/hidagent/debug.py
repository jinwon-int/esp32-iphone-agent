"""Debug traces: one folder per agent run with every model call and the screenshot it saw.

The screenshots are of the user's phone (messages, contacts, whatever was on screen), so folders are 0700 and files
0600 regardless of the process umask; a traces folder left looser by an older version is tightened on the next run.
"""

import json
import os
import time
from pathlib import Path


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)  # mkdir's mode is masked by the umask and ignored for an existing folder


def _private_open(path: Path, flags: int):
    return os.fdopen(os.open(path, flags | os.O_WRONLY | os.O_CREAT, 0o600), "ab" if flags & os.O_APPEND else "wb")


class Trace:
    def __init__(self, root: Path | None):
        self.root, self.dir, self._n = root, None, 0

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def start(self, task: str) -> None:
        if not self.enabled:
            return
        _private_dir(self.root)
        self.dir = self.root / time.strftime("%Y%m%d-%H%M%S")
        _private_dir(self.dir)
        self._n = 0
        self.write(kind="task", task=task)

    def write(self, frame: bytes | None = None, **record) -> None:
        if not self.dir:
            return
        self._n += 1
        if frame:
            record["frame"] = f"{self._n:03d}.jpg"
            with _private_open(self.dir / record["frame"], os.O_TRUNC) as f:
                f.write(frame)
        line = json.dumps({"n": self._n, "t": time.time(), **record}, ensure_ascii=False) + "\n"
        with _private_open(self.dir / "steps.jsonl", os.O_APPEND) as f:
            f.write(line.encode())
