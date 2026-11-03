"""
Tests for the supervisor's commands (taskboard/cli.py), the state machine (workflow.py) and the watcher
(watch.py). Each test builds a repo in tmp_path with a board.json and drives cli.main(argv) in-process. The
board.json is a git board with strict ownership unless a test configures `vcs` / `ownership` otherwise
(configure()); the version-control guards run against throw-away git and svn repositories.
"""
import json
import os
import shutil
import subprocess
from datetime import date

import pytest

from taskboard import cli, watch, workflow
from taskboard.config import BoardConfigError, load_board_config, validate_board_config
from taskboard.model import STATES, check_tasks, load_tasks, parse_task

TODAY = date.today().isoformat()


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".claude" / "board").mkdir(parents=True)
    (tmp_path / "AI" / "Plans").mkdir(parents=True)
    for state in STATES:
        (tmp_path / "AI" / "Tasks" / state).mkdir(parents=True)
    config = {"main_branch": "master", "sources": [{"name": "tasks", "root": "AI/Tasks", "prefix": "T",
                                                  "editable": True}]}
    (tmp_path / ".claude" / "board" / "board.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def tb(repo, *argv) -> int:
    return cli.main(["--config", str(repo / ".claude" / "board" / "board.json"), *argv])


def configure(repo, **keys):
    """Merge keys into the repo's board.json (e.g. vcs="svn", ownership="open")."""
    path = repo / ".claude" / "board" / "board.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config.update(keys)
    path.write_text(json.dumps(config), encoding="utf-8")


def task_file(repo, task_id="T001"):
    found = list((repo / "AI" / "Tasks").glob(f"*/{task_id}-*.md"))
    assert len(found) == 1, found
    return found[0]


def task(repo, task_id="T001") -> dict:
    path = task_file(repo, task_id)
    return parse_task(path.read_text(encoding="utf-8"), path.parent.name, path.name, path.name)


def log(repo, task_id="T001") -> list[str]:
    return task(repo, task_id)["sections"]["Log"].split("\n")


def check(repo) -> list[str]:
    """The check with the repo's board.json schema (its vcs decides the review field)."""
    from taskboard.config import schema_of
    config = load_board_config(str(repo / ".claude" / "board" / "board.json"))
    return check_tasks(load_tasks(str(repo / "AI" / "Tasks")), str(repo), schema_of(config))


def ready_task(repo, title="A task", task_type="bug"):
    assert tb(repo, "new", "--title", title, "--type", task_type, "--priority", "high", "--problem", "P",
              "--state", "01") == 0
    task_id = sorted(p.name[:4] for p in (repo / "AI" / "Tasks").glob("*/T*.md"))[-1]
    assert tb(repo, "section", task_id, "approach", "--text", "A") == 0
    assert tb(repo, "section", task_id, "acceptance", "--text", "- C") == 0
    return task_id


class TestNew:
    def test_creates_the_file_with_the_next_id(self, repo, capsys):
        assert tb(repo, "new", "--title", "Fix: the thing", "--type", "bug", "--priority", "high",
                  "--problem", "It breaks.") == 0
        out = capsys.readouterr().out
        path = task_file(repo)
        assert path.parent.name == "00-Conception" and path.name == "T001-fix-thing.md"
        assert 'title: "Fix: the thing"' in path.read_text(encoding="utf-8")
        assert task(repo)["sections"]["Problem"] == "It breaks."
        assert log(repo) == [f"- {TODAY} supervisor: created in 00-Conception."]
        assert "commit: tasks: T001 created (Fix: the thing)" in out
        assert check(repo) == []

    def test_numbers_are_never_reused(self, repo):
        (repo / "AI" / "Tasks" / "08-Dropped" / "T041-old.md").write_text("x", encoding="utf-8")
        assert tb(repo, "new", "--title", "Next", "--type", "docs", "--priority", "low") == 0
        assert task_file(repo, "T042").name == "T042-next.md"

    def test_invalid_type_and_state(self, repo):
        assert tb(repo, "new", "--title", "x", "--type", "data", "--priority", "low") == 1
        assert tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "03") == 1
        assert list((repo / "AI" / "Tasks").glob("*/*.md")) == []


class TestQuestions:
    def test_ask_and_answer_keep_waiting_on_consistent(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "ask", "T001", "--for", "user", "Which one?") == 0
        assert tb(repo, "ask", "T001", "--for", "user", "And when?") == 0
        assert tb(repo, "ask", "T001", "--for", "supervisor", "Which file?") == 0
        assert task(repo)["header"]["waiting_on"] == "user"
        assert tb(repo, "answer", "T001", "Q1", "the first", "--via", "chat", "--answered-by", "user") == 0
        assert task(repo)["header"]["waiting_on"] == "user"
        assert tb(repo, "answer", "T001", "q2", "today", "--answered-by", "user") == 0
        t = task(repo)
        assert t["header"]["waiting_on"] is None
        assert [(q["id"], q["status"], q["for"]) for q in t["questions"]] == [
            ("Q1", "answered", "user"), ("Q2", "answered", "user"), ("Q3", "open", "supervisor")]
        assert t["sections"]["Decisions"].split("\n") == [f"- {TODAY}, user: Q1 → the first",
                                                          f"- {TODAY}, user: Q2 → today"]
        assert f"- {TODAY} supervisor: recorded user's answer to Q1 (chat)." in log(repo)
        assert check(repo) == []

    def test_anyone_may_answer_a_question_for_another_role(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "supervisor", "Internal?")
        tb(repo, "ask", "T001", "--for", "supervisor", "Other?")
        assert tb(repo, "answer", "T001", "Q1", "yes", "--answered-by", "supervisor", "--no-decision") == 0
        assert task(repo)["sections"]["Decisions"] == ""
        assert tb(repo, "answer", "T001", "Q2", "no", "--answered-by", "user") == 0
        t = task(repo)
        assert t["sections"]["Questions"].split("\n") == [
            f"- **Q1** (answered {TODAY}, supervisor, for supervisor): Internal? → yes",
            f"- **Q2** (answered {TODAY}, user, for supervisor): Other? → no"]
        assert t["sections"]["Decisions"] == f"- {TODAY}, user: Q2 → no"
        assert check(repo) == []

    def test_unknown_role(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "ask", "T001", "--for", "analyst", "?") == 1

    def test_board_note_wakes_the_supervisor_but_cli_lines_do_not(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        config = load_board_config(str(repo / ".claude" / "board" / "board.json"))
        base = watch.snapshot(config)
        tb(repo, "log", "T001", "a supervisor note")
        assert watch.snapshot(config) == base
        path = task_file(repo)
        from taskboard.textedit import add_note
        path.write_text(add_note(path.read_text(encoding="utf-8"), "please hurry", TODAY), encoding="utf-8")
        assert watch.snapshot(config) - base == {("tasks", "T001", f"- {TODAY} user (board): please hurry")}
        assert task(repo)["header"]["waiting_on"] == "supervisor"


def with_people(repo, me=None):
    path = repo / ".claude" / "board" / "board.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["people"] = ["Alex", "Robin", "Owens"]
    path.write_text(json.dumps(config), encoding="utf-8")
    if me is not None:
        (repo / ".claude" / "board" / "local.json").write_text(json.dumps({"me": me}), encoding="utf-8")


def questions(repo, task_id="T001") -> list[str]:
    return task(repo, task_id)["sections"]["Questions"].split("\n")


def people_check(repo) -> list[str]:
    config = load_board_config(str(repo / ".claude" / "board" / "board.json"), with_local=False)
    return check_tasks(load_tasks(str(repo / "AI" / "Tasks")), str(repo), {
        "types": config["types"], "roles": config["roles"], "people": config["people"]})


class TestAnswerers:
    def test_answer_records_the_named_answerer(self, repo, capsys):
        with_people(repo, me="Robin")
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        assert tb(repo, "answer", "T001", "Q1", "CSV", "--answered-by", "Robin",
                  "--by", "coordinator (Robin)", "--via", "chat") == 0
        assert "T001: Q1 answered by Robin." in capsys.readouterr().out
        assert questions(repo) == [f"- **Q1** (answered {TODAY}, Robin, for user): Which one? → CSV"]
        assert task(repo)["sections"]["Decisions"] == f"- {TODAY}, Robin: Q1 → CSV"
        assert log(repo)[-1] == f"- {TODAY} coordinator (Robin): recorded Robin's answer to Q1 (chat)."
        assert people_check(repo) == []

    @pytest.mark.parametrize("with_me", [True, False])
    def test_answer_without_answered_by_is_refused(self, repo, capsys, with_me):
        with_people(repo, me="Robin" if with_me else None)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        before = task_file(repo).read_bytes()
        assert tb(repo, "answer", "T001", "Q1", "CSV") == 1
        err = capsys.readouterr().err.strip()
        assert err.startswith("error: --answered-by is needed") and "\n" not in err
        assert task_file(repo).read_bytes() == before

    def test_answer_by_a_role_still_works(self, repo):
        with_people(repo)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        assert tb(repo, "answer", "T001", "Q1", "CSV", "--answered-by", "user") == 0
        assert questions(repo) == [f"- **Q1** (answered {TODAY}, user, for user): Which one? → CSV"]
        assert log(repo)[-1] == f"- {TODAY} supervisor: recorded user's answer to Q1."

    def test_named_answerer_for_a_supervisor_question(self, repo):
        with_people(repo, me="Alex")
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "supervisor", "Split?")
        assert tb(repo, "answer", "T001", "Q1", "yes", "--answered-by", "Owens") == 0
        assert questions(repo) == [f"- **Q1** (answered {TODAY}, Owens, for supervisor): Split? → yes"]
        assert people_check(repo) == []

    @pytest.mark.parametrize("name", ["Bob", "alex", "coordinator (Robin)"])
    def test_unknown_answerer_or_decider_is_refused(self, repo, name, capsys):
        with_people(repo)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        before = task_file(repo).read_bytes()
        assert tb(repo, "answer", "T001", "Q1", "x", "--answered-by", name) == 1
        assert tb(repo, "decide", "T001", "x", "--decided-by", name) == 1
        assert "must be a person in board.json people (Alex, Robin, Owens) or a role" in \
            capsys.readouterr().err
        assert task_file(repo).read_bytes() == before

    def test_decide_takes_a_person_or_role(self, repo):
        with_people(repo, me="Owens")
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "decide", "T001", "CSV it is", "--decided-by", "Owens") == 0
        assert tb(repo, "decide", "T001", "keep it", "--decided-by", "Robin") == 0
        assert tb(repo, "decide", "T001", "split", "--decided-by", "supervisor") == 0
        assert task(repo)["sections"]["Decisions"].split("\n") == [
            f"- {TODAY}, Owens: CSV it is", f"- {TODAY}, Robin: keep it", f"- {TODAY}, supervisor: split"]

    @pytest.mark.parametrize("with_me", [True, False])
    def test_decide_without_decided_by_is_refused(self, repo, capsys, with_me):
        with_people(repo, me="Owens" if with_me else None)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        before = task_file(repo).read_bytes()
        assert tb(repo, "decide", "T001", "ok") == 1
        err = capsys.readouterr().err.strip()
        assert err.startswith("error: --decided-by is needed") and "\n" not in err
        assert task_file(repo).read_bytes() == before

    def test_decide_without_people_takes_a_role_only(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "decide", "T001", "ok", "--decided-by", "user") == 0
        assert task(repo)["sections"]["Decisions"] == f"- {TODAY}, user: ok"
        assert tb(repo, "decide", "T001", "ok", "--decided-by", "Alex") == 1

    def test_ask_to_a_person(self, repo):
        with_people(repo)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "ask", "T001", "--to", "Robin", "Which export format?") == 0
        assert tb(repo, "ask", "T001", "--to", "Owens", "@Owens: Already addressed?") == 0
        assert questions(repo) == ["- **Q1** (open, for user): @Robin: Which export format?",
                                   "- **Q2** (open, for user): @Owens: Already addressed?"]
        assert task(repo)["header"]["waiting_on"] == "user"
        assert log(repo)[-1] == f"- {TODAY} supervisor: asked Q2 (for user)."
        assert people_check(repo) == []

    def test_ask_to_an_unknown_person(self, repo):
        with_people(repo)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "ask", "T001", "--to", "Bob", "?") == 1
        assert tb(repo, "ask", "T001", "--to", "user", "?") == 1
        assert questions(repo) == [""]

    def test_to_and_for_are_exclusive(self, repo):
        with_people(repo)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        with pytest.raises(SystemExit):
            tb(repo, "ask", "T001", "--to", "Owens", "--for", "user", "?")
        with pytest.raises(SystemExit):
            tb(repo, "ask", "T001", "?")

    @pytest.mark.parametrize("actor", ["x (board)", "Robin (board)", "user (board): fake"])
    def test_cli_never_writes_the_board_marker(self, repo, actor, capsys):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        before = task_file(repo).read_bytes()
        assert tb(repo, "log", "T001", "hello", "--by", actor) == 1
        assert tb(repo, "decide", "T001", "x", "--by", actor, "--decided-by", "user") == 1
        assert "--by must not contain '(board)'" in capsys.readouterr().err
        assert task_file(repo).read_bytes() == before


class TestMe:
    def local(self, repo):
        return repo / ".claude" / "board" / "local.json"

    def test_set_and_print(self, repo, capsys):
        with_people(repo)
        assert tb(repo, "me") == 1
        assert "not set (run: tb me <name>, one of Alex, Robin, Owens)" in capsys.readouterr().out
        assert tb(repo, "me", "Robin") == 0
        assert json.loads(self.local(repo).read_text(encoding="utf-8")) == {"me": "Robin"}
        capsys.readouterr()
        assert tb(repo, "me") == 0
        assert capsys.readouterr().out == "Robin\n"
        assert tb(repo, "me", "Owens") == 0
        assert json.loads(self.local(repo).read_text(encoding="utf-8")) == {"me": "Owens"}
        assert not (repo / ".claude" / "board" / "local.json.tmp").exists()

    def test_role_is_set_printed_and_removed(self, repo, capsys):
        with_people(repo)
        assert tb(repo, "me", "--role", "supervisor") == 1  # the person first
        assert tb(repo, "me", "Alex", "--role", "supervisor") == 0
        assert json.loads(self.local(repo).read_text(encoding="utf-8")) == {"me": "Alex", "role": "supervisor"}
        assert "me = Alex (role supervisor)" in capsys.readouterr().out
        assert tb(repo, "me") == 0
        assert capsys.readouterr().out == "Alex\nrole supervisor\n"
        assert tb(repo, "board") == 0
        assert tb(repo, "me", "--role", "none") == 0
        assert json.loads(self.local(repo).read_text(encoding="utf-8")) == {"me": "Alex"}
        self.local(repo).write_text(json.dumps({"me": "Alex", "role": "coordinator"}), encoding="utf-8")
        assert tb(repo, "board") == 2
        assert 'role must be "supervisor" or absent' in capsys.readouterr().err

    def test_unknown_name_is_refused(self, repo, capsys):
        with_people(repo)
        assert tb(repo, "me", "Bob") == 1
        assert tb(repo, "me", "user") == 1
        assert not self.local(repo).exists()

    def test_a_stale_local_json_can_be_fixed(self, repo, capsys):
        with_people(repo, me="Bob")
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "board") == 2
        assert "me must be one of the people" in capsys.readouterr().err
        assert tb(repo, "me") == 1
        assert tb(repo, "me", "Alex") == 0
        assert tb(repo, "board") == 0


class TestLocalSources:
    """local.json may add private task folders (e.g. an untracked P source) that board.json never names."""

    def private(self, repo, sources=None, me="Alex"):
        root = repo / "PRIVATE_Tasks"
        for state in STATES:
            (root / state).mkdir(parents=True, exist_ok=True)
        sources = sources or [{"name": "private", "root": "PRIVATE_Tasks", "prefix": "P", "editable": True}]
        with_people(repo)
        (repo / ".claude" / "board" / "local.json").write_text(json.dumps({"me": me, "sources": sources}),
                                                               encoding="utf-8")
        return root

    def test_a_private_source_works_like_any_other(self, repo, capsys):
        root = self.private(repo)
        assert tb(repo, "new", "--title", "secret", "--type", "bug", "--priority", "low", "--source", "private") == 0
        assert len(list((root / "00-Conception").glob("P001-*.md"))) == 1
        capsys.readouterr()
        assert tb(repo, "board") == 0
        assert "P001" in capsys.readouterr().out
        config = load_board_config(str(repo / ".claude" / "board" / "board.json"))
        assert [s["name"] for s in config["sources"]] == ["tasks", "private"]
        assert load_board_config(str(repo / ".claude" / "board" / "board.json"),
                                 with_local=False)["sources"][-1]["name"] == "tasks"

    def test_tb_me_keeps_the_sources(self, repo):
        self.private(repo)
        assert tb(repo, "me", "Owens") == 0
        local = json.loads((repo / ".claude" / "board" / "local.json").read_text(encoding="utf-8"))
        assert local["me"] == "Owens" and local["sources"][0]["prefix"] == "P"

    @pytest.mark.parametrize("source, message", [
        ({"name": "tasks", "root": "PRIVATE_Tasks", "prefix": "P"}, "duplicate name 'tasks'"),
        ({"name": "private", "root": "PRIVATE_Tasks", "prefix": "T"}, "prefix T is used by another source"),
        ({"name": "private", "root": "missing", "prefix": "P"}, "root folder not found"),
    ])
    def test_clashes_and_missing_folders_are_refused(self, repo, source, message, capsys):
        self.private(repo, [source])
        assert tb(repo, "board") == 2
        err = capsys.readouterr().err
        assert "local.json sources[0]" in err and message in err


class TestLocalReloadList:
    """local.json's "reload" list belongs to the reload hook; tb accepts it and still rejects other unknown keys."""

    def write(self, repo, local):
        with_people(repo)
        (repo / ".claude" / "board" / "local.json").write_text(json.dumps(local), encoding="utf-8")

    def test_check_accepts_the_key(self, repo):
        self.write(repo, {"me": "Alex", "reload": ["notes/x.md"]})
        assert tb(repo, "check") == 0

    def test_check_still_rejects_other_unknown_keys(self, repo, capsys):
        self.write(repo, {"me": "Alex", "reload": ["notes/x.md"], "other": 1})
        assert tb(repo, "check") == 2
        assert "unknown key(s) other" in capsys.readouterr().err

    def test_tb_me_keeps_the_list(self, repo):
        self.write(repo, {"me": "Alex", "reload": ["notes/x.md"]})
        assert tb(repo, "me", "Owens") == 0
        local = json.loads((repo / ".claude" / "board" / "local.json").read_text(encoding="utf-8"))
        assert local == {"me": "Owens", "reload": ["notes/x.md"]}


class TestWatchMarker:
    def lines(self, repo, log_lines):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        path = task_file(repo)
        text = path.read_text(encoding="utf-8").rstrip("\n") + "\n" + "\n".join(log_lines) + "\n"
        path.write_text(text, encoding="utf-8")
        config = load_board_config(str(repo / ".claude" / "board" / "board.json"))
        return {line for _, _, line in watch.snapshot(config)}

    def test_board_lines_old_and_named(self, repo):
        assert self.lines(repo, [f"- {TODAY} user (board): answered Q1.",
                                 f"- {TODAY} Robin (board): answered Q2.",
                                 f"- {TODAY} Owens (board): please do T015 first"]) == {
            f"- {TODAY} user (board): answered Q1.", f"- {TODAY} Robin (board): answered Q2.",
            f"- {TODAY} Owens (board): please do T015 first"}

    def test_cli_lines_are_ignored(self, repo):
        assert self.lines(repo, [f"- {TODAY} supervisor: recorded Alex's answer to Q1 (chat).",
                                 f"- {TODAY} coordinator (Robin): recorded Robin's answer to Q2.",
                                 f"- {TODAY} supervisor: the note said user (board): something",
                                 f"- {TODAY} Robin (board) wrote this: no marker"]) == set()

    def test_a_named_board_answer_wakes_the_watcher(self, repo):
        with_people(repo)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        config = load_board_config(str(repo / ".claude" / "board" / "board.json"))
        base = watch.snapshot(config)
        tb(repo, "answer", "T001", "Q1", "CSV", "--answered-by", "Robin")
        assert watch.snapshot(config) == base
        tb(repo, "ask", "T001", "--for", "user", "And when?")
        assert watch.snapshot(config) == base
        path = task_file(repo)
        from taskboard.textedit import answer_question
        path.write_text(answer_question(path.read_text(encoding="utf-8"), "Q2", "today", TODAY, by="Robin",
                                        actor="Robin (board)"), encoding="utf-8")
        assert watch.snapshot(config) - base == {("tasks", "T001", f"- {TODAY} Robin (board): answered Q2.")}


class TestMove:
    def test_full_flow(self, repo, capsys):
        task_id = ready_task(repo)
        assert tb(repo, "move", task_id, "ready") == 0
        assert tb(repo, "move", task_id, "3", "--branch", "t001-fix") == 0
        assert task(repo)["header"]["waiting_on"] == "executor"
        assert tb(repo, "move", task_id, "04-CodeReview", "--pr", "#77") == 0
        t = task(repo)
        assert (t["state"], t["header"]["pr"], t["header"]["waiting_on"]) == ("04-CodeReview", 77, "reviewer")
        assert tb(repo, "move", task_id, "deploy", "--reason", "merged PR #77 into master, reviewer approved") == 0
        assert tb(repo, "move", task_id, "done", "--tokens", "executor 120k, reviewer 40k (estimate ~150k)") == 0
        lines = log(repo)
        assert lines[-6:] == [
            f"- {TODAY} supervisor: 01-Refinement → 02-Ready.",
            f"- {TODAY} supervisor: 02-Ready → 03-Implementation (branch t001-fix).",
            f"- {TODAY} supervisor: 03-Implementation → 04-CodeReview (PR #77).",
            f"- {TODAY} supervisor: 04-CodeReview → 06-Deploy (merged PR #77 into master, reviewer approved).",
            f"- {TODAY} supervisor: 06-Deploy → 07-Done.",
            f"- {TODAY} supervisor: tokens: executor 120k, reviewer 40k (estimate ~150k).",
        ]
        assert task(repo)["header"]["waiting_on"] is None
        assert "commit: tasks: T001 06-Deploy → 07-Done" in capsys.readouterr().out
        assert check(repo) == []

    def test_ready_needs_answers_sections_and_no_blocker(self, repo, capsys):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        tb(repo, "new", "--title", "y", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "?")
        tb(repo, "set", "T001", "blocked_by=T002")
        capsys.readouterr()
        assert tb(repo, "move", "T001", "ready") == 1
        err = capsys.readouterr().err
        for text in ("open question(s) Q1", "Approach section is empty", "Acceptance section is empty",
                     "blocked by T002"):
            assert text in err
        assert task_file(repo).parent.name == "01-Refinement"

    @pytest.mark.parametrize("argv, message", [
        (["03"], "not an allowed move"),
        (["ready", "--force"], "--force needs a --reason"),
        (["00"], "needs a reason"),
        (["dropped"], "needs a reason"),
        (["userreview"], "ask the user a question first"),
    ])
    def test_refused_moves(self, repo, capsys, argv, message):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        capsys.readouterr()
        assert tb(repo, "move", "T001", *argv) == 1
        assert message in capsys.readouterr().err
        assert task_file(repo).parent.name == "01-Refinement"

    def test_implementation_needs_a_branch_and_review_a_pr(self, repo, capsys):
        task_id = ready_task(repo)
        tb(repo, "move", task_id, "ready")
        assert tb(repo, "move", task_id, "implementation") == 1
        assert "no branch" in capsys.readouterr().err
        tb(repo, "move", task_id, "implementation", "--branch", "b")
        assert tb(repo, "move", task_id, "codereview") == 1
        assert "no pull request for the review (--pr)" in capsys.readouterr().err

    def test_done_with_a_pr_needs_tokens_and_landing_needs_a_reason(self, repo, capsys):
        task_id = ready_task(repo)
        tb(repo, "move", task_id, "ready")
        tb(repo, "move", task_id, "3", "--branch", "b")
        tb(repo, "move", task_id, "4", "--pr", "3")
        capsys.readouterr()
        assert tb(repo, "move", task_id, "done") == 1
        err = capsys.readouterr().err
        assert "token counts" in err and "how the change landed" in err

    def test_research_path_has_no_branch_pr_review_or_deploy(self, repo, capsys):
        task_id = ready_task(repo, task_type="research")
        assert tb(repo, "move", task_id, "ready") == 0
        assert tb(repo, "move", task_id, "3") == 0
        t = task(repo)
        assert (t["state"], t["header"]["branch"], t["header"]["waiting_on"]) == ("03-Implementation", None, "executor")
        capsys.readouterr()
        for target in ("codereview", "deploy"):
            assert tb(repo, "move", task_id, target) == 1
            err = capsys.readouterr().err
            assert "research task" in err and "is not on the path" in err
        assert task(repo)["state"] == "03-Implementation"
        assert tb(repo, "move", task_id, "done", "--reason", "result: 42") == 1
        assert "research task needs its agent token counts" in capsys.readouterr().err
        assert tb(repo, "move", task_id, "done", "--reason", "result: 42", "--tokens", "analyst 60k") == 0
        assert task(repo)["state"] == "07-Done"
        assert log(repo)[-2:] == [f"- {TODAY} supervisor: 03-Implementation → 07-Done (result: 42).",
                                  f"- {TODAY} supervisor: tokens: analyst 60k."]
        assert check(repo) == []

    def test_research_keeps_backward_moves_and_user_review(self, repo):
        task_id = ready_task(repo, task_type="research")
        tb(repo, "move", task_id, "ready")
        tb(repo, "move", task_id, "3")
        tb(repo, "ask", task_id, "--for", "user", "Does the result look right?")
        assert tb(repo, "move", task_id, "userreview") == 0
        tb(repo, "answer", task_id, "Q1", "yes", "--answered-by", "user")
        assert tb(repo, "move", task_id, "3", "--reason", "more work") == 0
        assert tb(repo, "move", task_id, "ready") == 1
        assert tb(repo, "move", task_id, "ready", "--reason", "rescope") == 0

    def research_in_user_review(self, repo):
        task_id = ready_task(repo, task_type="research")
        tb(repo, "move", task_id, "ready")
        tb(repo, "move", task_id, "3")
        tb(repo, "ask", task_id, "--for", "user", "Does the result look right?")
        tb(repo, "move", task_id, "userreview")
        tb(repo, "answer", task_id, "Q1", "yes", "--answered-by", "user")
        return task_id

    def test_research_user_review_cannot_go_to_review_or_deploy(self, repo, capsys):
        task_id = self.research_in_user_review(repo)
        capsys.readouterr()
        for target in ("codereview", "deploy"):
            assert tb(repo, "move", task_id, target) == 1
            err = capsys.readouterr().err
            assert "research task" in err and "is not on the path" in err
        assert task(repo)["state"] == "05-UserReview"

    def test_research_user_review_to_done_needs_tokens(self, repo, capsys):
        task_id = self.research_in_user_review(repo)
        capsys.readouterr()
        assert tb(repo, "move", task_id, "done", "--reason", "result: 42") == 1
        assert "research task needs its agent token counts" in capsys.readouterr().err
        assert task(repo)["state"] == "05-UserReview"
        assert tb(repo, "move", task_id, "done", "--reason", "result: 42", "--tokens", "analyst 60k") == 0
        assert task(repo)["state"] == "07-Done"

    def test_other_types_keep_the_branch_and_pr_path(self, repo, capsys):
        for task_type in ("feature", "ops"):
            task_id = ready_task(repo, title=task_type, task_type=task_type)
            tb(repo, "move", task_id, "ready")
            capsys.readouterr()
            assert tb(repo, "move", task_id, "3") == 1
            assert "no branch" in capsys.readouterr().err
            assert tb(repo, "move", task_id, "3", "--branch", "b") == 0
            assert tb(repo, "move", task_id, "done", "--tokens", "x") == 1
            assert "not an allowed move" in capsys.readouterr().err
            assert tb(repo, "move", task_id, "4") == 1
            assert "no pull request" in capsys.readouterr().err

    def test_show_lists_next_states_per_type(self, repo, capsys):
        research = ready_task(repo, title="r", task_type="research")
        feature = ready_task(repo, title="f", task_type="feature")
        for task_id in (research, feature):
            tb(repo, "move", task_id, "ready")
        tb(repo, "move", research, "3")
        tb(repo, "move", feature, "3", "--branch", "b")
        capsys.readouterr()
        assert tb(repo, "show", research) == 0
        out = capsys.readouterr().out
        assert "next states: 01-Refinement, 02-Ready, 05-UserReview, 07-Done, 08-Dropped" in out
        assert tb(repo, "show", feature) == 0
        assert ("next states: 01-Refinement, 02-Ready, 04-CodeReview, 05-UserReview, 08-Dropped"
                in capsys.readouterr().out)

    def test_force_is_logged(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        assert tb(repo, "move", "T001", "ready", "--force", "--reason", "trivial, user said go") == 0
        assert ("[forced past: the Problem section is empty; the Approach section is empty; "
                "the Acceptance section is empty]") in log(repo)[-1]

    def test_user_review_waits_on_the_user(self, repo):
        task_id = ready_task(repo)
        tb(repo, "move", task_id, "ready")
        tb(repo, "move", task_id, "3", "--branch", "b")
        tb(repo, "ask", task_id, "--for", "user", "Does the result look right?")
        assert tb(repo, "move", task_id, "userreview") == 0
        assert task(repo)["header"]["waiting_on"] == "user"
        tb(repo, "answer", task_id, "Q1", "yes", "--answered-by", "user")
        assert task(repo)["header"]["waiting_on"] is None

    def test_drop_closes_open_questions_and_waiting_on(self, repo, capsys):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        tb(repo, "ask", "T001", "--for", "user", "And when?")
        tb(repo, "ask", "T001", "--for", "supervisor", "Which file?")
        tb(repo, "answer", "T001", "Q3", "this one", "--no-decision", "--answered-by", "supervisor")
        tb(repo, "ask", "T001", "--for", "executor", "How?")
        assert task(repo)["header"]["waiting_on"] == "user"
        capsys.readouterr()
        assert tb(repo, "move", "T001", "dropped", "--reason", "duplicate of T009") == 0
        assert task_file(repo).parent.name == "08-Dropped"
        t = task(repo)
        assert t["header"]["waiting_on"] is None
        assert [(q["id"], q["status"]) for q in t["questions"]] == [
            ("Q1", "answered"), ("Q2", "answered"), ("Q3", "answered"), ("Q4", "answered")]
        questions = t["sections"]["Questions"].split("\n")
        assert questions[0] == (f"- **Q1** (answered {TODAY}, supervisor, for user): Which one? → "
                                "obsolete: task dropped (duplicate of T009)")
        assert questions[2].endswith("Which file? → this one")  # already answered: untouched
        assert questions[3].endswith("How? → obsolete: task dropped (duplicate of T009)")
        assert t["sections"]["Decisions"] == ""
        assert any("closed Q1, Q2, Q4 as obsolete" in line and "duplicate of T009" in line for line in log(repo))
        assert check(repo) == []

    def test_drop_without_open_questions_is_unchanged(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "move", "T001", "dropped", "--reason", "not needed") == 0
        assert log(repo)[-1] == f"- {TODAY} supervisor: 00-Conception → 08-Dropped (not needed)."
        assert check(repo) == []

    def test_other_moves_keep_questions_open(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        assert tb(repo, "move", "T001", "refinement") == 0
        assert task(repo)["questions"][0]["status"] == "open"
        assert task(repo)["header"]["waiting_on"] == "user"

    def test_every_allowed_target_is_a_state(self):
        assert set(workflow.ALLOWED) == set(STATES)
        for src, targets in workflow.ALLOWED.items():
            assert all(t in STATES and t != src for t in targets)
        assert all(dst in workflow.ALLOWED[src] for src, dst in workflow.BACKWARD)

    @pytest.mark.parametrize("name, state", [("2", "02-Ready"), ("02", "02-Ready"), ("ready", "02-Ready"),
                                             ("02-ready", "02-Ready"), ("CodeReview", "04-CodeReview")])
    def test_resolve_state(self, name, state):
        assert workflow.resolve_state(name) == state


class TestSetAndSection:
    def test_set_fields(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "new", "--title", "y", "--type", "bug", "--priority", "low")
        (repo / "AI" / "Plans" / "2610-01-X.md").write_text("plan", encoding="utf-8")
        assert tb(repo, "set", "T001", "priority=high", "blocked_by=T002", "plan=AI/Plans/2610-01-X.md",
                  "title=New: title") == 0
        h = task(repo)["header"]
        assert (h["priority"], h["blocked_by"], h["plan"], h["title"]) == (
            "high", ["T002"], "AI/Plans/2610-01-X.md", "New: title")
        assert log(repo)[-1].startswith(f"- {TODAY} supervisor: set priority = high, blocked_by = [T002], ")
        assert check(repo) == []

    @pytest.mark.parametrize("pair", ["owner=me", "pr=abc", "pr=0", "pr=r12", "blocked_by=T999", "plan=AI/Plans/none.md",
                                      "waiting_on=user", "priority=urgent", "type=data"])
    def test_refused_values(self, repo, pair):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        before = task_file(repo).read_bytes()
        assert tb(repo, "set", "T001", pair) == 1
        assert task_file(repo).read_bytes() == before

    def test_section_rewrite_keeps_the_rest(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--problem", "old")
        assert tb(repo, "section", "T001", "problem", "--text", "new\n\n- with a list") == 0
        t = task(repo)
        assert t["sections"]["Problem"] == "new\n\n- with a list"
        assert t["sections"]["Approach"] == ""
        assert tb(repo, "section", "T001", "log", "--text", "x") == 1
        assert tb(repo, "section", "T001", "approach", "--text", "## Log\nx") == 1
        assert check(repo) == []


def with_owners(repo, me=None):
    """Two people and Alex as default_owner (as the project's board.json)."""
    path = repo / ".claude" / "board" / "board.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["people"] = ["Alex", "Robin"]
    config["default_owner"] = "Alex"
    path.write_text(json.dumps(config), encoding="utf-8")
    local = repo / ".claude" / "board" / "local.json"
    if me is not None:
        local.write_text(json.dumps({"me": me}), encoding="utf-8")
    elif local.exists():
        local.unlink()


def owner_check(repo) -> list[str]:
    config = load_board_config(str(repo / ".claude" / "board" / "board.json"), with_local=False)
    from taskboard.config import schema_of
    return check_tasks(load_tasks(str(repo / "AI" / "Tasks")), str(repo), schema_of(config))


def decisions(repo, task_id="T001") -> list[str]:
    return [line for line in task(repo, task_id)["sections"]["Decisions"].split("\n") if line]


class TestOwner:
    def new(self, repo, *extra):
        return tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", *extra)

    def test_new_defaults_to_null(self, repo):
        with_owners(repo)
        assert self.new(repo) == 0
        text = task_file(repo).read_text(encoding="utf-8")
        assert "priority: low\nowner: null\ncreated:" in text
        assert log(repo) == [f"- {TODAY} supervisor: created in 00-Conception."]
        assert owner_check(repo) == []

    def test_new_with_an_owner(self, repo):
        with_owners(repo)
        assert self.new(repo, "--owner", "Robin", "--by", "coordinator (Robin)") == 0
        assert task(repo)["header"]["owner"] == "Robin"
        assert log(repo) == [f"- {TODAY} coordinator (Robin): created in 00-Conception (owner Robin)."]
        assert decisions(repo) == []
        assert self.new(repo, "--owner", "Alex") == 0
        assert task(repo, "T002")["header"]["owner"] is None
        assert owner_check(repo) == []

    def test_new_with_an_unknown_owner_is_refused(self, repo):
        with_owners(repo)
        assert self.new(repo, "--owner", "Owens") == 1
        assert self.new(repo, "--owner", "user") == 1
        assert list((repo / "AI" / "Tasks").glob("*/*.md")) == []

    def test_transfer_by_the_owner(self, repo, capsys):
        with_owners(repo, me="Alex")
        self.new(repo)
        before = task_file(repo).read_text(encoding="utf-8")
        assert tb(repo, "set", "T001", "owner=Robin") == 0
        assert "T001: owner Alex → Robin (decided by Alex)." in capsys.readouterr().out
        after = task_file(repo).read_text(encoding="utf-8")
        assert task(repo)["header"]["owner"] == "Robin"
        assert decisions(repo) == [f"- {TODAY}, Alex: owner: Robin (transferred from Alex)"]
        assert log(repo)[-2:] == [f"- {TODAY} supervisor: set owner = Robin.",
                                  f"- {TODAY} supervisor: recorded a decision by Alex."]
        assert after.replace("owner: Robin\n", "owner: null\n", 1).split("## Decisions")[0] == \
            before.split("## Decisions")[0]
        assert owner_check(repo) == []
        assert tb(repo, "set", "T001", "owner=Alex", "--decided-by", "Robin",
                  "--by", "coordinator (Robin)") == 0
        assert task(repo)["header"]["owner"] is None
        assert decisions(repo)[-1] == f"- {TODAY}, Robin: owner: Alex (transferred from Robin)"
        assert log(repo)[-2] == f"- {TODAY} coordinator (Robin): set owner = null."
        assert owner_check(repo) == []

    def test_only_the_owner_can_transfer(self, repo, capsys):
        with_owners(repo, me="Robin")
        self.new(repo)
        before = task_file(repo).read_bytes()
        assert tb(repo, "set", "T001", "owner=Robin") == 1
        assert ("T001 is owned by Alex; only Alex can transfer it (--decided-by Alex)"
                in capsys.readouterr().err)
        assert task_file(repo).read_bytes() == before

    def test_force_needs_a_reason_and_records_it(self, repo, capsys):
        with_owners(repo, me="Alex")
        self.new(repo, "--owner", "Robin")
        before = task_file(repo).read_bytes()
        assert tb(repo, "set", "T001", "owner=null", "--force") == 1
        assert "--force needs a --reason" in capsys.readouterr().err
        assert task_file(repo).read_bytes() == before
        assert tb(repo, "set", "T001", "owner=null", "--force", "--reason", "Robin left the team") == 0
        assert decisions(repo) == [
            f"- {TODAY}, Alex: owner: Alex (transferred from Robin; forced: Robin left the team)"]
        assert task(repo)["header"]["owner"] is None
        assert owner_check(repo) == []

    def test_same_owner_is_a_no_op(self, repo, capsys):
        with_owners(repo)
        self.new(repo)
        before = task_file(repo).read_bytes()
        assert tb(repo, "set", "T001", "owner=Alex") == 0
        assert tb(repo, "set", "T001", "owner=null") == 0
        assert "owner is already Alex; nothing changed." in capsys.readouterr().out
        assert task_file(repo).read_bytes() == before

    @pytest.mark.parametrize("argv, message", [
        (["owner=Robin", "priority=high"], "can't be combined"),
        (["owner=Owens"], "owner must be null or a person"),
        (["owner=Robin"], "--decided-by is needed"),
        (["owner=Robin", "--decided-by", "supervisor"], "--decided-by must be a person"),
        (["priority=high", "--decided-by", "Alex"], "only apply to owner="),
    ])
    def test_refused(self, repo, capsys, argv, message):
        with_owners(repo)
        self.new(repo)
        before = task_file(repo).read_bytes()
        assert tb(repo, "set", "T001", *argv) == 1
        assert message in capsys.readouterr().err
        assert task_file(repo).read_bytes() == before

    def test_file_without_the_field_needs_the_migration(self, repo, capsys):
        with_owners(repo, me="Alex")
        self.new(repo)
        path = task_file(repo)
        path.write_text(path.read_text(encoding="utf-8").replace("owner: null\n", ""), encoding="utf-8")
        assert tb(repo, "set", "T001", "owner=Robin") == 1
        assert "has no owner field yet; run: tb migrate-owner" in capsys.readouterr().err
        # Other edits still work during the rollout; the missing field is reported, not refused.
        assert tb(repo, "set", "T001", "priority=high") == 1
        assert "problem: 00-Conception/T001-x.md: missing header field(s) owner" in capsys.readouterr().err
        assert task(repo)["header"]["priority"] == "high"

    def test_decide_owner_is_refused(self, repo, capsys):
        with_owners(repo)
        self.new(repo)
        before = task_file(repo).read_bytes()
        assert tb(repo, "decide", "T001", "owner: Robin", "--decided-by", "Robin") == 1
        assert "use: tb set T001 owner=<name>" in capsys.readouterr().err
        assert task_file(repo).read_bytes() == before

    def test_show_and_board_filters(self, repo, capsys):
        with_owners(repo, me="Robin")
        self.new(repo)
        self.new(repo, "--owner", "Robin")
        capsys.readouterr()
        assert tb(repo, "show", "T001") == 0
        assert "owner: - (Alex)" in capsys.readouterr().out
        assert tb(repo, "show", "T002") == 0
        assert "owner: Robin\n" in capsys.readouterr().out
        assert tb(repo, "board", "--mine") == 0
        out = capsys.readouterr().out
        assert "T002" in out and "T001" not in out and "[owner Robin]" in out and "2 tasks, no problems." in out
        assert tb(repo, "board", "--owner", "Alex") == 0
        out = capsys.readouterr().out
        assert "T001" in out and "T002" not in out
        assert tb(repo, "board", "--owner", "Owens") == 1
        with_owners(repo)
        assert tb(repo, "board", "--mine") == 1
        assert "--mine needs this checkout's person" in capsys.readouterr().err

    def test_board_filter_still_checks_every_task(self, repo, capsys):
        with_owners(repo)
        self.new(repo, "--owner", "Robin")
        self.new(repo)
        path = task_file(repo, "T002")
        path.write_text(path.read_text(encoding="utf-8").replace("priority: low", "priority: urgent"),
                        encoding="utf-8")
        assert tb(repo, "board", "--owner", "Robin") == 1
        assert "T002-x.md: invalid priority 'urgent'" in capsys.readouterr().err


class TestMigrateOwnerCommand:
    def old_task(self, repo, state, name, decisions="", priority=True, newline="\n", comment=""):
        lines = ["---", f"id: {name[:4]}", "title: x", "type: bug"] + (
            [f"priority: low{comment}"] if priority else []) + [
            "created: 2026-10-01", "updated: 2026-10-02", "waiting_on: null", "blocked_by: []", "plan: null",
            "branch: null", "pr: null", "---", "", "## Problem", "", "## Approach", "", "## Acceptance", "",
            "## Questions", "", "## Decisions", *decisions, "", "## Log", "- 2026-10-01 supervisor: created.", ""]
        path = repo / "AI" / "Tasks" / state / name
        path.write_bytes(newline.join(lines).encode("utf-8"))
        return path

    def test_migration(self, repo, capsys):
        with_owners(repo)
        plain = self.old_task(repo, "00-Conception", "T001-a.md", comment="   # high | medium | low")
        owned = self.old_task(repo, "07-Done", "T002-b.md", decisions=[
            "- 2026-10-01, Robin: owner: Robin",
            "- 2026-10-02, Robin: owner: Alex (transferred from Robin)",
            "- 2026-10-03, Alex: owner: Robin (transferred from Alex)"])
        back = self.old_task(repo, "02-Ready", "T003-c.md", decisions=["- 2026-10-01, Alex: owner: Alex"])
        crlf = self.old_task(repo, "01-Refinement", "T004-d.md", newline="\r\n")
        unknown = self.old_task(repo, "01-Refinement", "T005-e.md", decisions=["- 2026-10-01, Owens: owner: Owens"])
        nopriority = self.old_task(repo, "01-Refinement", "T006-f.md", priority=False)
        files = [plain, owned, back, crlf, unknown, nopriority]
        before = {p: p.read_bytes() for p in files}

        assert tb(repo, "migrate-owner", "--dry-run") == 0
        out = capsys.readouterr()
        assert {p: p.read_bytes() for p in files} == before
        assert "would set owner: Robin  AI/Tasks/07-Done/T002-b.md" in out.out
        assert "6 file(s) would get an owner line, 0 already had one (dry run, nothing written)." in out.out
        assert "T005-e.md: owner Owens is not in board.json people" in out.err

        assert tb(repo, "migrate-owner") == 0
        assert "6 file(s) got an owner line" in capsys.readouterr().out
        assert plain.read_bytes() == before[plain].replace(
            b"priority: low   # high | medium | low\n", b"priority: low   # high | medium | low\nowner: null\n")
        assert owned.read_bytes() == before[owned].replace(b"priority: low\n", b"priority: low\nowner: Robin\n")
        assert back.read_bytes() == before[back].replace(b"priority: low\n", b"priority: low\nowner: null\n")
        assert crlf.read_bytes() == before[crlf].replace(b"priority: low\r\n", b"priority: low\r\nowner: null\r\n")
        assert unknown.read_bytes() == before[unknown].replace(b"priority: low\n", b"priority: low\nowner: Owens\n")
        assert nopriority.read_bytes() == before[nopriority].replace(b"pr: null\n---", b"pr: null\nowner: null\n---")
        problems = owner_check(repo)
        assert [p for p in problems if "T005" in p] == [
            "01-Refinement/T005-e.md: invalid owner 'Owens' (not in board.json people)"]
        assert [p for p in problems if "T005" not in p] == [
            "01-Refinement/T006-f.md: missing header field(s) priority"]

        migrated = {p: p.read_bytes() for p in files}
        assert tb(repo, "migrate-owner") == 0
        assert "0 file(s) got an owner line, 6 already had one." in capsys.readouterr().out
        assert {p: p.read_bytes() for p in files} == migrated

    def test_existing_owner_mismatch_is_reported_not_changed(self, repo, capsys):
        with_owners(repo)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        path = task_file(repo)
        path.write_text(path.read_text(encoding="utf-8").replace(
            "## Decisions\n", "## Decisions\n- 2026-10-01, Robin: owner: Robin\n"), encoding="utf-8")
        before = path.read_bytes()
        capsys.readouterr()
        assert tb(repo, "migrate-owner") == 0
        assert "Decisions name owner Robin, header owner is Alex (left as is)" in capsys.readouterr().err
        assert path.read_bytes() == before


def git(cwd, *args, check=True) -> str:
    from taskboard.vcs.git import git_env
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=git_env(),
                            stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


@pytest.fixture
def git_repo(tmp_path):
    """A local git repository on master (repository-local identity) laid out like `repo`; returns the checkout."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    wc = tmp_path / "wc"
    git(tmp_path, "init", "-q", "-b", "master", str(wc))
    for key, value in (("user.name", "dev"), ("user.email", "dev@example.invalid"), ("core.autocrlf", "false"),
                       ("commit.gpgsign", "false")):
        git(wc, "config", key, value)
    (wc / ".claude" / "board").mkdir(parents=True)
    (wc / "AI" / "Plans").mkdir(parents=True)
    for state in STATES:
        (wc / "AI" / "Tasks" / state).mkdir(parents=True)
    (wc / "AI" / "Tasks" / "README.md").write_text("tasks\n", encoding="utf-8")
    config = {"main_branch": "master", "sources": [{"name": "tasks", "root": "AI/Tasks", "prefix": "T",
                                                    "editable": True}]}
    (wc / ".claude" / "board" / "board.json").write_text(json.dumps(config), encoding="utf-8")
    git(wc, "add", "-A")
    git(wc, "commit", "-q", "-m", "init")
    assert git(wc, "rev-parse", "--show-toplevel").strip().replace("\\", "/").lower().endswith("/wc")
    return wc


class TestGuards:
    def test_tracked_file_moves_with_git_mv(self, git_repo, capsys):
        task_id = ready_task(git_repo)
        git(git_repo, "add", "-A", "AI")
        git(git_repo, "commit", "-q", "-m", "task")
        assert tb(git_repo, "move", task_id, "ready") == 0
        assert "(git mv)" in capsys.readouterr().out
        status = git(git_repo, "status", "--porcelain").splitlines()
        assert "R  AI/Tasks/01-Refinement/T001-task.md -> AI/Tasks/02-Ready/T001-task.md" in [
            line.replace("RM", "R ") for line in status]
        assert check(git_repo) == []

    def test_untracked_file_is_renamed(self, git_repo, capsys):
        task_id = ready_task(git_repo)
        assert tb(git_repo, "move", task_id, "ready") == 0
        assert "(rename)" in capsys.readouterr().out
        assert git(git_repo, "status", "--porcelain", "--", "AI/Tasks/02-Ready").startswith("??")

    @pytest.mark.parametrize("how", ["branch", "detached"])
    def test_writes_only_on_the_main_branch(self, git_repo, capsys, how):
        tb(git_repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        git(git_repo, "add", "-A", "AI")
        git(git_repo, "commit", "-q", "-m", "task")
        if how == "branch":
            git(git_repo, "checkout", "-q", "-b", "t001-x")
        else:
            git(git_repo, "checkout", "-q", "--detach")
        before = task_file(git_repo).read_bytes()
        capsys.readouterr()
        for argv in (["log", "T001", "x"], ["move", "T001", "01"], ["set", "T001", "priority=high"],
                     ["new", "--title", "y", "--type", "bug", "--priority", "low"]):
            assert tb(git_repo, *argv) == 1
            err = capsys.readouterr().err
            assert "not of 'master'" in err
            assert ("branch 't001-x'" if how == "branch" else "a detached HEAD") in err
        assert task_file(git_repo).read_bytes() == before
        assert len(list((git_repo / "AI" / "Tasks").glob("*/*.md"))) == 1
        git(git_repo, "checkout", "-q", "master")
        assert tb(git_repo, "log", "T001", "x") == 0

    def test_a_folder_git_tracks_nothing_in_is_writable_off_the_main_branch(self, git_repo, capsys):
        private = git_repo / "PRIVATE_Tasks"
        for state in STATES:
            (private / state).mkdir(parents=True)
        (git_repo / ".gitignore").write_text("PRIVATE_Tasks/\n", encoding="utf-8")
        board = git_repo / ".claude" / "board" / "board.json"
        data = json.loads(board.read_text(encoding="utf-8"))
        data["sources"].append({"name": "private", "root": "PRIVATE_Tasks", "prefix": "P", "editable": True})
        board.write_text(json.dumps(data), encoding="utf-8")
        git(git_repo, "checkout", "-q", "-b", "t001-x")
        assert tb(git_repo, "new", "--source", "private", "--title", "mine", "--type", "bug",
                  "--priority", "low") == 0
        assert tb(git_repo, "log", "P001", "still writable") == 0
        assert tb(git_repo, "new", "--title", "y", "--type", "bug", "--priority", "low") == 1

    def test_outside_any_git_checkout_writes_are_allowed(self, repo):
        from taskboard.vcs import git as gitvcs
        if gitvcs.git_branch(str(repo)) is not None:
            pytest.skip("the temp folder is inside a git checkout")
        assert tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low") == 0

    def test_outside_any_svn_working_copy_writes_are_allowed(self, repo):
        configure(repo, vcs="svn")
        assert tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low") == 0
        assert "rev: null" in task_file(repo).read_text(encoding="utf-8")


class TestReadCommands:
    def test_show_board_check_next(self, repo, capsys):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which?")
        capsys.readouterr()
        assert tb(repo, "show", "T001") == 0
        out = capsys.readouterr().out
        assert "state: 00-Conception" in out and "Q1 (for user): Which?" in out
        assert "next states: 01-Refinement, 08-Dropped" in out
        assert tb(repo, "board") == 0 and "waiting on user" in capsys.readouterr().out
        assert tb(repo, "check") == 0
        assert tb(repo, "next-id") == 0 and capsys.readouterr().out.strip().endswith("T002")
        stamp = date.today().strftime("%y%m")
        (repo / "AI" / "Plans" / f"{stamp}-03-Old.md").write_text("x", encoding="utf-8")
        assert tb(repo, "next-plan", "board watcher, v2") == 0
        assert capsys.readouterr().out.strip() == f"AI/Plans/{stamp}-04-Board-Watcher-V2.md"

    def test_check_fails_on_a_broken_file(self, repo):
        (repo / "AI" / "Tasks" / "02-Ready" / "T001-x.md").write_text("no header", encoding="utf-8")
        assert tb(repo, "check") == 1

    def test_watch_once_lists_tasks_waiting_on_the_supervisor(self, repo, capsys):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        capsys.readouterr()
        assert tb(repo, "watch", "--once") == 0
        assert "T001 x (01-Refinement)" in capsys.readouterr().out


class TestWebLockedStates:
    def test_post_to_a_locked_state_is_refused(self, repo):
        from taskboard.web.app import create_app
        task_id = ready_task(repo)
        tb(repo, "move", task_id, "ready")
        tb(repo, "move", task_id, "3", "--branch", "b")
        config = load_board_config(str(repo / ".claude" / "board" / "board.json"))
        client = create_app(config).test_client()
        url = "/task/tasks/03-Implementation/T001-task.md"
        html = client.get(url, base_url="http://127.0.0.1:5070").get_data(as_text=True)
        token = html.split('name="token" value="')[1].split('"')[0] if 'name="token"' in html else ""
        before = task_file(repo).read_bytes()
        response = client.post(url + "/note", data={"token": token, "hash": "", "note": "x"},
                               base_url="http://127.0.0.1:5070")
        assert response.status_code == 403
        assert task_file(repo).read_bytes() == before


class TestFinishedTasksAreClosed:
    def _write(self, repo, state, waiting, question):
        assert tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low") == 0
        path = task_file(repo)
        body = path.read_text(encoding="utf-8").replace("waiting_on: null", f"waiting_on: {waiting}", 1)
        if question:
            body = body.replace("## Questions\n", "## Questions\n- **Q1** (open, for user): Still?\n", 1)
        target = repo / "AI" / "Tasks" / state / path.name
        target.write_text(body, encoding="utf-8")
        path.unlink()

    def test_dropped_with_an_open_question_is_flagged(self, repo):
        self._write(repo, "08-Dropped", "user", True)
        problems = check(repo)
        assert any("open question(s) Q1 in a finished task" in p for p in problems)
        assert any("waiting_on is 'user' in a finished task" in p for p in problems)

    def test_done_with_waiting_on_is_flagged(self, repo):
        self._write(repo, "07-Done", "user", False)
        problems = check(repo)
        assert any("waiting_on is 'user' in a finished task" in p for p in problems)
        assert not any("open question(s)" in p for p in problems)

    def test_done_with_an_open_question_is_flagged(self, repo):
        self._write(repo, "07-Done", "null", True)
        assert any("open question(s) Q1 in a finished task" in p for p in check(repo))

    def test_clean_finished_tasks_pass(self, repo):
        self._write(repo, "08-Dropped", "null", False)
        assert check(repo) == []


class TestGuardKeysProblemsNotTexts:
    def _finished(self, repo, state="07-Done"):
        assert tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low") == 0
        path = task_file(repo)
        body = path.read_text(encoding="utf-8").replace(
            "## Questions\n", "## Questions\n- **Q1** (open, for supervisor): One?\n"
                              "- **Q2** (open, for supervisor): Two?\n", 1)
        target = repo / "AI" / "Tasks" / state / path.name
        target.write_text(body, encoding="utf-8")
        path.unlink()

    def test_answering_one_of_two_stale_questions_passes(self, repo):
        self._finished(repo)
        assert any("open question(s) Q1, Q2 in a finished task" in p for p in check(repo))
        # Written (not refused); the exit code 1 only reports the problem that remains (Q2).
        assert tb(repo, "answer", "T001", "Q1", "see Decisions", "--answered-by", "supervisor",
                  "--no-decision") == 1
        assert [q["status"] for q in task(repo)["questions"]] == ["answered", "open"]
        assert any("open question(s) Q2 in a finished task" in p for p in check(repo))
        assert tb(repo, "answer", "T001", "Q2", "see Decisions", "--answered-by", "supervisor",
                  "--no-decision") == 0
        assert check(repo) == []
        assert task(repo)["header"]["waiting_on"] is None

    def test_an_edit_that_adds_a_problem_is_still_refused(self, repo, capsys):
        self._finished(repo)
        before = task_file(repo).read_bytes()
        capsys.readouterr()
        # waiting_on user on a finished task: a problem the file did not have
        assert tb(repo, "set", "T001", "waiting_on=user") == 1
        assert "inconsistent" in capsys.readouterr().err
        assert task_file(repo).read_bytes() == before

    def test_new_problems_compares_keys_not_texts(self):
        from taskboard.store import new_problems
        base = ("---\nid: T001\ntitle: x\ntype: bug\npriority: low\nowner: null\ncreated: 2026-01-01\n"
                "updated: 2026-01-01\nwaiting_on: null\nblocked_by: []\nplan: null\nbranch: null\npr: null\n"
                "---\n\n## Problem\nP\n\n## Approach\n\n## Acceptance\n\n## Questions\n{q}\n\n## Decisions\n\n"
                "## Log\n- 2026-01-01 supervisor: created.\n")
        q1, q2 = "- **Q1** (open, for supervisor): One?", "- **Q2** (open, for supervisor): Two?"
        a1 = "- **Q1** (answered 2026-01-02, supervisor, for supervisor): One? → ok"
        a2 = "- **Q2** (answered 2026-01-02, supervisor, for supervisor): Two? → ok"
        both, one, none = (base.format(q=q1 + "\n" + q2), base.format(q=a1 + "\n" + q2),
                           base.format(q=a1 + "\n" + a2))
        args = ("07-Done", "T001-x.md", None, "T")
        assert new_problems(both, one, *args) == []  # the message changed, the problem is the same (Q2)
        assert new_problems(one, none, *args) == []
        added = new_problems(none, one, *args)  # Q2 became open again: a new problem
        assert len(added) == 1 and "open question(s) Q2" in added[0]
        assert len(new_problems(one, both, *args)) == 1  # Q1 reopened

    def test_drop_accepts_a_free_text_by_with_open_questions(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        assert tb(repo, "move", "T001", "dropped", "--reason", "x", "--by", "supervisor (T187)") == 0
        t = task(repo)
        assert t["questions"][0]["status"] == "answered" and t["questions"][0]["answered_by"] == "supervisor"
        assert log(repo)[-1].startswith(f"- {TODAY} supervisor (T187): ")
        assert check(repo) == []

    def test_drop_with_an_unrelated_free_text_by_falls_back_to_user(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        assert tb(repo, "move", "T001", "dropped", "--reason", "x", "--by", "cleanup script") == 0
        assert task(repo)["questions"][0]["answered_by"] == "user"
        assert check(repo) == []

    def test_drop_guard_compares_against_the_original_text(self, repo, monkeypatch, capsys):
        # If closing the questions silently did nothing, the dropped file would keep its problems: refused.
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        monkeypatch.setattr(cli, "close_open_questions", lambda config, text, reason, by: (text, []))
        before = task_file(repo).read_bytes()
        capsys.readouterr()
        assert tb(repo, "move", "T001", "dropped", "--reason", "x") == 1
        assert "inconsistent" in capsys.readouterr().err
        assert task_file(repo).read_bytes() == before

    def test_forced_done_with_an_open_question_passes_as_before(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        tb(repo, "ask", "T001", "--for", "user", "Still open?")
        assert tb(repo, "move", "T001", "userreview") == 0
        # As on master: the forced move is written; the exit code 1 reports the problems the file keeps.
        assert tb(repo, "move", "T001", "done", "--force", "--reason", "closed by hand") == 1
        assert task_file(repo).parent.name == "07-Done"
        assert "[forced past: open question(s) Q1]" in log(repo)[-1]

    def test_done_with_an_open_question_is_refused_without_force(self, repo, capsys):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--state", "01")
        tb(repo, "ask", "T001", "--for", "user", "Still open?")
        assert tb(repo, "move", "T001", "userreview") == 0
        capsys.readouterr()
        assert tb(repo, "move", "T001", "done", "--reason", "closed by hand") == 1
        assert task_file(repo).parent.name == "05-UserReview"


class TestHandoverSizeWarning:
    """tb check / tb board warn (never a problem) when the handover file is over its configured size."""

    def configure(self, repo, handover):
        path = repo / ".claude" / "board" / "board.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["handover"] = handover
        path.write_text(json.dumps(config), encoding="utf-8")

    def write(self, repo, text):
        (repo / "AI" / "SupervisorHandover.md").write_text(text, encoding="utf-8")

    def run_check(self, repo, capsys, command="check"):
        assert tb(repo, command) == 0
        out = capsys.readouterr().out
        assert "no problems." in out
        return [line for line in out.splitlines() if line.startswith("warning:")]

    def test_under_the_limits_is_silent(self, repo, capsys):
        self.configure(repo, {"max_lines": 3, "max_chars": 50, "max_line_chars": 20})
        self.write(repo, "one\ntwo\nthree\n")
        assert self.run_check(repo, capsys) == []

    def test_over_max_lines(self, repo, capsys):
        self.configure(repo, {"max_lines": 2})
        self.write(repo, "a\nb\nc\nd\n")
        warnings = self.run_check(repo, capsys)
        assert len(warnings) == 1 and "4 lines, 2 over the limit of 2" in warnings[0]

    def test_over_max_chars(self, repo, capsys):
        self.configure(repo, {"max_chars": 10})
        self.write(repo, "abcdef\nghijklm\n")
        warnings = self.run_check(repo, capsys)
        assert len(warnings) == 1 and "15 characters, 5 over the limit of 10" in warnings[0]

    def test_long_line(self, repo, capsys):
        self.configure(repo, {"max_line_chars": 10})
        self.write(repo, "short\n" + "x" * 25 + "\n" + "y" * 12 + "\n")
        warnings = self.run_check(repo, capsys)
        assert len(warnings) == 1 and "2 line(s) over 10 characters (longest 25)" in warnings[0]

    def test_board_command_warns_too(self, repo, capsys):
        self.configure(repo, {"max_lines": 1})
        self.write(repo, "a\nb\n")
        assert len(self.run_check(repo, capsys, "board")) == 1

    def test_defaults(self, repo, capsys):
        self.configure(repo, {})
        self.write(repo, "\n".join(["line"] * 61) + "\n")
        warnings = self.run_check(repo, capsys)
        assert len(warnings) == 1 and "61 lines, 1 over the limit of 60" in warnings[0]
        self.write(repo, "x" * 161 + "\n")
        assert len(self.run_check(repo, capsys)) == 1

    def test_custom_path(self, repo, capsys):
        self.configure(repo, {"path": "AI/Other.md", "max_lines": 1})
        (repo / "AI" / "Other.md").write_text("a\nb\n", encoding="utf-8")
        assert len(self.run_check(repo, capsys)) == 1

    def test_missing_file_is_silent(self, repo, capsys):
        self.configure(repo, {"max_lines": 1})
        assert self.run_check(repo, capsys) == []

    def test_missing_key_is_silent(self, repo, capsys):
        self.write(repo, "x" * 5000 + "\n")
        assert self.run_check(repo, capsys) == []

    @pytest.mark.parametrize("handover", [
        {"other": 1}, {"max_lines": 0}, {"max_chars": "5"}, {"max_line_chars": True}, {"path": "../x.md"},
        {"path": "/etc/x.md"}, {"path": ""}, [], "AI/SupervisorHandover.md"])
    def test_invalid_key_is_refused(self, repo, capsys, handover):
        self.configure(repo, handover)
        assert tb(repo, "check") == 2
        assert "handover" in capsys.readouterr().err


class TestTypeRules:
    """The per-type move rules come from `type_rules` in board.json (workflow.DEFAULT_TYPE_RULES without it)."""

    def configure(self, repo, rules):
        path = repo / ".claude" / "board" / "board.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["type_rules"] = rules
        path.write_text(json.dumps(config), encoding="utf-8")

    def validate(self, repo, rules):
        return validate_board_config({"repo_root": ".", "type_rules": rules,
                                      "sources": [{"name": "t", "root": "AI/Tasks"}]}, str(repo))

    def test_shipped_board_json_matches_the_code_default(self):
        try:
            config = load_board_config(with_local=False)
        except BoardConfigError:
            pytest.skip("no readable board.json next to the package (the project's build renders it)")
        assert config["type_rules"] == workflow.DEFAULT_TYPE_RULES

    def test_research_path_from_the_config(self, repo, capsys):
        # A custom path: research may go 03 -> 04 and 03 -> 07, needs no tokens, but needs a branch.
        self.configure(repo, {"research": {"moves": {"03": ["04", "07"]}, "refuse": {"03": ["06"]},
                                            "tokens_from": ["05"]}})
        task_id = ready_task(repo, task_type="research")
        tb(repo, "move", task_id, "ready")
        capsys.readouterr()
        assert tb(repo, "move", task_id, "3") == 1
        assert "no branch" in capsys.readouterr().err
        assert tb(repo, "move", task_id, "3", "--branch", "b") == 0
        assert tb(repo, "show", task_id) == 0
        assert "next states: 04-CodeReview, 07-Done" in capsys.readouterr().out
        assert tb(repo, "move", task_id, "deploy") == 1
        assert "is not on the path of a research task" in capsys.readouterr().err
        assert tb(repo, "move", task_id, "done") == 0

    def test_default_rules_without_the_key_and_none_with_an_empty_map(self, repo, capsys):
        task_id = ready_task(repo, task_type="research")
        tb(repo, "move", task_id, "ready")
        assert tb(repo, "move", task_id, "3") == 0            # no key: the code default (no branch)
        self.configure(repo, {})
        task2 = ready_task(repo, title="two", task_type="research")
        tb(repo, "move", task2, "ready")
        capsys.readouterr()
        assert tb(repo, "move", task2, "3") == 1               # empty map: research follows the default rules
        assert "no branch" in capsys.readouterr().err

    def test_a_type_without_an_entry_is_unchanged(self, repo, capsys):
        self.configure(repo, {"research": {"no_branch": True}})
        task_id = ready_task(repo, task_type="docs")
        tb(repo, "move", task_id, "ready")
        capsys.readouterr()
        assert tb(repo, "move", task_id, "3") == 1
        assert "no branch" in capsys.readouterr().err

    def test_valid_rules_are_normalized(self, repo):
        rules = self.validate(repo, {"research": {"moves": {"3": ["done", "7"]},
                                                  "refuse": {"implementation": ["codereview"]},
                                                  "tokens_from": ["03", "userreview"]}})["type_rules"]
        assert rules == {"research": {"moves": {"03-Implementation": ["07-Done"]},
                                      "refuse": {"03-Implementation": ["04-CodeReview"]},
                                      "tokens_from": ["03-Implementation", "05-UserReview"]}}

    @pytest.mark.parametrize("rules, message", [
        ([], "type_rules must be an object"),
        ({"nonsense": {}}, "unknown type 'nonsense'"),
        ({"research": []}, "type_rules.research must be an object"),
        ({"research": {"move": {}}}, r"unknown key\(s\) move"),
        ({"research": {"moves": []}}, "must be an object"),
        ({"research": {"moves": {"99": ["03"]}}}, "Unknown state '99'"),
        ({"research": {"moves": {"03": "07"}}}, "must be a list of states"),
        ({"research": {"moves": {"03": ["nowhere"]}}}, "Unknown state 'nowhere'"),
        ({"research": {"moves": {"03": ["03"]}}}, "cannot lead to itself"),
        ({"research": {"refuse": {"03": ["04"]}}}, "also an allowed move"),
        ({"research": {"no_branch": "yes"}}, "no_branch must be true or false"),
        ({"research": {"tokens_from": "03"}}, "must be a list of states"),
    ])
    def test_invalid_rules_are_refused(self, repo, rules, message):
        with pytest.raises(BoardConfigError, match=message):
            self.validate(repo, rules)

    def test_invalid_rules_in_the_file_fail_the_cli(self, repo, capsys):
        self.configure(repo, {"research": {"bogus": 1}})
        assert tb(repo, "check") == 2
        assert "type_rules.research" in capsys.readouterr().err


# ---- the svn backend: guards, svn mv, the rev review field ----

def svn(cwd, *args) -> str:
    return subprocess.run(["svn", "--non-interactive", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def svn_repo(tmp_path):
    """A local SVN repository (trunk/branches) with a trunk working copy laid out like `repo`; returns the WC."""
    if shutil.which("svn") is None or shutil.which("svnadmin") is None:
        pytest.skip("svn not installed")
    server = tmp_path / "server"
    subprocess.run(["svnadmin", "create", str(server)], check=True, capture_output=True)
    url = server.as_uri()
    svn(tmp_path, "mkdir", "-m", "layout", f"{url}/trunk", f"{url}/branches")
    wc = tmp_path / "wc"
    svn(tmp_path, "checkout", f"{url}/trunk", str(wc))
    (wc / ".claude" / "board").mkdir(parents=True)
    (wc / "AI" / "Plans").mkdir(parents=True)
    for state in STATES:
        (wc / "AI" / "Tasks" / state).mkdir(parents=True)
    config = {"vcs": "svn", "sources": [{"name": "tasks", "root": "AI/Tasks", "prefix": "T", "editable": True}]}
    (wc / ".claude" / "board" / "board.json").write_text(json.dumps(config), encoding="utf-8")
    svn(wc, "add", "--force", ".")
    svn(wc, "commit", "-m", "init")
    return wc


class TestSvnGuards:
    def test_versioned_file_moves_with_svn_mv(self, svn_repo, capsys):
        task_id = ready_task(svn_repo)
        svn(svn_repo, "add", "--force", "AI")
        svn(svn_repo, "commit", "-m", "task")
        assert tb(svn_repo, "move", task_id, "ready") == 0
        assert "(svn mv)" in capsys.readouterr().out
        lines = svn(svn_repo, "status").replace("\\", "/").splitlines()
        assert any(line.startswith("A  +") and line.endswith("AI/Tasks/02-Ready/T001-task.md") for line in lines)
        assert any(line.startswith("D") and line.endswith("AI/Tasks/01-Refinement/T001-task.md") for line in lines)
        assert check(svn_repo) == []

    def test_unversioned_file_is_renamed(self, svn_repo, capsys):
        task_id = ready_task(svn_repo)
        assert tb(svn_repo, "move", task_id, "ready") == 0
        assert "(rename)" in capsys.readouterr().out

    def test_writes_only_in_a_trunk_working_copy(self, svn_repo, capsys):
        tb(svn_repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        svn(svn_repo, "add", "--force", "AI")
        svn(svn_repo, "commit", "-m", "task")
        url = svn(svn_repo, "info", "--show-item", "repos-root-url").strip()
        svn(svn_repo, "copy", "-m", "branch", f"{url}/trunk", f"{url}/branches/t001-x")
        svn(svn_repo, "switch", f"{url}/branches/t001-x")
        before = task_file(svn_repo).read_bytes()
        capsys.readouterr()
        for argv in (["log", "T001", "x"], ["move", "T001", "01"], ["set", "T001", "priority=high"],
                     ["new", "--title", "y", "--type", "bug", "--priority", "low"]):
            assert tb(svn_repo, *argv) == 1
            assert "not of '^/trunk'" in capsys.readouterr().err
        assert task_file(svn_repo).read_bytes() == before
        assert len(list((svn_repo / "AI" / "Tasks").glob("*/*.md"))) == 1
        svn(svn_repo, "switch", f"{url}/trunk")
        assert tb(svn_repo, "log", "T001", "x") == 0

    def test_main_branch_spellings(self, svn_repo):
        for spelling in ("trunk", "^/trunk", "/trunk/"):
            configure(svn_repo, main_branch=spelling)
            assert tb(svn_repo, "log", "T001", "x") == 1  # no task yet: the guard passed, the task is missing
            assert tb(svn_repo, "new", "--title", "x", "--type", "bug", "--priority", "low") == 0
            task_file(svn_repo).unlink()


class TestReviewFieldPerVcs:
    """`pr` (git) or `rev` (svn): the header field, `tb move --pr/--rev`, `tb set`, `tb show`, `tb board`."""

    def test_svn_full_flow_uses_rev(self, repo, capsys):
        configure(repo, vcs="svn")
        task_id = ready_task(repo)
        assert "rev: null" in task_file(repo).read_text(encoding="utf-8")
        assert tb(repo, "move", task_id, "ready") == 0
        assert tb(repo, "move", task_id, "3", "--branch", "t001-fix") == 0
        capsys.readouterr()
        assert tb(repo, "move", task_id, "codereview") == 1
        assert "no branch revision for the review (--rev)" in capsys.readouterr().err
        assert tb(repo, "move", task_id, "04-CodeReview", "--rev", "r12") == 0
        t = task(repo)
        assert (t["state"], t["header"]["rev"], t["header"]["waiting_on"]) == ("04-CodeReview", 12, "reviewer")
        capsys.readouterr()
        assert tb(repo, "move", task_id, "deploy") == 1
        assert "merged into trunk r57, reviewer approved" in capsys.readouterr().err
        assert tb(repo, "move", task_id, "deploy", "--reason", "merged into trunk r13, reviewer approved") == 0
        assert tb(repo, "move", task_id, "done", "--tokens", "executor 120k, reviewer 40k") == 0
        assert log(repo)[-4:-1] == [
            f"- {TODAY} supervisor: 03-Implementation → 04-CodeReview (review r12).",
            f"- {TODAY} supervisor: 04-CodeReview → 06-Deploy (merged into trunk r13, reviewer approved).",
            f"- {TODAY} supervisor: 06-Deploy → 07-Done.",
        ]
        capsys.readouterr()
        assert tb(repo, "show", task_id) == 0
        assert "rev: r12" in capsys.readouterr().out
        assert tb(repo, "board") == 0
        assert "[r12]" in capsys.readouterr().out
        assert check(repo) == []

    def test_git_show_and_board_label_the_pull_request(self, repo, capsys):
        task_id = ready_task(repo)
        tb(repo, "move", task_id, "ready")
        tb(repo, "move", task_id, "3", "--branch", "b")
        tb(repo, "move", task_id, "4", "--pr", "77")
        capsys.readouterr()
        assert tb(repo, "show", task_id) == 0
        assert "pr: PR #77" in capsys.readouterr().out
        assert tb(repo, "board") == 0
        assert "[waiting on reviewer; PR #77]" in capsys.readouterr().out

    def test_the_other_flag_is_refused_and_names_the_vcs(self, repo, capsys):
        task_id = ready_task(repo)
        tb(repo, "move", task_id, "ready")
        tb(repo, "move", task_id, "3", "--branch", "b")
        before = task_file(repo).read_bytes()
        capsys.readouterr()
        assert tb(repo, "move", task_id, "4", "--rev", "12") == 1
        err = capsys.readouterr().err
        assert "--rev is the review field of svn boards; this board's vcs is git, use --pr" in err
        assert task_file(repo).read_bytes() == before
        configure(repo, vcs="svn")
        assert tb(repo, "move", task_id, "4", "--pr", "12") == 1
        assert "this board's vcs is svn, use --rev" in capsys.readouterr().err

    @pytest.mark.parametrize("vcs, good, bad, other", [
        ("git", ["pr=77", "pr=#77"], ["pr=r12", "pr=abc", "pr=0"], "rev=12"),
        ("svn", ["rev=12", "rev=r12"], ["rev=#12", "rev=abc", "rev=0"], "pr=12"),
    ])
    def test_set_takes_the_vcs_review_field_only(self, repo, capsys, vcs, good, bad, other):
        configure(repo, vcs=vcs)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        field = good[0].split("=")[0]
        for pair in good:
            assert tb(repo, "set", "T001", pair) == 0
            assert task(repo)["header"][field] == int(pair.split("=")[1].lstrip("#r"))
        capsys.readouterr()
        for pair in bad:
            assert tb(repo, "set", "T001", pair) == 1
            assert f"{field} must be" in capsys.readouterr().err
        assert tb(repo, "set", "T001", other) == 1
        err = capsys.readouterr().err
        assert "review field of another version control" in err and f"its review field is {field}=" in err
        assert tb(repo, "set", "T001", "colour=red") == 1
        assert f"key one of title, type, priority, owner, waiting_on, blocked_by, plan, branch, {field}" in \
            capsys.readouterr().err

    def test_a_file_with_the_other_vcs_field_is_a_problem(self, repo):
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        path = task_file(repo)
        path.write_bytes(path.read_bytes().replace(b"pr: null", b"rev: null"))
        assert check(repo) == ["00-Conception/T001-x.md: missing header field(s) pr",
                            "00-Conception/T001-x.md: unknown header field(s) rev"]


# ---- open ownership (board.json "ownership": "open") ----

class TestOpenOwnership:
    """With open ownership --answered-by / --decided-by default to tb me (else user) and anyone sets owners."""

    def people(self, repo, me=None):
        with_people(repo, me=me)
        configure(repo, ownership="open")

    def owners(self, repo, me=None):
        with_owners(repo, me=me)
        configure(repo, ownership="open")

    def test_answer_defaults_to_me(self, repo, capsys):
        self.people(repo, me="Robin")
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        assert tb(repo, "answer", "T001", "Q1", "CSV", "--by", "coordinator (Robin)", "--via", "chat") == 0
        assert "T001: Q1 answered by Robin." in capsys.readouterr().out
        assert questions(repo) == [f"- **Q1** (answered {TODAY}, Robin, for user): Which one? → CSV"]
        assert task(repo)["sections"]["Decisions"] == f"- {TODAY}, Robin: Q1 → CSV"
        assert log(repo)[-1] == f"- {TODAY} coordinator (Robin): recorded Robin's answer to Q1 (chat)."
        assert people_check(repo) == []

    def test_answer_falls_back_to_user(self, repo):
        self.people(repo)
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        assert tb(repo, "answer", "T001", "Q1", "CSV") == 0
        assert questions(repo) == [f"- **Q1** (answered {TODAY}, user, for user): Which one? → CSV"]
        assert log(repo)[-1] == f"- {TODAY} supervisor: recorded user's answer to Q1."

    def test_decide_defaults_to_me_and_takes_a_person_or_role(self, repo):
        self.people(repo, me="Owens")
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "decide", "T001", "CSV it is") == 0
        assert tb(repo, "decide", "T001", "keep it", "--decided-by", "Robin") == 0
        assert tb(repo, "decide", "T001", "split", "--decided-by", "supervisor") == 0
        assert task(repo)["sections"]["Decisions"].split("\n") == [
            f"- {TODAY}, Owens: CSV it is", f"- {TODAY}, Robin: keep it", f"- {TODAY}, supervisor: split"]

    def test_decide_without_people_or_me_is_user(self, repo):
        configure(repo, ownership="open")
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        assert tb(repo, "decide", "T001", "ok") == 0
        assert task(repo)["sections"]["Decisions"] == f"- {TODAY}, user: ok"
        assert tb(repo, "decide", "T001", "ok", "--decided-by", "Alex") == 1

    def test_strict_still_requires_the_flags(self, repo, capsys):
        with_people(repo, me="Robin")
        tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low")
        tb(repo, "ask", "T001", "--for", "user", "Which one?")
        capsys.readouterr()
        assert tb(repo, "answer", "T001", "Q1", "CSV") == 1
        assert "--answered-by is needed" in capsys.readouterr().err
        assert tb(repo, "decide", "T001", "ok") == 1
        assert "--decided-by is needed" in capsys.readouterr().err

    def test_set_owner_by_the_owner_records_set_by(self, repo, capsys):
        self.owners(repo, me="Alex")
        assert tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low") == 0
        assert tb(repo, "set", "T001", "owner=Robin") == 0
        assert "T001: owner Alex → Robin (decided by Alex)." in capsys.readouterr().out
        assert task(repo)["header"]["owner"] == "Robin"
        assert decisions(repo) == [f"- {TODAY}, Alex: owner: Robin (set by Alex, from Alex)"]
        assert log(repo)[-2:] == [f"- {TODAY} supervisor: set owner = Robin.",
                                  f"- {TODAY} supervisor: recorded a decision by Alex."]
        assert owner_check(repo) == []
        assert tb(repo, "set", "T001", "owner=Alex", "--decided-by", "Robin", "--by", "coordinator (Robin)") == 0
        assert task(repo)["header"]["owner"] is None
        assert decisions(repo)[-1] == f"- {TODAY}, Robin: owner: Alex (set by Robin, from Robin)"
        assert owner_check(repo) == []

    def test_anyone_may_set_the_owner(self, repo):
        self.owners(repo, me="Alex")
        configure(repo, people=["Alex", "Robin", "Owens"])
        assert tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--owner", "Robin") == 0
        # a third person, set by someone who is not the current owner
        assert tb(repo, "set", "T001", "owner=Owens") == 0
        assert task(repo)["header"]["owner"] == "Owens"
        assert decisions(repo)[-1] == f"- {TODAY}, Alex: owner: Owens (set by Alex, from Robin)"
        assert owner_check(repo) == []
        assert tb(repo, "set", "T001", "owner=Alex", "--decided-by", "Robin") == 0
        assert decisions(repo)[-1] == f"- {TODAY}, Robin: owner: Alex (set by Robin, from Owens)"
        assert task(repo)["header"]["owner"] is None
        assert owner_check(repo) == []

    def test_force_is_refused_with_open_ownership(self, repo, capsys):
        self.owners(repo, me="Alex")
        assert tb(repo, "new", "--title", "x", "--type", "bug", "--priority", "low", "--owner", "Robin") == 0
        before = task_file(repo).read_bytes()
        assert tb(repo, "set", "T001", "owner=null", "--force", "--reason", "left") == 1
        assert "--force and --reason are for strict ownership" in capsys.readouterr().err
        assert task_file(repo).read_bytes() == before

    def test_invalid_ownership_value_is_a_config_error(self, repo, capsys):
        configure(repo, ownership="loose")
        assert tb(repo, "check") == 2
        assert "ownership must be one of strict, open" in capsys.readouterr().err
        configure(repo, vcs="hg")
        assert tb(repo, "check") == 2
        assert "vcs must be one of git, svn" in capsys.readouterr().err

    def test_main_branch_defaults_per_vcs(self, repo):
        assert load_board_config(str(repo / ".claude" / "board" / "board.json"))["main_branch"] == "master"
        path = repo / ".claude" / "board" / "board.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        del config["main_branch"]
        config["vcs"] = "svn"
        path.write_text(json.dumps(config), encoding="utf-8")
        assert load_board_config(str(path))["main_branch"] == "trunk"
        config["vcs"] = "git"
        path.write_text(json.dumps(config), encoding="utf-8")
        assert load_board_config(str(path))["main_branch"] == "master"


class TestBoardCommitsOptIn:
    def local(self, repo):
        return repo / ".claude" / "board" / "local.json"

    def test_tb_me_board_commits(self, repo, capsys):
        with_people(repo)
        assert tb(repo, "me", "--board-commits", "on") == 1
        assert "set the person first" in capsys.readouterr().err
        assert tb(repo, "me", "Robin", "--board-commits", "on") == 0
        assert "me = Robin (board commits on)" in capsys.readouterr().out
        assert json.loads(self.local(repo).read_text(encoding="utf-8")) == {"me": "Robin", "board_commits": True}
        assert tb(repo, "me") == 0
        assert capsys.readouterr().out == "Robin\nboard commits on\n"
        assert load_board_config(str(repo / ".claude" / "board" / "board.json"))["board_commits"] is True
        assert tb(repo, "me", "Owens") == 0  # keeps the opt-in
        assert tb(repo, "me", "--board-commits", "off") == 0
        assert json.loads(self.local(repo).read_text(encoding="utf-8")) == {"me": "Owens", "board_commits": False}
        capsys.readouterr()
        assert tb(repo, "me") == 0
        assert capsys.readouterr().out == "Owens\n"
        assert load_board_config(str(repo / ".claude" / "board" / "board.json"))["board_commits"] is False

    def test_tb_me_keeps_the_other_keys(self, repo):
        with_people(repo)
        self.local(repo).write_text(json.dumps({"me": "Alex", "reload": ["notes/x.md"]}), encoding="utf-8")
        assert tb(repo, "me", "Robin", "--board-commits", "on") == 0
        assert json.loads(self.local(repo).read_text(encoding="utf-8")) == {
            "me": "Robin", "reload": ["notes/x.md"], "board_commits": True}

    def test_invalid_board_commits_is_a_config_error(self, repo, capsys):
        with_people(repo)
        self.local(repo).write_text(json.dumps({"me": "Alex", "board_commits": "yes"}), encoding="utf-8")
        assert tb(repo, "check") == 2
        assert "board_commits must be true or false" in capsys.readouterr().err
