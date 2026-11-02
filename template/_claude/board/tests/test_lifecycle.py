"""
Tests for the lifecycle of the watcher and its board: the session check (session.py, `tb watch` exit 4), the idle
exit of a watcher-started board (idle.py, `tb serve --idle-exit`) and the board identity (`/api/fingerprint`
"board", watch.board_state). The real-process tests start their own processes on free ports (never 5070) and stop
only those, by pid.
"""
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import signal

import pytest

from taskboard import cli, idle, session, watch
from taskboard.config import BoardConfigError, board_id, load_board_config, validate_board_config
from taskboard.model import STATES
from taskboard.web.app import create_app

BOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TB = os.path.join(BOARD_DIR, "tb.py")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def make_config(tmp_path, port=None, **watch_keys):
    (tmp_path / ".claude" / "board").mkdir(parents=True, exist_ok=True)
    for state in STATES:
        (tmp_path / "AI" / "Tasks" / state).mkdir(parents=True, exist_ok=True)
    data = {"watch": {"poll_seconds": 1, "quiet_seconds": 1, "update_seconds": 0,
                                              **watch_keys},
            "sources": [{"name": "tasks", "root": "AI/Tasks", "prefix": "T", "editable": True}]}
    if port:
        data["port"] = port
    path = tmp_path / ".claude" / "board" / "board.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_board_config(str(path))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 5070
    return port


def running(pid: int) -> bool:
    return session.alive(session.Ancestor(pid, None, "test"))


def wait_until(condition, timeout: float, step: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(step)
    return condition()


def kill_pid(pid: int) -> None:
    """Stop one process this test started (by pid only)."""
    try:
        os.kill(pid, signal.SIGTERM)  # Windows: TerminateProcess (signal 1 would be CTRL_BREAK_EVENT)
    except OSError:
        pass
    assert wait_until(lambda: not running(pid), 10), f"pid {pid} still runs"


def exit_code(handle) -> int:
    """The exit code of a process that is not our child, through a handle opened while it ran (Windows)."""
    import ctypes
    from ctypes import wintypes
    code = wintypes.DWORD()
    session._kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    return code.value


# ---- session.py ----

class TestSession:
    def test_ancestors_of_the_test_process(self):
        chain = session.ancestors()
        assert chain, "the parent chain could not be determined"
        assert chain[0].pid == os.getppid()
        assert len(chain) <= session.MAX_LEVELS
        if os.name == "nt":
            assert all(entry.created for entry in chain)
            # Every entry but the last is a shell or launcher; the walk ends at the first that is not one.
            assert all(session.is_launcher(entry.image) for entry in chain[:-1])
        assert session.SessionGuard(chain).gone() is None

    def test_alive_parent_finished_child_and_reused_pid(self):
        parent = session.ancestors()[0]
        assert session.alive(parent)
        child = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=NO_WINDOW)
        created = None
        if os.name == "nt":
            created = session._windows_created(child.pid)
        child.wait()
        assert not session.alive(session.Ancestor(child.pid, created, "python"))
        if parent.created is not None:
            # The same pid with another creation time is another process (pid reuse): gone.
            assert not session.alive(parent._replace(created=parent.created + 1))

    def test_walk_stops_at_the_first_non_launcher_and_at_max_levels(self):
        table = {10: (9, "python.exe", 50), 9: (8, "bash.exe", 40), 8: (7, "claude.exe", 30),
                 7: (1, "explorer.exe", 20)}
        chain = session.walk(10, lambda p: table[p][0], lambda p: table[p][2],
                             lambda p: table[p][1] if p in table else None)
        assert [(e.pid, e.image) for e in chain] == [(10, "python"), (9, "bash"), (8, "claude")]
        deep = {i: (i - 1, "bash", i) for i in range(2, 20)}
        chain = session.walk(19, lambda p: deep[p][0], lambda p: deep[p][2], lambda p: deep[p][1])
        assert len(chain) == session.MAX_LEVELS

    def test_walk_stops_at_a_parent_created_after_its_child(self):
        """The recorded parent pid was reused by a newer process: it is not our parent, the walk stops."""
        table = {10: (9, "bash", 50), 9: (8, "notepad", 60)}
        chain = session.walk(10, lambda p: table[p][0], lambda p: table[p][2], lambda p: table[p][1])
        assert [e.pid for e in chain] == [10]

    def test_launcher_names(self):
        for name in ["bash.exe", "sh", "pwsh.exe", "powershell.exe", "cmd.exe", "python3.12", "pythonw.exe", "py",
                     "C:\\Program Files\\Git\\usr\\bin\\bash.exe", "env", "timeout", "winpty"]:
            assert session.is_launcher(name), name
        for name in ["claude.exe", "node", "WindowsTerminal.exe", "explorer.exe", "systemd"]:
            assert not session.is_launcher(name), name

    def test_posix_parent_change_and_no_proc(self):
        entry = session.Ancestor(4242, None, "bash")
        assert not session._posix_alive(entry, getppid=lambda: 1, first_pid=4242)  # re-parented: gone
        assert session._posix_ancestors(getppid=lambda: 4242, proc_stat=lambda pid: None) == [
            session.Ancestor(4242, None, "parent")]
        assert session._posix_ancestors(getppid=lambda: 1, proc_stat=lambda pid: None) == []
        stats = {30: (20, "python3", 300), 20: (10, "bash", 200), 10: (1, "sshd", 100)}
        chain = session._posix_ancestors(getppid=lambda: 30, proc_stat=stats.get)
        assert [(e.pid, e.created, e.image) for e in chain] == [(30, 300, "python3"), (20, 200, "bash"),
                                                                 (10, 100, "sshd")]

    def test_guard_reports_the_first_ended_entry(self):
        chain = [session.Ancestor(1, 1, "bash"), session.Ancestor(2, 2, "claude")]
        ended = set()
        guard = session.SessionGuard(chain, check=lambda entry, first: entry.pid not in ended)
        assert guard.gone() is None
        ended.add(2)
        assert guard.gone() == chain[1]
        assert session.gone_message(chain[1]) == "SESSION GONE: claude (pid 2) ended; the watcher stops"


# ---- watch() with a session guard ----

class Guard:
    def __init__(self, fire_at: int):
        self.calls = 0
        self.fire_at = fire_at

    def gone(self):
        self.calls += 1
        return session.Ancestor(77, 1, "bash") if self.calls >= self.fire_at else None


def fake_sleep(monkeypatch, limit=100):
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) >= limit:
            raise KeyboardInterrupt
    monkeypatch.setattr(watch.time, "sleep", sleep)
    return calls


class TestWatchGuard:
    def test_exit_4_in_the_poll_loop_and_the_board_is_not_restarted(self, tmp_path, monkeypatch, capsys):
        config = make_config(tmp_path)
        fake_sleep(monkeypatch)
        started = []
        monkeypatch.setattr(watch, "board_state", lambda c: "down")  # the board ended meanwhile
        monkeypatch.setattr(watch, "start_board", lambda c, no_update=False, commits=False: started.append(1))
        assert watch.watch(config, update=False, guard=Guard(fire_at=4)) == watch.EXIT_SESSION_GONE
        assert len(started) == 3  # at start and after polls 1 and 2; none after the guard fired at poll 3
        assert "SESSION GONE: bash (pid 77) ended; the watcher stops" in capsys.readouterr().out

    def test_exit_4_at_start_touches_no_board(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        fake_sleep(monkeypatch)
        monkeypatch.setattr(watch, "board_state", lambda c: pytest.fail("no board check expected"))
        assert watch.watch(config, update=False, guard=Guard(fire_at=1)) == 4

    def test_exit_4_in_the_quiet_period(self, tmp_path, monkeypatch, capsys):
        config = make_config(tmp_path)
        task = tmp_path / "AI" / "Tasks" / "01-Refinement" / "T001-x.md"
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:  # someone answers on the board: the quiet period starts
                task.write_text(TASK + "- 2026-10-26 Alex (board): answered Q1.\n", encoding="utf-8")
            if len(sleeps) > 50:
                raise KeyboardInterrupt
        task.write_text(TASK, encoding="utf-8")
        config["watch"]["quiet_seconds"] = 30
        monkeypatch.setattr(watch.time, "sleep", sleep)
        guard = Guard(fire_at=5)
        assert watch.watch(config, start_server=False, update=False, guard=guard) == 4
        out = capsys.readouterr().out
        assert "SESSION GONE" in out and "BOARD INPUT" not in out
        assert len(sleeps) == 4  # poll 1, poll 2 (input), two quiet-period polls

    def test_no_guard_is_todays_watcher(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        fake_sleep(monkeypatch, limit=10)
        assert watch.watch(config, start_server=False, update=False) == 130


TASK = ("---\nid: T001\ntitle: X\ntype: bug\npriority: low\nowner: null\ncreated: 2026-10-26\nupdated: 2026-10-26\n"
        "waiting_on: null\nblocked_by: []\nplan: null\nbranch: null\npr: null\n---\n\n## Problem\nP\n\n"
        "## Approach\n\n## Acceptance\n\n## Questions\n\n## Decisions\n\n## Log\n- 2026-10-26 supervisor: created.\n")


class TestWatchCli:
    def test_watch_builds_a_guard_unless_no_parent_check(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)
        seen = []
        monkeypatch.setattr(watch, "watch", lambda c, **kw: seen.append(kw["guard"]) or 0)
        path = config["config_path"]
        assert cli.main(["--config", path, "watch", "--no-server"]) == 0
        assert isinstance(seen[-1], session.SessionGuard) and seen[-1].chain
        assert cli.main(["--config", path, "watch", "--no-server", "--no-parent-check"]) == 0
        assert seen[-1] is None
        monkeypatch.setattr(session, "ancestors", lambda: [])
        assert cli.main(["--config", path, "watch", "--no-server"]) == 0
        assert seen[-1] is None


def start_intermediate(config, extra, out_path):
    """A python that starts `tb watch --no-server --no-update` (output to out_path), prints its pid and waits."""
    script = ("import subprocess, sys, time\n"
              "out = open(sys.argv[1], 'wb')\n"
              "p = subprocess.Popen([sys.executable, *sys.argv[2:]], stdout=out, stderr=out, "
              "stdin=subprocess.DEVNULL)\n"
              "print(p.pid, flush=True)\n"
              "time.sleep(600)\n")
    command = [sys.executable, "-c", script, str(out_path), TB, "--config", config["config_path"], "watch",
               "--no-server", "--no-update", *extra]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
    return process, int(process.stdout.readline())


class TestRealProcessTree:
    def test_watcher_exits_4_when_its_parent_ends(self, tmp_path):
        config = make_config(tmp_path)
        out = tmp_path / "watch.out"
        twin_out = tmp_path / "twin.out"
        middle, watcher = start_intermediate(config, [], out)
        twin_middle, twin = start_intermediate(config, ["--no-parent-check"], twin_out)
        handle = session._open(watcher)[0] if os.name == "nt" else None
        try:
            assert wait_until(lambda: "watching" in out.read_text(errors="replace"), 15)
            assert wait_until(lambda: "watching" in twin_out.read_text(errors="replace"), 15)
            middle.kill()
            middle.wait()
            twin_middle.kill()
            twin_middle.wait()
            start = time.monotonic()
            assert wait_until(lambda: not running(watcher), 15), out.read_text(errors="replace")
            assert time.monotonic() - start < 15
            text = out.read_text(errors="replace")
            assert "SESSION GONE:" in text and "the watcher stops" in text
            if handle:
                assert exit_code(handle) == 4
            time.sleep(3)  # the twin without the parent check keeps running
            assert running(twin)
            assert "SESSION GONE" not in twin_out.read_text(errors="replace")
        finally:
            for pid in (watcher, twin):
                if running(pid):
                    kill_pid(pid)
            for p in (middle, twin_middle):
                if p.poll() is None:
                    p.kill()
            for p in (middle, twin_middle):
                p.stdout.close()
            assert not any(running(pid) for pid in (watcher, twin, middle.pid, twin_middle.pid))
            if handle:
                session._kernel32.CloseHandle(handle)


# ---- idle exit ----

class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestIdle:
    def test_idle_loop_stops_after_the_limit(self):
        clock = FakeClock()
        activity = idle.Activity(clock)
        stopped, lines = [], []

        def sleep(seconds):
            clock.now += seconds
            if clock.now > 1000:
                raise AssertionError("never stopped")
        idle.idle_loop(activity, 60, threading.RLock(), lambda: stopped.append(clock.now), sleep=sleep,
                       out=lambda text, flush=False: lines.append(text))
        assert stopped and 60 <= stopped[0] <= 70
        assert lines[0].endswith("idle for 1 min; the board stops")

    def test_requests_keep_it_alive(self):
        clock = FakeClock()
        activity = idle.Activity(clock)
        stopped = []

        def sleep(seconds):
            clock.now += seconds
            if clock.now < 300:
                activity.touch()  # a watcher poll or an open page, every step
            if clock.now > 1000:
                raise AssertionError("never stopped")
        idle.idle_loop(activity, 60, threading.RLock(), lambda: stopped.append(clock.now), sleep=sleep,
                       out=lambda *a, **k: None)
        assert 350 <= stopped[0] <= 360  # 60 s after the last request (at 290 s)

    def test_a_write_in_progress_delays_the_stop(self):
        activity = idle.Activity()
        lock = threading.RLock()
        stopped = threading.Event()
        lock.acquire()  # a write (or an update) holds the board's write lock
        try:
            thread = idle.start_idle_thread(activity, 0.2, lock, stopped.set)
            time.sleep(1.0)
            assert not stopped.is_set()
        finally:
            lock.release()
        assert stopped.wait(5)
        thread.join(5)

    def test_idle_limit_seconds(self, monkeypatch):
        monkeypatch.delenv(idle.IDLE_SECONDS_ENV, raising=False)
        assert idle.idle_limit_seconds(15) == 900 and idle.idle_limit_seconds(0) == 0
        monkeypatch.setenv(idle.IDLE_SECONDS_ENV, "2")
        assert idle.idle_limit_seconds(15) == 2 and idle.idle_limit_seconds(0) == 0

    def test_the_app_notes_accepted_requests_only(self, tmp_path):
        config = make_config(tmp_path)
        clock = FakeClock()
        activity = idle.Activity(clock)
        client = create_app(config, activity=activity).test_client()
        clock.now = 100
        client.get("/api/fingerprint", base_url="http://evil.example:5070")  # refused by the host check
        assert activity.idle_seconds() == 100
        client.get("/api/fingerprint", base_url=f"http://127.0.0.1:{config['port']}")
        assert activity.idle_seconds() == 0


def start_serve(config, port, extra, env_seconds, log_path):
    env = {**os.environ, idle.IDLE_SECONDS_ENV: str(env_seconds)}
    log = open(log_path, "wb")
    try:
        return subprocess.Popen([sys.executable, TB, "--config", config["config_path"], "serve", "--no-update",
                                 "--port", str(port), *extra], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                env=env, creationflags=NO_WINDOW)
    finally:
        log.close()


def fetch(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fingerprint", timeout=3) as response:
        return json.load(response)


def up(port) -> bool:
    try:
        fetch(port)
        return True
    except (urllib.error.URLError, OSError):
        return False


class TestRealIdleExit:
    def test_idle_board_exits_0_and_a_polled_one_stays(self, tmp_path):
        config = make_config(tmp_path)
        port = free_port()
        log_path = tmp_path / "server.log"
        server = start_serve(config, port, ["--idle-exit", "1"], 3, log_path)
        try:
            assert wait_until(lambda: up(port), 15)
            for _ in range(12):  # 6 s of polls, twice the idle limit: still up
                assert server.poll() is None
                fetch(port)
                time.sleep(0.5)
            assert server.poll() is None
            server.wait(20)  # no more requests: it stops on its own
            assert server.returncode == 0
            log = log_path.read_text(errors="replace")
            assert "stops after 3 s without a request" in log  # the effective limit (TB_IDLE_SECONDS)
            assert "idle for 3 s; the board stops" in log
            assert not up(port)
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()

    def test_manual_serve_without_the_flag_does_not_exit(self, tmp_path):
        config = make_config(tmp_path)
        port = free_port()
        server = start_serve(config, port, [], 1, tmp_path / "server.log")
        try:
            assert wait_until(lambda: up(port), 15)
            time.sleep(4)
            assert server.poll() is None and up(port)
        finally:
            server.kill()
            server.wait()


# ---- the watcher's board command and config ----

class TestBoardCommand:
    def test_idle_exit_passed_unless_zero(self, tmp_path):
        config = make_config(tmp_path)
        assert watch.board_command(config, no_update=True)[-3:] == ["--idle-exit", "15", "--no-update"]
        config = make_config(tmp_path, board_idle_minutes=2.5)
        assert "2.5" in watch.board_command(config, no_update=False)
        config = make_config(tmp_path, board_idle_minutes=0)
        assert "--idle-exit" not in watch.board_command(config, no_update=False)

    @pytest.mark.parametrize("value", [-1, "15", True, None])
    def test_board_idle_minutes_validation(self, tmp_path, value):
        with pytest.raises(BoardConfigError, match="board_idle_minutes"):
            validate_board_config({"repo_root": ".", "watch": {"board_idle_minutes": value},
                                   "sources": [{"name": "t", "root": ".", "editable": False}]}, str(tmp_path))

    def test_board_idle_minutes_default(self, tmp_path):
        assert make_config(tmp_path)["watch"]["board_idle_minutes"] == 15

    def test_idle_exit_option_refuses_negative(self, tmp_path, capsys):
        config = make_config(tmp_path)
        assert cli.main(["--config", config["config_path"], "serve", "--idle-exit", "-1"]) == 2
        assert "--idle-exit" in capsys.readouterr().err


# ---- identity ----

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TestIdentity:
    def test_fingerprint_returns_the_board_id(self, tmp_path):
        config = make_config(tmp_path)
        client = create_app(config).test_client()
        data = client.get("/api/fingerprint", base_url=f"http://127.0.0.1:{config['port']}").get_json()
        assert data["board"] == board_id(config) and len(data["board"]) == 16
        assert str(tmp_path) not in json.dumps(data) and "board.json" not in json.dumps(data)

    def test_board_id_differs_per_checkout_and_ignores_case_on_windows(self, tmp_path):
        a = make_config(tmp_path / "a")
        b = make_config(tmp_path / "b")
        assert board_id(a) != board_id(b)
        if os.name == "nt":
            assert board_id({"config_path": a["config_path"].upper()}) == board_id(a)

    @pytest.mark.parametrize("reply,expected", [
        ("ours", "ours"), ("other", "foreign"), ("none", "foreign"), ("html", "foreign"), ("404", "foreign"),
        ("refused", "down")])
    def test_board_state(self, tmp_path, monkeypatch, reply, expected):
        config = make_config(tmp_path)

        def urlopen(url, timeout):
            if reply == "refused":
                raise urllib.error.URLError(ConnectionRefusedError())
            if reply == "404":
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            body = {"ours": {"fingerprint": "x", "board": board_id(config)},
                    "other": {"fingerprint": "x", "board": "0123456789abcdef"},
                    "none": {"fingerprint": "x"}}.get(reply)
            return FakeResponse(b"<html></html>" if body is None else json.dumps(body).encode())
        monkeypatch.setattr(watch.urllib.request, "urlopen", urlopen)
        assert watch.board_state(config) == expected

    def test_board_state_against_a_real_foreign_board(self, tmp_path):
        """Another checkout's board on the port: foreign for us, ours for its own config."""
        port = free_port()
        other = make_config(tmp_path / "other", port=port)
        mine = make_config(tmp_path / "mine", port=port)
        server = start_serve(other, port, [], 1, tmp_path / "server.log")
        try:
            assert wait_until(lambda: up(port), 15)
            assert watch.board_state(other) == "ours"
            assert watch.board_state(mine) == "foreign"
        finally:
            server.kill()
            server.wait()
        assert watch.board_state(mine) == "down"

    def test_watch_exits_2_on_a_foreign_board_and_starts_none(self, tmp_path, monkeypatch, capsys):
        config = make_config(tmp_path)
        monkeypatch.setattr(watch, "board_state", lambda c: "foreign")
        monkeypatch.setattr(watch, "start_board", lambda *a, **k: pytest.fail("must not start a board"))
        fake_sleep(monkeypatch)
        assert watch.watch(config, update=False) == 2
        err = capsys.readouterr().err
        assert f"BOARD FAILED: port {config['port']} is held by another task board" in err
        assert "--port N" in err and ("netstat -ano" in err if os.name == "nt" else "lsof" in err)

    def test_start_board_reports_a_foreign_board(self, tmp_path, monkeypatch):
        config = make_config(tmp_path)

        class Running:
            def poll(self):
                return None
        states = iter(["down", "foreign"])
        monkeypatch.setattr(watch, "BOARD_DIR", str(tmp_path))
        monkeypatch.setattr(watch, "board_state", lambda c: next(states))
        monkeypatch.setattr(watch.subprocess, "Popen", lambda cmd, stdout, **kw: Running())
        monkeypatch.setattr(watch.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="held by another task board"):
            watch.start_board(config)


class TestLogQuieting:
    def _handler(self, path, code, caplog):
        h = cli.quiet_handler_class().__new__(cli.quiet_handler_class())
        h.path = path
        h.requestline = f"GET {path} HTTP/1.1"
        h.client_address = ("127.0.0.1", 1)
        with caplog.at_level("INFO", logger="werkzeug"):
            h.log_request(code, "-")
        text = caplog.text
        caplog.clear()
        return text

    def test_routine_polls_are_not_logged(self, caplog):
        for path in ("/api/fingerprint", "/api/fingerprint?x=1", "/api/update", "/static/app.js"):
            assert self._handler(path, 200, caplog) == ""
        assert self._handler("/api/fingerprint", 304, caplog) == ""

    def test_errors_and_other_requests_are_logged(self, caplog):
        assert "404" in self._handler("/nope", 404, caplog)
        assert "500" in self._handler("/api/fingerprint", 500, caplog)
        assert "/task/T1" in self._handler("/task/T1", 200, caplog)

    def test_start_board_rotates_a_large_log(self, tmp_path, monkeypatch):
        log_dir = tmp_path / "log"
        log_dir.mkdir()
        (log_dir / "server.log").write_bytes(b"x" * (1024 * 1024 + 1))
        (log_dir / "server.log.1").write_bytes(b"old")

        class Running:
            def poll(self):
                return None
        monkeypatch.setattr(watch, "BOARD_DIR", str(tmp_path))
        monkeypatch.setattr(watch, "board_state", lambda c: "ours")
        monkeypatch.setattr(watch.subprocess, "Popen", lambda cmd, stdout, **kw: Running())
        watch.start_board(make_config(tmp_path))
        assert (log_dir / "server.log.1").stat().st_size == 1024 * 1024 + 1
        assert (log_dir / "server.log").stat().st_size == 0

    def test_small_log_is_kept(self, tmp_path):
        log = tmp_path / "server.log"
        log.write_bytes(b"small")
        watch.rotate_log(str(log))
        assert log.read_bytes() == b"small" and not (tmp_path / "server.log.1").exists()
