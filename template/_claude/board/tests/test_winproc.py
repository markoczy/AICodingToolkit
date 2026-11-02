"""Tests for the Windows console-window flags (taskboard/winproc.py) and where they are passed: every git and svn
child of the backends (taskboard/vcs) and the board the watcher starts."""
import contextlib
import os
import subprocess

import pytest

from taskboard import store, watch, winproc
from taskboard.vcs import git as gitvcs
from taskboard.vcs import svn as svnvcs

NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200


def fake_os(monkeypatch, name):
    monkeypatch.setattr(os, "name", name)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", NO_WINDOW, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", NEW_GROUP, raising=False)


def capture_run(monkeypatch):
    calls = []

    def fake(cmd, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    return calls


@pytest.mark.parametrize("name,expected", [("nt", NO_WINDOW), ("posix", 0)])
def test_no_window_flags(monkeypatch, name, expected):
    fake_os(monkeypatch, name)
    assert winproc.no_window_flags() == expected


@pytest.mark.parametrize("name,expected", [("nt", NO_WINDOW), ("posix", 0)])
def test_git_calls_pass_flags(monkeypatch, name, expected):
    fake_os(monkeypatch, name)
    calls = capture_run(monkeypatch)
    gitvcs._run({"repo_root_path": "."}, ["status"])
    gitvcs.git_branch(".")
    gitvcs.has_tracked_files(".")
    gitvcs._versioned("x")
    assert len(calls) == 4
    assert all(c.get("creationflags", 0) == expected for c in calls)
    assert all(c["stdin"] == subprocess.DEVNULL and c["env"]["GIT_TERMINAL_PROMPT"] == "0" for c in calls)


@pytest.mark.parametrize("name,expected", [("nt", NO_WINDOW), ("posix", 0)])
def test_svn_calls_pass_flags(monkeypatch, name, expected):
    fake_os(monkeypatch, name)
    calls = capture_run(monkeypatch)
    svnvcs._run({"repo_root_path": "."}, ["info"])
    svnvcs.svn_location(".")
    svnvcs._versioned("x")
    assert len(calls) == 3
    assert all(c.get("creationflags", 0) == expected for c in calls)
    assert all(c["stdin"] == subprocess.DEVNULL for c in calls)


@pytest.mark.parametrize("vcs", ["git", "svn"])
@pytest.mark.parametrize("name,expected", [("nt", NO_WINDOW), ("posix", 0)])
def test_versioned_move_passes_flags(monkeypatch, tmp_path, name, expected, vcs):
    src, dst = tmp_path / "a.md", tmp_path / "b.md"
    src.write_text("x")
    fake_os(monkeypatch, name)
    calls = capture_run(monkeypatch)
    monkeypatch.setattr(store, "is_versioned", lambda p, vcs="git": True)
    monkeypatch.setattr(store, "task_write_lock", contextlib.nullcontext)  # the OS lock depends on the real os.name
    assert store.move_file(str(tmp_path), str(src), str(dst), vcs) == f"{vcs} mv"
    assert calls[-1].get("creationflags", 0) == expected


@pytest.mark.parametrize("name,expected", [("nt", NO_WINDOW), ("posix", 0)])
def test_watcher_git_calls_pass_flags(monkeypatch, tmp_path, name, expected):
    fake_os(monkeypatch, name)
    calls = capture_run(monkeypatch)
    monkeypatch.setattr(gitvcs, "_GIT_DIRS", {})
    gitvcs.git_dir({"repo_root_path": str(tmp_path)})
    gitvcs.own_commits_only({"repo_root_path": str(tmp_path)}, "a", "b")
    assert len(calls) == 2 and all(c.get("creationflags", 0) == expected for c in calls)


@pytest.mark.parametrize("name", ["nt", "posix"])
def test_start_board_flags(monkeypatch, tmp_path, name):
    fake_os(monkeypatch, name)
    monkeypatch.setattr(watch, "BOARD_DIR", str(tmp_path))
    monkeypatch.setattr(watch, "board_state", lambda c: "ours")
    popen = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: popen.append(kw))
    watch.start_board({"repo_root_path": str(tmp_path), "config_path": "c.json"})
    kw = popen[0]
    if name == "nt":
        assert kw["creationflags"] == NO_WINDOW | NEW_GROUP
        assert "start_new_session" not in kw
    else:
        assert kw["start_new_session"] is True
        assert "creationflags" not in kw
