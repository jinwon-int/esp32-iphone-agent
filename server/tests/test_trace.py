"""Debug traces hold screenshots of the phone: owner-only on disk whatever the process umask."""

import os
import stat

import pytest

from hidagent.debug import Trace


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)  # the usual default: would give 0755 folders and 0644 files
    yield
    os.umask(old)


def test_trace_folders_and_files_are_owner_only(tmp_path, loose_umask):
    trace = Trace(tmp_path / "traces")
    trace.start("open Settings")
    trace.write(b"\xff\xd8jpeg\xff\xd9", kind="llm", role="actor")
    trace.write(kind="step", action="tap")
    assert mode(tmp_path / "traces") == 0o700
    assert mode(trace.dir) == 0o700
    files = sorted(trace.dir.iterdir())
    assert [f.name for f in files] == ["002.jpg", "steps.jsonl"]
    assert all(mode(f) == 0o600 for f in files)


def test_an_existing_loose_traces_folder_is_tightened(tmp_path, loose_umask):
    root = tmp_path / "traces"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)  # left behind by an older version
    Trace(root).start("t")
    assert mode(root) == 0o700


def test_disabled_trace_writes_nothing(tmp_path):
    trace = Trace(None)
    trace.start("t")
    trace.write(b"\xff\xd8\xff\xd9", kind="llm")
    assert list(tmp_path.iterdir()) == []
