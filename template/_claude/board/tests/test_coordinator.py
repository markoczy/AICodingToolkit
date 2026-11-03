"""
Tests for the coordinator automation: the coordinator's watcher (`tb watch --for <dev>`, watch.person_state)
and the board's own commits of answers and notes in coordinator checkouts (commit.py, `tb serve --no-commit`,
local.json `board_commits`, `tb me --board-commits`), with both backends: svn commits the one file to the
repository (and redoes the edit when the file is out of date), git commits it locally.

The version control tests use the throw-away repositories of test_update (`repos`: git, a bare origin and two
clones; `svn_repos`: `svnadmin create` and two trunk checkouts); nothing touches a real repository or a running
board.
"""
import json
import os
import re
import subprocess
import threading
from types import SimpleNamespace

import pytest

from taskboard import cli, commit, watch
from taskboard.config import BoardConfigError, load_board_config, validate_local
from taskboard.model import STATES
from taskboard.update import UpdateResult
from taskboard.vcs import git as gitvcs
from taskboard.vcs import svn as svnvcs
from taskboard.web.app import create_app
from test_update import (BASE, FakeTime, board_json, commit as git_commit_all, git, repos, run_watch, svn,
                         svn_commit_all, svn_repos, task_path, tb)

assert repos and svn_repos  # the fixtures, imported for the version control tests

PEOPLE = ["Alex", "Robin", "Owens"]


@pytest.fixture
def people_config(tmp_path):
    (tmp_path / ".claude" / "board").mkdir(parents=True)
    for state in STATES:
        (tmp_path / "AI" / "Tasks" / state).mkdir(parents=True)
    config = {"people": PEOPLE, "default_owner": "Alex",
              "sources": [{"name": "tasks", "root": "AI/Tasks", "prefix": "T", "editable": True}]}
    board_json(tmp_path).write_text(json.dumps(config), encoding="utf-8")
    return load_board_config(str(board_json(tmp_path)))


def write_task(config, task_id, title, owner="null", state="01-Refinement", waiting="null", questions="", log=""):
    root = config["sources"][0]["root_path"]
    path = os.path.join(root, state, f"{task_id}-{title.lower().replace(' ', '-')}.md")
    text = (f"---\nid: {task_id}\ntitle: {title}\ntype: bug\npriority: low\nowner: {owner}\ncreated: 2026-10-20\n"
            f"updated: 2026-10-20\nwaiting_on: {waiting}\nblocked_by: []\nplan: null\nbranch: null\npr: null\n"
            f"---\n\n## Problem\nP\n\n## Approach\n\n## Acceptance\n\n## Questions\n{questions}\n## Decisions\n\n"
            f"## Log\n- 2026-10-20 supervisor: created.\n{log}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def append(path, line):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def updates_with(arrivals):
    """An update function: "ok" at revision 5 until an arrival is queued, then it runs and the revision moves."""
    revs = [5]

    def fake_update(c):
        if arrivals:
            arrivals.pop(0)()
            revs.append(revs[-1] + 1)
            return UpdateResult("changed", revision_before=revs[-2], revision=revs[-1], changed=[("U", "AI/Tasks")])
        return UpdateResult("ok", revision=revs[-1])
    return fake_update


def run_for(config, monkeypatch, actions, limit=80, person="Robin"):
    """Run `tb watch --for person` with scripted actions; actions return an arrival (or None for a local edit)."""
    arrivals = []

    def wrap(action):
        def run():
            arrival = action()
            if arrival is not None:
                arrivals.append(arrival)
        return run
    fake = FakeTime(actions={k: wrap(a) for k, a in actions.items()}, limit=limit)
    return run_watch(config, monkeypatch, fake, updates_with(arrivals), person=person)


# ---- the supervisor's watcher is unchanged ----

class TestSupervisorParity:
    def test_state_and_report_are_todays(self, people_config, capsys):
        config = people_config
        write_task(config, "T001", "Ask", owner="Robin", waiting="supervisor",
                   questions="- **Q1** (open, for supervisor): Which?\n- **Q2** (open, for user): @Robin: x?\n",
                   log="- 2026-10-20 Robin (board): answered Q0.\n")
        write_task(config, "T002", "Other", state="00-Conception")
        current = watch.state(config)
        assert current == watch.state(config, None)
        assert current == {
            ("task", "tasks", "T001"): "T001 new task: Ask (01-Refinement)",
            ("question", "tasks", "T001", "Q1"): "T001 Q1 open, for supervisor",
            ("waiting", "tasks", "T001"): "T001 now waiting on supervisor (01-Refinement)",
            ("board", "tasks", "T001", "- 2026-10-20 Robin (board): answered Q0."):
                "T001 Robin (board): answered Q0.",
            ("task", "tasks", "T002"): "T002 new task: Other (00-Conception)",
        }
        incoming = {k: v for k, v in current.items() if k[2] == "T001"}
        watch.report(config, {("tasks", "T002", "- 2026-10-20 Alex (board): note.")}, incoming, (5, 7))
        assert capsys.readouterr().out == (
            "BOARD INPUT:\n"
            "  T002: - 2026-10-20 Alex (board): note.\n"
            "        state 00-Conception, waiting on -, owner Alex\n"
            "INCOMING (git pull, 5 -> 7):\n"
            "  T001 new task: Ask (01-Refinement)\n"
            "  T001 Q1 open, for supervisor\n"
            "  T001 now waiting on supervisor (01-Refinement)\n"
            "        state 01-Refinement, waiting on supervisor, owner Robin\n"
            "  T001 Robin (board): answered Q0.\n"
            "WAITING ON SUPERVISOR:\n"
            "  T001 Ask (01-Refinement)\n")

    def test_supervisor_board_never_commits(self, people_config, capsys, monkeypatch):
        assert "--no-commit" in watch.board_command(people_config, no_update=True)
        assert "--no-commit" not in watch.board_command(people_config, no_update=True, commits=True)
        started = []
        monkeypatch.setattr(watch, "board_state", lambda c: "ours" if started else "down")
        monkeypatch.setattr(watch, "start_board", lambda c, no_update=False, commits=False: started.append(commits))
        monkeypatch.setattr(watch.time, "sleep", FakeTime(limit=3).sleep)
        config = {**people_config, "board_commits": True}
        assert watch.watch(config, update=False) == 130
        assert started == [False]
        out = capsys.readouterr().out
        assert "a board it starts never commits" in out and "tb me --board-commits off" in out
        started.clear()
        assert watch.watch(config, update=False, person="Robin") == 130
        assert started == [True]


# ---- the coordinator's watcher: what concerns the dev ----

class TestPersonState:
    def test_keys_for_the_dev(self, people_config):
        config = people_config
        write_task(config, "T001", "Mine", owner="Robin",
                   questions="- **Q1** (open, for user): Unaddressed?\n- **Q2** (open, for supervisor): Internal?\n"
                             "- **Q3** (open, for user): @Owens: yours?\n- **Q4** (answered 2026-10-20, user): x → y\n",
                   log="- 2026-10-20 Alex (board): note on hers.\n- 2026-10-20 Robin (board): own note.\n")
        write_task(config, "T002", "Default owned", owner="null",
                   questions="- **Q1** (open, for user): @Robin: for you?\n- **Q2** (open, for user): Owner's?\n")
        write_task(config, "T003", "Owens", owner="Owens", questions="- **Q1** (open, for user): Owens?\n",
                   log="- 2026-10-20 Alex (board): note on Owens'.\n")
        current = watch.state(config, "Robin")
        shown = {k: v for k, v in current.items() if v is not None}
        assert shown == {
            ("owned", "tasks", "T001"): "T001 now owned by Robin (01-Refinement)",
            ("question", "tasks", "T001", "Q1"): "T001 Q1 open, for Robin",
            ("question", "tasks", "T002", "Q1"): "T002 Q1 open, for Robin",
            ("board", "tasks", "T001", "- 2026-10-20 Alex (board): note on hers."):
                "T001 Alex (board): note on hers.",
        }
        hidden = {k for k, v in current.items() if v is None}
        assert hidden == {("board", "tasks", "T001", "- 2026-10-20 Robin (board): own note."),
                          ("board", "tasks", "T003", "- 2026-10-20 Alex (board): note on Owens'.")}
        # The default owner gets the null tasks and their unaddressed questions.
        alex = watch.state(config, "Alex")
        assert ("owned", "tasks", "T002") in alex and ("question", "tasks", "T002", "Q2") in alex
        assert ("question", "tasks", "T002", "Q1") not in alex

    def test_waiting_list_and_once(self, people_config, capsys):
        config = people_config
        write_task(config, "T001", "Mine", owner="Robin", waiting="user",
                   questions="- **Q1** (open, for user): Which?\n")
        write_task(config, "T002", "Theirs", owner="Owens", waiting="user", questions="- **Q1** (open, for user): x?\n")
        assert watch.watch(config, once=True, person="Robin") == 0
        assert capsys.readouterr().out == "WAITING ON ROBIN:\n  T001 Mine (01-Refinement)\n"
        assert watch.watch(config, once=True, person="Alex") == 0
        assert capsys.readouterr().out == "WAITING ON ALEX: none\n"


class TestCoordinatorWatch:
    def test_incoming_for_the_dev(self, people_config, monkeypatch, capsys):
        config = people_config
        mine = write_task(config, "T001", "Mine", owner="Robin")
        write_task(config, "T002", "Later hers", owner="Owens")

        def arrive():
            def files():
                append(mine, "- 2026-10-21 Alex (board): a note on hers.")
                write_task(config, "T002", "Later hers", owner="Robin")
                write_task(config, "T003", "Asks her", owner="Owens", waiting="user",
                           questions="- **Q1** (open, for user): @Robin: which?\n")
            return files
        assert run_for(config, monkeypatch, {3: arrive}) == 0
        out = capsys.readouterr().out
        assert out.startswith("watching tasks for Robin (board")
        assert "BOARD INPUT" not in out
        incoming = out.split("INCOMING (git pull, 5 -> 6):\n")[1].split("WAITING ON ROBIN:")[0]
        assert incoming.splitlines() == [
            "  T003 Q1 open, for Robin",
            "  T002 now owned by Robin (01-Refinement)",
            "  T001 Alex (board): a note on hers.",
        ]
        assert out.endswith("WAITING ON ROBIN:\n  T003 Asks her (01-Refinement)\n")

    def test_what_does_not_concern_the_dev_never_wakes(self, people_config, monkeypatch, capsys):
        config = people_config
        mine = write_task(config, "T001", "Mine", owner="Robin")
        owens = write_task(config, "T002", "Owens", owner="Owens")

        def others():
            def files():
                append(owens, "- 2026-10-21 Alex (board): answered Q1.")  # board line on another dev's task
                write_task(config, "T003", "New for Owens", owner="Owens", waiting="user",
                           questions="- **Q1** (open, for user): Which?\n")  # question for another dev
                write_task(config, "T004", "Supervisor", owner="Owens", waiting="supervisor",
                           state="04-CodeReview",
                           questions="- **Q1** (open, for supervisor): Internal?\n")  # moved / internal question
                os.remove(mine)
                write_task(config, "T001", "Mine", owner="Robin", state="02-Ready",
                           log="- 2026-10-21 executor: done.\n")  # a move and an agent's log line
            return files

        def own_cli():  # the coordinator's own `tb ask --to` (no revision move) is no input
            assert cli.main(["--config", config["config_path"], "ask", "T002", "--to", "Robin",
                             "Own question?"]) == 0
        assert run_for(config, monkeypatch, {3: own_cli, 6: others}, limit=80) == 130
        out = capsys.readouterr().out
        assert "INCOMING" not in out and "BOARD INPUT" not in out

    def test_own_board_input_always_wakes(self, people_config, monkeypatch, capsys):
        """Every line written on this checkout's board wakes the coordinator, on any task."""
        config = people_config
        theirs = write_task(config, "T002", "Owens", owner="Owens")
        assert run_for(config, monkeypatch, {3: lambda: append(theirs, "- 2026-10-21 Robin (board): a note.")}) == 0
        out = capsys.readouterr().out
        assert "BOARD INPUT:\n  T002: - 2026-10-21 Robin (board): a note.\n" in out
        assert "INCOMING" not in out and out.endswith("WAITING ON ROBIN: none\n")

    def test_cli_validates_for(self, people_config, monkeypatch, capsys):
        assert cli.main(["--config", people_config["config_path"], "watch", "--for", "Bob", "--once"]) == 2
        assert "'Bob' is not in board.json people" in capsys.readouterr().err
        assert cli.main(["--config", people_config["config_path"], "watch", "--for", "Owens", "--once"]) == 0
        assert capsys.readouterr().out == "WAITING ON OWENS: none\n"


# ---- local.json board_commits, tb me, tb serve --no-commit ----

class TestOptIn:
    def local(self, config):
        return os.path.join(os.path.dirname(config["config_path"]), "local.json")

    def test_tb_me_board_commits(self, people_config, capsys):
        path = people_config["config_path"]
        assert cli.main(["--config", path, "me", "--board-commits", "on"]) == 1
        assert "set the person first" in capsys.readouterr().err
        assert cli.main(["--config", path, "me", "Robin", "--board-commits", "on"]) == 0
        assert "me = Robin (board commits on)" in capsys.readouterr().out
        with open(self.local(people_config), encoding="utf-8") as f:
            assert json.load(f) == {"me": "Robin", "board_commits": True}
        assert cli.main(["--config", path, "me"]) == 0
        assert capsys.readouterr().out == "Robin\nboard commits on\n"
        assert load_board_config(path)["board_commits"] is True
        assert cli.main(["--config", path, "me", "Owens"]) == 0  # keeps the opt-in
        assert cli.main(["--config", path, "me", "--board-commits", "off"]) == 0
        with open(self.local(people_config), encoding="utf-8") as f:
            assert json.load(f) == {"me": "Owens", "board_commits": False}
        capsys.readouterr()
        assert cli.main(["--config", path, "me"]) == 0
        assert capsys.readouterr().out == "Owens\n"
        assert load_board_config(path)["board_commits"] is False

    def test_validate_local(self):
        assert validate_local({"me": "Robin", "board_commits": True}, PEOPLE) == "Robin"
        with pytest.raises(BoardConfigError, match="board_commits must be true or false"):
            validate_local({"me": "Robin", "board_commits": "yes"}, PEOPLE)

    @pytest.mark.parametrize("local, argv, expected", [
        (None, [], False),
        ({"me": "Robin"}, [], False),
        ({"me": "Robin", "board_commits": True}, [], True),
        ({"me": "Robin", "board_commits": True}, ["--no-commit"], False),
    ])
    def test_serve_commits_only_when_opted_in(self, people_config, monkeypatch, local, argv, expected):
        if local is not None:
            with open(self.local(people_config), "w", encoding="utf-8") as f:
                json.dump(local, f)
        seen = []
        import taskboard.web.app as web_app
        real_create = web_app.create_app

        def fake_create(config, updater=None, activity=None, commits=False):
            seen.append(commits)
            return real_create(config, updater, activity, commits)
        monkeypatch.setattr(web_app, "create_app", fake_create)
        monkeypatch.setattr(cli, "port_problem", lambda host, port: None)
        monkeypatch.setattr(cli, "run_board", lambda *a, **k: None)
        assert cli.main(["--config", people_config["config_path"], "serve", "--no-update", *argv]) == 0
        assert seen == [expected]

    def test_header_note(self, people_config, monkeypatch):
        monkeypatch.setattr(gitvcs, "git_available", lambda: True)
        page = create_app(people_config, commits=True).test_client().get("/", base_url=BASE).get_data(as_text=True)
        assert "Board commits on" in page
        page = create_app(people_config).test_client().get("/", base_url=BASE).get_data(as_text=True)
        assert "Board commits" not in page
        monkeypatch.setattr(gitvcs, "git_available", lambda: False)
        page = create_app(people_config, commits=True).test_client().get("/", base_url=BASE).get_data(as_text=True)
        assert "Board commits off: git is not on PATH" in page


# ---- board commits against throw-away repositories ----

def coordinator(repos, commits=True, vcs="svn"):
    """dev_a as Robin's coordinator checkout (board commits on) with T001 asking her Q1, committed and in dev_b."""
    (repos.a / ".claude" / "board" / "local.json").write_text(
        json.dumps({"me": "Robin", "board_commits": commits}), encoding="utf-8")
    assert tb(repos.a, "new", "--title", "A task", "--type", "bug", "--priority", "low", "--state",
              "01-Refinement", "--owner", "Robin") == 0
    assert tb(repos.a, "ask", "T001", "--to", "Robin", "Which one?") == 0
    if vcs == "svn":
        svn_commit_all(repos.a, "T001")
        svn(repos.b, "update")
    else:
        git_commit_all(repos.a, "T001")
        git(repos.b, "pull", "-q", "--ff-only")
    config = load_board_config(str(board_json(repos.a)))
    return SimpleNamespace(config=config, path=task_path(repos.a), client=create_app(config, commits=commits)
                           .test_client())


def post(board, kind="answer", text="my answer", question="Q1"):
    url = "/task/tasks/01-Refinement/" + board.path.name
    html = board.client.get(url, base_url=BASE).get_data(as_text=True)
    form = {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
            "hash": re.search(r'name="hash" value="([^"]+)"', html).group(1)}
    if kind == "answer":
        form.update(question=question, answer=text)
    else:
        form.update(note=text)
    return board.client.post(f"{url}/{kind}", base_url=BASE, data=form, follow_redirects=True)


def head_log(wc, limit=1) -> str:
    return svn(wc, "log", "-v", "-l", str(limit), "-r", "HEAD:1", svn(wc, "info", "--show-item", "repos-root-url")
               .strip() + "/trunk")


def status(wc) -> str:
    return svn(wc, "status", "-q", "AI").strip()


class TestBoardCommitsSvn:
    def test_answer_and_note_are_committed_alone(self, svn_repos):
        repos = svn_repos
        board = coordinator(repos)
        (repos.a / "src" / "code.txt").write_text("local change\n", encoding="utf-8")  # never committed
        response = post(board)
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        rev = re.search(r"Answer saved and committed \(r(\d+)\)\.", body).group(1)
        log = head_log(repos.a)
        assert f"r{rev} |" in log and "tasks: T001 board input (Robin)" in log
        assert f"/trunk/AI/Tasks/01-Refinement/{board.path.name}" in log and "code.txt" not in log
        assert status(repos.a) == ""
        assert "M" in svn(repos.a, "status", "-q", "src")
        assert "Note saved and committed" in post(board, "note", "a note").get_data(as_text=True)
        svn(repos.b, "update")
        text = task_path(repos.b).read_text(encoding="utf-8")
        assert "Robin (board): answered Q1." in text and "Robin (board): a note" in text

    def test_commits_off_writes_only(self, svn_repos):
        board = coordinator(svn_repos, commits=False)
        body = post(board).get_data(as_text=True)
        assert "Answer saved." in body and "committed" not in body
        assert status(svn_repos.a).startswith("M")

    def test_out_of_date_redoes_the_edit_on_the_incoming_version(self, svn_repos):
        repos = svn_repos
        board = coordinator(repos)
        assert tb(repos.b, "log", "T001", "from b") == 0
        svn_commit_all(repos.b, "b's line")
        body = post(board).get_data(as_text=True)
        assert re.search(r"Answer saved and committed \(r\d+\)\.", body), body
        assert status(repos.a) == ""
        svn(repos.b, "update")
        text = task_path(repos.b).read_text(encoding="utf-8")
        assert "supervisor: from b" in text and "Robin (board): answered Q1." in text
        assert "<<<<<<<" not in text

    def test_out_of_date_and_answered_meanwhile_is_refused(self, svn_repos):
        repos = svn_repos
        board = coordinator(repos)
        assert tb(repos.b, "answer", "T001", "Q1", "b's answer", "--answered-by", "Alex") == 0
        svn_commit_all(repos.b, "answered on b")
        incoming = task_path(repos.b).read_bytes()
        response = post(board, text="my late answer")
        body = response.get_data(as_text=True)
        assert response.status_code == 409
        assert "Answer not saved: the task changed meanwhile" in body and "my late answer" in body
        assert board.path.read_bytes().replace(b"\r\n", b"\n") == incoming.replace(b"\r\n", b"\n")
        assert status(repos.a) == ""
        assert "board input" not in head_log(repos.a)

    def test_out_of_date_and_moved_meanwhile_is_refused(self, svn_repos):
        repos = svn_repos
        board = coordinator(repos)
        moved = repos.b / "AI" / "Tasks" / "02-Ready" / board.path.name
        svn(repos.b, "mv", str(task_path(repos.b)), str(moved))
        svn(repos.b, "commit", "-m", "moved on b")
        response = post(board, text="answer to a moved task")
        body = response.get_data(as_text=True)
        assert response.status_code == 409
        assert "moved or deleted meanwhile" in body and "answer to a moved task" in body
        assert not board.path.exists() and status(repos.a) == ""
        assert "board input" not in head_log(repos.a)

    def test_a_failed_update_in_the_redo_never_loses_the_answer(self, svn_repos, monkeypatch):
        repos = svn_repos
        board = coordinator(repos)
        assert tb(repos.b, "log", "T001", "from b") == 0
        svn_commit_all(repos.b, "b's line")
        real_svn = svnvcs._svn

        def locked_update(config, *args):
            if args[0] == "update":
                return "E155004 Working copy is locked"
            return real_svn(config, *args)
        monkeypatch.setattr(svnvcs, "_svn", locked_update)
        response = post(board, text="answer kept")
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Answer saved, not committed: E155011" in body and "then svn update failed: E155004" in body
        text = board.path.read_text(encoding="utf-8")
        assert "→ answer kept" in text and "Robin (board): answered Q1." in text
        assert status(repos.a).startswith("M")
        assert "board input" not in head_log(repos.a)

    def test_a_failed_update_and_a_refused_rewrite_keep_the_text(self, svn_repos, monkeypatch):
        repos = svn_repos
        board = coordinator(repos)
        assert tb(repos.b, "log", "T001", "from b") == 0
        svn_commit_all(repos.b, "b's line")
        real_svn, real_apply = svnvcs._svn, commit.apply_edit
        calls = []

        def locked_update(config, *args):
            return "E155004 Working copy is locked" if args[0] == "update" else real_svn(config, *args)

        def refuse_the_rewrite(*args, **kwargs):
            calls.append(args[3])
            if len(calls) > 1:
                raise commit.EditError("Q1 is already answered.")
            return real_apply(*args, **kwargs)
        monkeypatch.setattr(svnvcs, "_svn", locked_update)
        monkeypatch.setattr(commit, "apply_edit", refuse_the_rewrite)
        response = post(board, text="typed text kept")
        body = response.get_data(as_text=True)
        assert response.status_code == 409 and calls[1] is None
        assert "Answer not saved:" in body and "could not be written again" in body and "typed text kept" in body

    @pytest.mark.parametrize("stderr, reason, stale", [
        # file:// and the client wrapper: the out-of-date code is on the first line
        ("svn: E155011: Commit failed (details follow):\nsvn: E155011: File 'x.md' is out of date\n",
         "E155011 File 'x.md' is out of date", True),
        # a server's code on a later line only (svn://, https)
        ("svn: E155015: Commit failed (details follow):\nsvn: E160028: File '/trunk/x.md' is out of date\n",
         "E160028 File '/trunk/x.md' is out of date", True),
        ("svn: E175002: Commit failed (details follow):\nsvn: E170013: Unable to connect to a repository\n"
         "svn: E215004: No more credentials or we tried too many times.\n",
         "E170013 Unable to connect to a repository; E215004 No more credentials or we tried too many times.", False),
        ("svn: E165001: Commit failed (details follow):\n", "E165001 Commit failed (details follow):", False),
    ])
    def test_commit_errors_name_the_reason(self, svn_repos, monkeypatch, stderr, reason, stale):
        def failing_run(config, args, timeout=0):
            return SimpleNamespace(returncode=1, stdout="", stderr=stderr)
        monkeypatch.setattr(svnvcs, "_run", failing_run)
        assert svnvcs.svn_commit(svn_repos.config, "x.md", "m") == (None, reason, stale)

    def test_prior_local_edits_are_committed_whole(self, svn_repos):
        repos = svn_repos
        board = coordinator(repos)
        assert tb(repos.a, "log", "T001", "coordinator's own edit") == 0
        assert "Answer saved and committed" in post(board).get_data(as_text=True)
        svn(repos.b, "update")
        text = task_path(repos.b).read_text(encoding="utf-8")
        assert "coordinator's own edit" in text and "Robin (board): answered Q1." in text

    def test_prior_local_edits_out_of_date_are_not_redone(self, svn_repos):
        repos = svn_repos
        board = coordinator(repos)
        assert tb(repos.a, "log", "T001", "coordinator's own edit") == 0
        assert tb(repos.b, "log", "T001", "from b") == 0
        svn_commit_all(repos.b, "b's line")
        response = post(board)
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Answer saved, not committed: E" in body and "svn commit AI" in body
        text = board.path.read_text(encoding="utf-8")
        assert "coordinator's own edit" in text and "Robin (board): answered Q1." in text
        assert status(repos.a).startswith("M")

    def test_svn_failure_leaves_the_answer_saved(self, svn_repos):
        repos = svn_repos
        board = coordinator(repos)
        os.rename(repos.tmp / "server", repos.tmp / "server-gone")
        body = post(board).get_data(as_text=True)
        assert "Answer saved, not committed: E" in body
        assert "Robin (board): answered Q1." in board.path.read_text(encoding="utf-8")

    def test_problems_of_the_file_block_the_commit(self, svn_repos, monkeypatch):
        board = coordinator(svn_repos)
        real_apply = commit.apply_edit

        def apply_with_a_problem(*args, **kwargs):
            real_apply(*args, **kwargs)
            return ["01-Refinement/T001-task.md: broken"]
        monkeypatch.setattr(commit, "apply_edit", apply_with_a_problem)
        body = post(board).get_data(as_text=True)
        assert "Answer saved, not committed: the file has problems: 01-Refinement/T001-task.md: broken" in body
        assert status(svn_repos.a).startswith("M")

    def test_the_write_lock_is_held_during_the_commit(self, svn_repos, monkeypatch):
        board = coordinator(svn_repos)
        seen = []
        real_commit = svnvcs.svn_commit

        def commit_while_updating(config, path, message):
            thread = threading.Thread(target=lambda: seen.append(svnvcs.svn_update(config, lock_wait=0.2).status))
            thread.start()
            thread.join()
            return real_commit(config, path, message)
        monkeypatch.setattr(svnvcs, "svn_commit", commit_while_updating)
        assert "Answer saved and committed" in post(board).get_data(as_text=True)
        assert seen == ["busy"]


def git_status(wc, *paths) -> str:
    return git(wc, "status", "--porcelain", "--", *paths).rstrip()


class TestBoardCommitsGit:
    """git boards commit the one file locally (no push); the update then pauses for the unpushed commit."""

    def test_answer_and_note_are_committed_locally_and_alone(self, repos):
        board = coordinator(repos, vcs="git")
        (repos.a / "src" / "code.txt").write_text("local change\n", encoding="utf-8")  # never committed
        response = post(board)
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        sha = re.search(r"Answer saved and committed locally \(([0-9a-f]{7})\); push with git push\.", body).group(1)
        assert git(repos.a, "rev-parse", "HEAD").startswith(sha)
        shown = git(repos.a, "show", "--stat", "--format=%s", "HEAD")
        assert shown.startswith("tasks: T001 board input (Robin)")
        assert board.path.name in shown and "code.txt" not in shown
        assert git_status(repos.a, "AI") == "" and git_status(repos.a, "src") == " M src/code.txt"
        assert "Note saved and committed locally" in post(board, "note", "a note").get_data(as_text=True)
        # Not pushed: the update pauses until the coordinator pushes, then dev_b sees the lines.
        result = gitvcs.git_update(board.config)
        assert result.status == "dirty-skip" and "2 unpushed commits" in result.message
        git(repos.a, "push", "-q")
        assert gitvcs.git_update(board.config).status == "ok"
        git(repos.b, "pull", "-q", "--ff-only")
        text = task_path(repos.b).read_text(encoding="utf-8")
        assert "Robin (board): answered Q1." in text and "Robin (board): a note" in text

    def test_commits_off_writes_only(self, repos):
        board = coordinator(repos, commits=False, vcs="git")
        body = post(board).get_data(as_text=True)
        assert "Answer saved." in body and "committed" not in body
        assert git_status(repos.a, "AI").startswith(" M")

    def test_a_failed_commit_leaves_the_answer_saved(self, repos, monkeypatch):
        board = coordinator(repos, vcs="git")
        real_run = gitvcs._run

        def failing(config, args, timeout=0):
            if args[0] == "commit":
                return subprocess.CompletedProcess(args, 1, "", "fatal: cannot lock ref HEAD\n")
            return real_run(config, args, timeout)
        monkeypatch.setattr(gitvcs, "_run", failing)
        body = post(board).get_data(as_text=True)
        assert "Answer saved, not committed: git commit failed: cannot lock ref HEAD" in body
        assert "git commit -- AI, then git push" in body
        assert "Robin (board): answered Q1." in board.path.read_text(encoding="utf-8")
        assert git_status(repos.a, "AI").startswith("M ")  # staged by git add, not committed

    def test_prior_local_edits_are_committed_whole(self, repos):
        board = coordinator(repos, vcs="git")
        assert tb(repos.a, "log", "T001", "coordinator's own edit") == 0
        assert "Answer saved and committed locally" in post(board).get_data(as_text=True)
        assert git_status(repos.a, "AI") == ""
        git(repos.a, "push", "-q")
        git(repos.b, "pull", "-q", "--ff-only")
        text = task_path(repos.b).read_text(encoding="utf-8")
        assert "coordinator's own edit" in text and "Robin (board): answered Q1." in text

    def test_problems_of_the_file_block_the_commit(self, repos, monkeypatch):
        board = coordinator(repos, vcs="git")
        real_apply = commit.apply_edit

        def apply_with_a_problem(*args, **kwargs):
            real_apply(*args, **kwargs)
            return ["01-Refinement/T001-task.md: broken"]
        monkeypatch.setattr(commit, "apply_edit", apply_with_a_problem)
        body = post(board).get_data(as_text=True)
        assert "Answer saved, not committed: the file has problems: 01-Refinement/T001-task.md: broken" in body
        assert git_status(repos.a, "AI").startswith(" M")

    def test_the_write_lock_is_held_during_the_commit(self, repos, monkeypatch):
        """git's update takes the lock only for the fast-forward (the local checks pause it first), so the lock
        is observed directly: another taker finds it busy while the board commits."""
        from taskboard import store
        board = coordinator(repos, vcs="git")
        seen = []
        real_commit = gitvcs.git_commit

        def commit_while_locked(config, path, message):
            def take():
                try:
                    with store.write_lock(wait=0.2):
                        seen.append("free")
                except store.LockBusy:
                    seen.append("busy")
            thread = threading.Thread(target=take)
            thread.start()
            thread.join()
            return real_commit(config, path, message)
        monkeypatch.setattr(gitvcs, "git_commit", commit_while_locked)
        assert "Answer saved and committed locally" in post(board).get_data(as_text=True)
        assert seen == ["busy"]
