"""
Tests for what wakes the supervisor and when (watch.py): the hold window (`watch.hold_seconds`), the "wake
now" marker (`log/wake-now.json`, the board's `/wake`), the INCOMING filter (only questions for the
supervisor, tasks now waiting on it and board lines start a batch; a coordinator wakes for its questions and
board lines, not for a task newly theirs) and the report's task blocks (state, the answered questions with
their answers). The loop is driven with the fakes of test_update (FakeTime for the sleeps and the clock).
"""
import json
import os
import re
from datetime import datetime

import pytest

from taskboard import watch
from taskboard.config import BoardConfigError, validate_board_config
from taskboard.model import STATES
from taskboard.update import UpdateResult
from taskboard.web.app import create_app
from test_coordinator import people_config, write_task as write_owned
from test_update import BASE, FakeTime, plain_config, run_watch, write_task

assert plain_config and people_config  # the fixtures, imported for the watcher tests

ANSWERED = ("- **Q1** (answered 2026-10-20, Alex, for user): Which format? (a) CSV (b) JSON → a: CSV\n"
            "- **Q2** (answered 2026-10-20, Alex, for user): A long one\n"
            "  that continues here → the second answer; with details\n"
            "- **Q3** (open, for user): Still open?\n")


def updates(arrivals):
    """An update function: "ok" at revision 5 until an arrival is queued, then it runs and the revision moves."""
    revs = [5]

    def fake_update(c):
        if arrivals:
            arrivals.pop(0)()
            revs.append(revs[-1] + 1)
            return UpdateResult("changed", revision_before=5, revision=revs[-1], changed=[("U", "AI/Tasks")])
        return UpdateResult("ok", revision=revs[-1])
    return fake_update


def board_line(path, line="- 2026-10-20 Alex (board): answered Q1."):
    def write():
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    return write


def arrival(arrivals, make):
    """An action that queues `make` as the next update's arrival."""
    return lambda: arrivals.append(make)


# ---- config ----

class TestHoldConfig:
    def config(self, tmp_path, **watch_keys):
        for state in STATES:
            (tmp_path / "AI" / "Tasks" / state).mkdir(parents=True, exist_ok=True)
        return validate_board_config({"repo_root": ".", "watch": watch_keys,
                                      "sources": [{"name": "t", "root": "AI/Tasks"}]}, str(tmp_path))

    def test_default_keeps_old_configs_working(self, tmp_path):
        assert self.config(tmp_path)["watch"]["hold_seconds"] == 0

    @pytest.mark.parametrize("hold", [0, 30, 1800])
    def test_valid_hold(self, tmp_path, hold):
        assert self.config(tmp_path, hold_seconds=hold)["watch"]["hold_seconds"] == hold

    @pytest.mark.parametrize("hold", [-1, 1.5, "60", True, None])
    def test_invalid_hold(self, tmp_path, hold):
        with pytest.raises(BoardConfigError, match="watch.hold_seconds must be a whole number of seconds, at least 0"):
            self.config(tmp_path, hold_seconds=hold)


# ---- the hold window ----

class TestHold:
    def test_exits_only_after_the_hold_even_when_quiet_passed(self, plain_config, monkeypatch, capsys):
        config = plain_config
        path = write_task(config, "T001", "Mine")
        fake = FakeTime(actions={3: board_line(path)})  # input at 15 s (polls of 5 s)
        assert run_watch(config, monkeypatch, fake, updates([]), quiet_seconds=10, hold_seconds=100) == 0
        assert fake.now == 115  # the hold counts from the first input, not from the end of the quiet period
        out = capsys.readouterr().out
        assert "hold 100 s" in out.splitlines()[0]
        assert "BOARD INPUT:\n  T001: - 2026-10-20 Alex (board): answered Q1.\n" in out

    def test_without_a_hold_the_quiet_period_alone_decides(self, plain_config, monkeypatch):
        path = write_task(plain_config, "T001", "Mine")
        fake = FakeTime(actions={3: board_line(path)})
        assert run_watch(plain_config, monkeypatch, fake, updates([]), quiet_seconds=10) == 0
        assert fake.now == 25

    def test_later_input_extends_the_quiet_period_but_not_the_hold(self, plain_config, monkeypatch, capsys):
        path = write_task(plain_config, "T001", "Mine")
        fake = FakeTime(actions={3: board_line(path), 10: board_line(path, "- 2026-10-20 Alex (board): more.")})
        assert run_watch(plain_config, monkeypatch, fake, updates([]), quiet_seconds=10, hold_seconds=100) == 0
        assert fake.now == 115  # quiet since 60, hold since 15: the hold ends the wait
        assert capsys.readouterr().out.count("Alex (board)") == 2
        fake = FakeTime(actions={3: board_line(path, "- 2026-10-20 Alex (board): third.")})
        assert run_watch(plain_config, monkeypatch, fake, updates([]), quiet_seconds=10, hold_seconds=0) == 0

    def test_the_hold_is_read_from_the_config(self, plain_config, monkeypatch):
        plain_config["watch"]["hold_seconds"] = 50
        path = write_task(plain_config, "T001", "Mine")
        fake = FakeTime(actions={3: board_line(path)})
        assert run_watch(plain_config, monkeypatch, fake, updates([]), quiet_seconds=10) == 0
        assert fake.now == 65

    def test_an_update_problem_is_not_held(self, plain_config, monkeypatch, capsys):
        conflict = UpdateResult("conflict", conflicts=["AI/Tasks/01-Refinement/T001-x.md"], message="conflict")
        results = [UpdateResult("ok", revision=3), conflict]
        fake = FakeTime()
        assert run_watch(plain_config, monkeypatch, fake, lambda c: results.pop(0), hold_seconds=1000) == 3
        assert fake.now == 10 and "UPDATE STOPPED" in capsys.readouterr().out


# ---- wake now ----

class TestWakeNow:
    def test_marker_ends_the_wait_at_once(self, plain_config, monkeypatch, capsys):
        config = plain_config
        path = write_task(config, "T001", "Mine", questions=ANSWERED)
        fake = FakeTime(actions={3: board_line(path),
                                 5: lambda: watch.request_wake(config, "Robin", "2026-10-20T10:00:00")})
        assert run_watch(config, monkeypatch, fake, updates([]), quiet_seconds=100, hold_seconds=1000) == 0
        assert fake.now == 25
        assert not os.path.exists(watch.wake_path(config))
        out = capsys.readouterr().out
        assert out.splitlines()[1:] == [
            "WAKE NOW: requested by Robin at 2026-10-20T10:00:00",
            "BOARD INPUT:",
            "  T001: - 2026-10-20 Alex (board): answered Q1.",
            "        state 00-Conception, waiting on -, owner -",
            "        Q1: Which format? (a) CSV (b) JSON",
            "        answer (Alex): a: CSV",
            "WAITING ON SUPERVISOR: none",
        ]

    def test_marker_with_nothing_pending_still_exits_0(self, plain_config, monkeypatch, capsys):
        write_task(plain_config, "T001", "Waits", waiting="supervisor")
        fake = FakeTime(actions={4: lambda: watch.request_wake(plain_config, "Alex", "2026-10-20T10:00:00")})
        assert run_watch(plain_config, monkeypatch, fake, updates([])) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[1:] == ["WAKE NOW: requested by Alex at 2026-10-20T10:00:00",
                                        "WAITING ON SUPERVISOR:", "  T001 Waits (00-Conception)"]
        assert "BOARD INPUT" not in out and "INCOMING" not in out

    def test_a_stale_marker_at_start_is_deleted_silently(self, plain_config, monkeypatch, capsys):
        watch.request_wake(plain_config, "Alex")
        assert os.path.exists(watch.wake_path(plain_config))
        assert run_watch(plain_config, monkeypatch, FakeTime(limit=6), updates([])) == 130
        assert not os.path.exists(watch.wake_path(plain_config))
        out = capsys.readouterr().out
        assert out.count("\n") == 1 and "WAKE NOW" not in out

    def test_marker_file_shape_and_an_unreadable_marker(self, plain_config, monkeypatch, capsys):
        watch.request_wake(plain_config, "Robin")
        path = watch.wake_path(plain_config)
        assert path == os.path.join(os.path.dirname(plain_config["config_path"]), "log", "wake-now.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["by"] == "Robin" and datetime.fromisoformat(data["at"])
        assert watch.take_wake(plain_config) == data and watch.take_wake(plain_config) is None
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json")
        assert watch.take_wake(plain_config) == {"by": "someone", "at": "an unknown time"}
        assert not os.path.exists(path)

    def test_wake_now_also_ends_a_coordinators_wait(self, people_config, monkeypatch, capsys):
        fake = FakeTime(actions={3: lambda: watch.request_wake(people_config, "Robin", "t")})
        assert run_watch(people_config, monkeypatch, fake, updates([]), person="Robin") == 0
        assert capsys.readouterr().out.endswith("WAKE NOW: requested by Robin at t\nWAITING ON ROBIN: none\n")


def local_json(config, **data):
    with open(os.path.join(os.path.dirname(config["config_path"]), "local.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


class TestWakeRoute:
    def token_of(self, html: str) -> str:
        return re.search(r'name="token" value="([^"]+)"', html).group(1)

    def test_post_writes_the_marker_as_me_and_redirects_back(self, people_config):
        from taskboard.config import load_board_config
        local_json(people_config, me="Robin")
        config = load_board_config(people_config["config_path"])
        client = create_app(config).test_client()
        html = client.get("/?priority=high", base_url=BASE).get_data(as_text=True)
        assert "Wake the supervisor now" in html and 'action="/wake"' in html
        assert 'name="next" value="/?priority=high"' in html
        response = client.post("/wake", base_url=BASE, data={"token": self.token_of(html), "next": "/?priority=high"})
        assert response.status_code == 302
        assert response.headers["Location"] == "/?priority=high&wake=requested"
        with open(watch.wake_path(config), encoding="utf-8") as f:
            data = json.load(f)
        assert data["by"] == "Robin" and datetime.fromisoformat(data["at"])
        page = client.get(response.headers["Location"], base_url=BASE).get_data(as_text=True)
        assert "Wake requested" in page
        # No task file changed, nothing else was written next to the marker.
        assert sorted(os.listdir(os.path.dirname(watch.wake_path(config)))) == ["wake-now.json"]

    def test_coordinator_checkout_wakes_its_own_session(self, people_config):
        from taskboard.config import load_board_config
        local_json(people_config, me="Robin", board_commits=True)
        config = load_board_config(people_config["config_path"])
        html = create_app(config).test_client().get("/", base_url=BASE).get_data(as_text=True)
        assert "Wake Robin&#39;s session now" in html or "Wake Robin's session now" in html
        assert "Wake the supervisor now" not in html

    def test_without_a_person_the_marker_says_user(self, plain_config):
        client = create_app(plain_config).test_client()
        html = client.get("/", base_url=BASE).get_data(as_text=True)
        assert client.post("/wake", base_url=BASE, data={"token": self.token_of(html)}).status_code == 302
        with open(watch.wake_path(plain_config), encoding="utf-8") as f:
            assert json.load(f)["by"] == "user"

    def test_bad_token_or_foreign_next_target(self, plain_config):
        client = create_app(plain_config).test_client()
        assert client.post("/wake", base_url=BASE, data={"token": "guess"}).status_code == 403
        assert not os.path.exists(watch.wake_path(plain_config))
        html = client.get("/", base_url=BASE).get_data(as_text=True)
        response = client.post("/wake", base_url=BASE, data={"token": self.token_of(html),
                                                             "next": "//evil.example/x"})
        assert response.headers["Location"] == "/?wake=requested"
        assert client.post("/wake", base_url="http://evil.example:5070", data={}).status_code == 403


# ---- what wakes: the INCOMING filter ----

class TestIncomingFilter:
    def test_a_new_task_alone_does_not_wake(self, plain_config, monkeypatch, capsys):
        config = plain_config
        arrivals = []
        fake = FakeTime(actions={3: arrival(arrivals, lambda: write_task(config, "T001", "New"))}, limit=60)
        assert run_watch(config, monkeypatch, fake, updates(arrivals), quiet_seconds=10) == 130
        assert "INCOMING" not in capsys.readouterr().out

    def test_another_devs_board_line_alone_does_not_wake(self, plain_config, monkeypatch, capsys):
        config = plain_config
        path = write_task(config, "T001", "Old")
        arrivals = []
        fake = FakeTime(actions={3: arrival(arrivals, board_line(path, "- 2026-10-20 Robin (board): hi."))}, limit=60)
        assert run_watch(config, monkeypatch, fake, updates(arrivals), quiet_seconds=10) == 130
        assert "INCOMING" not in capsys.readouterr().out and "BOARD INPUT" not in capsys.readouterr().out

    def test_a_question_for_the_supervisor_wakes_and_brings_the_rest(self, plain_config, monkeypatch, capsys):
        config = plain_config
        arrivals = []

        def new_task():
            write_task(config, "T001", "New")

        def question():
            write_task(config, "T002", "Asks", questions="- **Q1** (open, for supervisor): Which?\n")
        fake = FakeTime(actions={3: arrival(arrivals, new_task), 6: arrival(arrivals, question)})
        assert run_watch(config, monkeypatch, fake, updates(arrivals), quiet_seconds=10) == 0
        out = capsys.readouterr().out
        assert fake.now == 40  # the question arrived at 30 s; the task at 15 s started no batch
        assert out.split("INCOMING (git pull, 5 -> 7):\n")[1].splitlines()[:3] == [
            "  T001 new task: New (00-Conception)", "  T002 new task: Asks (00-Conception)",
            "  T002 Q1 open, for supervisor"]

    def test_a_task_now_waiting_on_the_supervisor_wakes(self, plain_config, monkeypatch, capsys):
        config = plain_config
        arrivals = []
        fake = FakeTime(actions={3: arrival(arrivals, lambda: write_task(config, "T001", "Review", waiting="supervisor",
                                                                          state="04-CodeReview"))})
        assert run_watch(config, monkeypatch, fake, updates(arrivals), quiet_seconds=10) == 0
        assert "  T001 now waiting on supervisor (04-CodeReview)\n" in capsys.readouterr().out

    def test_wake_kinds(self):
        assert watch.wake_kinds(None) == {"question", "waiting"}
        assert watch.wake_kinds("Robin") == {"question", "board"}

    def test_coordinator_a_task_newly_theirs_does_not_wake_alone(self, people_config, monkeypatch, capsys):
        config = people_config
        arrivals = []
        fake = FakeTime(actions={3: arrival(arrivals, lambda: write_owned(config, "T001", "Hers", owner="Robin"))},
                        limit=60)
        assert run_watch(config, monkeypatch, fake, updates(arrivals), quiet_seconds=10, person="Robin") == 130
        assert "INCOMING" not in capsys.readouterr().out

    def test_coordinator_a_board_line_on_their_task_wakes(self, people_config, monkeypatch, capsys):
        config = people_config
        path = write_owned(config, "T001", "Hers", owner="Robin")
        arrivals = []
        fake = FakeTime(actions={3: arrival(arrivals, board_line(path, "- 2026-10-21 Alex (board): a note."))})
        assert run_watch(config, monkeypatch, fake, updates(arrivals), quiet_seconds=10, person="Robin") == 0
        assert "  T001 Alex (board): a note.\n" in capsys.readouterr().out


# ---- the report's task blocks ----

class TestReportBlocks:
    def test_board_input_block_with_questions_and_answers(self, plain_config, capsys):
        config = plain_config
        write_task(config, "T012", "Export", state="01-Refinement", waiting="supervisor", questions=ANSWERED,
                   log="- 2026-10-20 Alex (board): answered Q1.\n- 2026-10-20 Alex (board): answered Q2.\n")
        lines = {("tasks", "T012", "- 2026-10-20 Alex (board): answered Q1."),
                 ("tasks", "T012", "- 2026-10-20 Alex (board): answered Q2."),
                 ("tasks", "T012", "- 2026-10-20 Alex (board): a note.")}
        watch.report(config, lines)
        assert capsys.readouterr().out == (
            "BOARD INPUT:\n"
            "  T012: - 2026-10-20 Alex (board): a note.\n"
            "  T012: - 2026-10-20 Alex (board): answered Q1.\n"
            "  T012: - 2026-10-20 Alex (board): answered Q2.\n"
            "        state 01-Refinement, waiting on supervisor, owner -\n"
            "        Q1: Which format? (a) CSV (b) JSON\n"
            "        answer (Alex): a: CSV\n"
            "        Q2: A long one\n"
            "        answer (Alex): the second answer; with details\n"
            "WAITING ON SUPERVISOR:\n"
            "  T012 Export (01-Refinement)\n")

    def test_waiting_key_block_uses_the_incoming_board_lines(self, people_config, capsys):
        config = people_config
        write_owned(config, "T012", "Export", owner="Robin", waiting="supervisor", questions=ANSWERED)
        incoming = {("waiting", "tasks", "T012"): "T012 now waiting on supervisor (01-Refinement)",
                    ("board", "tasks", "T012", "- 2026-10-20 Alex (board): answered Q2."):
                        "T012 Alex (board): answered Q2."}
        watch.report(config, set(), incoming, (5, 6))
        assert capsys.readouterr().out == (
            "INCOMING (git pull, 5 -> 6):\n"
            "  T012 now waiting on supervisor (01-Refinement)\n"
            "        state 01-Refinement, waiting on supervisor, owner Robin\n"
            "        Q2: A long one\n"
            "        answer (Alex): the second answer; with details\n"
            "  T012 Alex (board): answered Q2.\n"
            "WAITING ON SUPERVISOR:\n"
            "  T012 Export (01-Refinement)\n")

    def test_a_note_and_an_open_question_need_no_answer_line(self, plain_config, capsys):
        write_task(plain_config, "T001", "Mine", questions=ANSWERED)
        watch.report(plain_config, {("tasks", "T001", "- 2026-10-20 Alex (board): a note."),
                                    ("tasks", "T001", "- 2026-10-20 Alex (board): answered Q3.")})
        out = capsys.readouterr().out
        assert "        state 00-Conception, waiting on -, owner -\n        Q3: Still open?\nWAITING" in out
        assert "answer (" not in out

    def test_long_texts_are_cut_and_unknown_questions_skipped(self, plain_config):
        long_q = "Q" * 400
        long_a = "A" * 400
        task = {"state": "01-Refinement", "header": {"waiting_on": None, "owner": None}, "questions": [],
                "sections": {"Questions": f"- **Q1** (answered 2026-10-20, Robin, for user): {long_q} → {long_a}\n"}}
        from taskboard.model import parse_questions
        task["questions"], _ = parse_questions(task["sections"]["Questions"])
        block = watch.task_block(plain_config, task, ["- 2026-10-20 Robin (board): answered Q1.",
                                                      "- 2026-10-20 Robin (board): answered Q9."])
        assert block[0] == "state 01-Refinement, waiting on -, owner -"
        assert block[1].startswith("Q1: QQQ") and block[1].endswith("...") and len(block[1]) == 4 + 160
        assert block[2].startswith("answer (Robin): AAA") and len(block[2]) == len("answer (Robin): ") + 300
        assert len(block) == 3

    def test_the_default_owner_is_shown(self, people_config):
        write_owned(people_config, "T001", "Mine")
        task = next(t for _, t in watch._tasks(people_config))
        assert watch.task_block(people_config, task, []) == ["state 01-Refinement, waiting on -, owner Alex"]
