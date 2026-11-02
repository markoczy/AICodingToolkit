import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def private_write_lock(tmp_path_factory, monkeypatch):
    """Every test gets its own write-lock file (never the checkout's .claude/board/log/write.lock); child
    processes find it through TB_WRITE_LOCK."""
    from taskboard import store
    path = str(tmp_path_factory.mktemp("lock") / "write.lock")
    monkeypatch.setattr(store, "LOCK_PATH", path)
    monkeypatch.setenv("TB_WRITE_LOCK", path)
    return path
