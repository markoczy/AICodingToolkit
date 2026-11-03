"""
Tests for the web board and the shared write path: the board's text edits (answer, note), the guarded file
write (hash conflict, read-only source, main-branch guard, paths), the config, the Flask routes and the
renderer. Everything runs on task folders in tmp_path.
"""
import json
import os
import re
import socket
import threading
import time

import pytest

from taskboard import cli, store
from taskboard.config import BoardConfigError, load_board_config, validate_board_config
from taskboard.model import STATES, check_tasks, load_tasks, parse_task
from taskboard.store import content_hash
from taskboard.vcs import git as gitvcs
from taskboard.vcs import svn as svnvcs
from taskboard.textedit import (ConflictError, EditError, ForbiddenError, NotFoundError, add_note,
                                answer_question)
from taskboard.web.app import create_app, search_text
from taskboard.web.options import parse_options, question_texts
from taskboard.web.render import render_markdown

BOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD_WEB_DIR = os.path.join(BOARD_DIR, "taskboard", "web")
PACKAGE_CONFIG = os.path.join(BOARD_DIR, "board.json")


def apply_edit(*args, **kwargs):
    kwargs.setdefault("main_branch", "master")
    return store.apply_edit(*args, **kwargs)


def packaged_config() -> dict:
    """The board.json next to the package, or a skip: the project's build renders it from a template."""
    try:
        return load_board_config(PACKAGE_CONFIG, with_local=False)
    except BoardConfigError:
        pytest.skip("no readable board.json next to the package (the project's build renders it)")

TODAY = "2026-10-08"

TASK = """---
id: T001
title: A task
type: bug
priority: high
owner: null
created: 2026-10-01
updated: 2026-10-02
waiting_on: user     # null | user | supervisor
blocked_by: []
plan: null
branch: null
pr: null
---

## Problem
Something.

## Approach

## Acceptance

## Questions
- **Q1** (answered 2026-10-02, user): Old? → yes
- **Q2** (open, for user): Which one?
- **Q3** (open, for supervisor): Internal?

## Decisions
- 2026-10-02, user: old decision.

## Log
- 2026-10-01 supervisor: created.
"""

ANSWERED_Q2 = """---
id: T001
title: A task
type: bug
priority: high
owner: null
created: 2026-10-01
updated: 2026-10-08
waiting_on: supervisor     # null | user | supervisor
blocked_by: []
plan: null
branch: null
pr: null
---

## Problem
Something.

## Approach

## Acceptance

## Questions
- **Q1** (answered 2026-10-02, user): Old? → yes
- **Q2** (answered 2026-10-08, user, for user): Which one? → the second
- **Q3** (open, for supervisor): Internal?

## Decisions
- 2026-10-02, user: old decision.
- 2026-10-08, user: Q2 → the second

## Log
- 2026-10-01 supervisor: created.
- 2026-10-08 user (board): answered Q2.
"""


def problems_of(text: str) -> list[str]:
    return check_tasks({"states": STATES, "tasks": [parse_task(text, "01-Refinement", "T001-a.md", "x")],
                        "errors": []})


class TestAnswerQuestion:
    def test_exact_file_change(self):
        assert answer_question(TASK, "Q2", "the second", TODAY) == ANSWERED_Q2
        assert problems_of(ANSWERED_Q2) == []

    def test_waiting_on_stays_user_while_another_user_question_is_open(self):
        text = TASK.replace("- **Q3** (open, for supervisor)", "- **Q3** (open, for user)")
        result = answer_question(text, "Q2", "x", TODAY)
        assert "waiting_on: user     # null" in result
        assert "- **Q3** (open, for user): Internal?" in result
        assert problems_of(result) == []

    @pytest.mark.parametrize("qid, message", [("Q1", "already answered"), ("Q9", "no question Q9")])
    def test_refused_questions(self, qid, message):
        with pytest.raises(EditError, match=message):
            answer_question(TASK, qid, "x", TODAY)

    def test_multi_line_question_gets_the_answer_on_its_last_line(self):
        text = TASK.replace("- **Q2** (open, for user): Which one?",
                            "- **Q2** (open, for user): Which one,\n  the first or the second?")
        result = answer_question(text, "Q2", "first", TODAY)
        assert "- **Q2** (answered 2026-10-08, user, for user): Which one,\n  the first or the second? → first\n" \
            in result
        assert problems_of(result) == []

    def test_empty_decisions_section(self):
        text = TASK.replace("## Decisions\n- 2026-10-02, user: old decision.\n", "## Decisions\n")
        result = answer_question(text, "Q2", "a", TODAY)
        assert "## Decisions\n- 2026-10-08, user: Q2 → a\n\n## Log" in result

    def test_crlf_file_keeps_crlf(self):
        result = answer_question(TASK.replace("\n", "\r\n"), "Q2", "the second", TODAY)
        assert result == ANSWERED_Q2.replace("\n", "\r\n")

    def test_mixed_line_endings_change_only_the_edited_lines(self):
        lf_mostly = TASK.replace("Something.\n", "Something.\r\n")
        assert answer_question(lf_mostly, "Q2", "the second", TODAY) == \
            ANSWERED_Q2.replace("Something.\n", "Something.\r\n")
        crlf_mostly = TASK.replace("\n", "\r\n").replace("Something.\r\n", "Something.\n")
        assert answer_question(crlf_mostly, "Q2", "the second", TODAY) == \
            ANSWERED_Q2.replace("\n", "\r\n").replace("Something.\r\n", "Something.\n")

    def test_file_without_final_newline(self):
        result = add_note(TASK.rstrip("\n"), "n", TODAY)
        assert result.endswith("- 2026-10-01 supervisor: created.\n- 2026-10-08 user (board): n")

    def test_control_characters_are_removed(self):
        result = add_note(TASK, "a b\x00c\x1bd\x7fe\tf\x0bg", TODAY)
        assert "user (board): a bcde f g\n" in result

    def test_input_is_one_line(self):
        result = answer_question(TASK, "Q2", "  line one\n\nline   two  ", TODAY)
        assert "Which one? → line one line two\n" in result

    @pytest.mark.parametrize("answer, message", [("   \n ", "empty"), ("x" * 2001, "longer than 2000")])
    def test_invalid_input(self, answer, message):
        with pytest.raises(EditError, match=message):
            answer_question(TASK, "Q2", answer, TODAY)

    def test_markup_in_answer_cannot_start_a_section(self):
        result = answer_question(TASK, "Q2", "a\n## Log\n- injected", TODAY)
        assert result.count("\n## Log\n") == 1 and problems_of(result) == []


class TestAddNote:
    def test_exact_file_change(self):
        text = TASK.replace("waiting_on: user", "waiting_on: executor").replace(
            "- **Q2** (open, for user)", "- **Q2** (open, for supervisor)")
        result = add_note(text, "please also check X", TODAY)
        expected = text.replace("updated: 2026-10-02", "updated: 2026-10-08").replace(
            "waiting_on: executor", "waiting_on: supervisor").replace(
            "- 2026-10-01 supervisor: created.\n",
            "- 2026-10-01 supervisor: created.\n- 2026-10-08 user (board): please also check X\n")
        assert result == expected
        assert problems_of(result) == []

    def test_note_with_open_user_question_keeps_waiting_on_user(self):
        result = add_note(TASK, "a note", TODAY)
        assert "waiting_on: user" in result
        assert problems_of(result) == []

    def test_empty_note_refused(self):
        with pytest.raises(EditError):
            add_note(TASK, "", TODAY)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "Tasks"
    for state in STATES:
        (root / state).mkdir(parents=True)
    (root / "01-Refinement" / "T001-a-task.md").write_bytes(TASK.encode())
    (root / "03-Implementation" / "T002-busy.md").write_bytes(
        TASK.replace("T001", "T002").encode())
    return root


def source(root, editable=True) -> dict:
    return {"name": "public", "root": str(root), "root_path": str(root), "prefix": "T", "editable": editable}


def file_hash(path) -> str:
    return content_hash(path.read_bytes())


class TestApplyEdit:
    def answer(self, text):
        return answer_question(text, "Q2", "the second", TODAY)

    def test_writes_the_edit(self, root):
        path = root / "01-Refinement" / "T001-a-task.md"
        problems = apply_edit(source(root), "01-Refinement", path.name, file_hash(path), self.answer)
        assert path.read_bytes() == ANSWERED_Q2.encode()
        assert problems == []
        assert sorted(os.listdir(root)) == sorted(STATES)

    def test_stale_hash_is_a_conflict_and_leaves_the_file(self, root):
        path = root / "01-Refinement" / "T001-a-task.md"
        with pytest.raises(ConflictError):
            apply_edit(source(root), "01-Refinement", path.name, content_hash(b"older"), self.answer)
        assert path.read_bytes() == TASK.encode()

    def test_read_only_source(self, root):
        path = root / "01-Refinement" / "T001-a-task.md"
        with pytest.raises(ForbiddenError, match="read-only"):
            apply_edit(source(root, editable=False), "01-Refinement", path.name, file_hash(path), self.answer)

    @pytest.mark.parametrize("state, name", [("01-Refinement", "..%2F..%2Fx.md"), ("..", "T001-a-task.md"),
                                             ("01-Refinement", "README.md"), ("01-Refinement", "T009-none.md")])
    def test_paths_outside_task_files_are_refused(self, root, state, name):
        with pytest.raises(NotFoundError):
            apply_edit(source(root), state, name, "", self.answer)

    def test_edit_that_adds_a_problem_is_refused(self, root):
        path = root / "01-Refinement" / "T001-a-task.md"
        with pytest.raises(EditError, match="inconsistent"):
            apply_edit(source(root), "01-Refinement", path.name, file_hash(path),
                       lambda t: t.replace("priority: high", "priority: urgent"))
        assert path.read_bytes() == TASK.encode()

    def test_failed_replace_removes_the_temporary_file(self, root, monkeypatch):
        path = root / "01-Refinement" / "T001-a-task.md"

        def failing_replace(src, dst):
            raise PermissionError("in use")
        monkeypatch.setattr(store.os, "replace", failing_replace)
        with pytest.raises(EditError, match="Could not write"):
            apply_edit(source(root), "01-Refinement", path.name, file_hash(path), self.answer)
        assert path.read_bytes() == TASK.encode()
        assert sorted(os.listdir(root)) == sorted(STATES)


class TestConcurrentWrites:
    def test_second_writer_waits_and_gets_a_conflict(self, root):
        path = root / "01-Refinement" / "T001-a-task.md"
        displayed = file_hash(path)
        first_editing = threading.Event()
        results: dict = {}

        def slow_note(text):
            first_editing.set()
            time.sleep(0.3)
            return add_note(text, "first", TODAY)

        def run(name, edit):
            try:
                apply_edit(source(root), "01-Refinement", path.name, displayed, edit)
                results[name] = "written"
            except ConflictError:
                results[name] = "conflict"

        first = threading.Thread(target=run, args=("first", slow_note))
        first.start()
        assert first_editing.wait(5)
        second = threading.Thread(target=run, args=("second", lambda t: add_note(t, "second", TODAY)))
        second.start()
        first.join(5)
        second.join(5)
        assert results == {"first": "written", "second": "conflict"}
        text = path.read_text(encoding="utf-8")
        assert "user (board): first" in text and "user (board): second" not in text


class TestGitGuard:
    def make_tasks(self, tmp_path):
        tasks = tmp_path / "doc" / "Tasks"
        for state in STATES:
            (tasks / state).mkdir(parents=True)
        path = tasks / "01-Refinement" / "T001-a-task.md"
        path.write_bytes(TASK.encode())
        return tasks, path

    @pytest.mark.parametrize("branch,tracked", [("master", True), (None, False), (None, True),
                                                ("t066-feature", False)])
    def test_master_no_checkout_or_an_untracked_folder_is_allowed(self, tmp_path, monkeypatch, branch, tracked):
        tasks, path = self.make_tasks(tmp_path)
        monkeypatch.setattr(gitvcs, "git_branch", lambda p: branch)
        monkeypatch.setattr(gitvcs, "has_tracked_files", lambda p: tracked)
        apply_edit(source(tasks), "01-Refinement", path.name, file_hash(path),
                   lambda t: add_note(t, "n", TODAY))
        assert "user (board): n" in path.read_text(encoding="utf-8")

    @pytest.mark.parametrize("branch", ["t066-feature", "master-old", "main", "HEAD"])
    def test_other_branches_are_refused(self, tmp_path, monkeypatch, branch):
        tasks, path = self.make_tasks(tmp_path)
        monkeypatch.setattr(gitvcs, "git_branch", lambda p: branch)
        monkeypatch.setattr(gitvcs, "has_tracked_files", lambda p: True)
        with pytest.raises(ForbiddenError, match=r"not of 'master'"):
            apply_edit(source(tasks), "01-Refinement", path.name, file_hash(path),
                       lambda t: add_note(t, "n", TODAY))
        assert path.read_bytes() == TASK.encode()

    def test_outside_any_checkout(self, tmp_path):
        if gitvcs.git_branch(str(tmp_path.parent)) is not None:
            pytest.skip("the temp folder is inside a git checkout")
        assert gitvcs.git_branch(str(tmp_path)) is None


class TestSvnGuard:
    def make_tasks(self, tmp_path):
        tasks = tmp_path / "doc" / "Tasks"
        for state in STATES:
            (tasks / state).mkdir(parents=True)
        path = tasks / "01-Refinement" / "T001-a-task.md"
        path.write_bytes(TASK.encode())
        return tasks, path

    @pytest.mark.parametrize("location", ["^/trunk", "^/trunk/doc/Tasks", None])
    def test_trunk_or_no_working_copy_is_allowed(self, tmp_path, monkeypatch, location):
        tasks, path = self.make_tasks(tmp_path)
        monkeypatch.setattr(svnvcs, "svn_location", lambda p: location)
        apply_edit(source(tasks), "01-Refinement", path.name, file_hash(path),
                   lambda t: add_note(t, "n", TODAY), main_branch="trunk", vcs="svn")
        assert "user (board): n" in path.read_text(encoding="utf-8")

    @pytest.mark.parametrize("location", ["^/branches/t066-feature/doc/Tasks", "^/trunk-old/doc/Tasks",
                                          "^/tags/1.0"])
    def test_other_lines_are_refused(self, tmp_path, monkeypatch, location):
        tasks, path = self.make_tasks(tmp_path)
        monkeypatch.setattr(svnvcs, "svn_location", lambda p: location)
        with pytest.raises(ForbiddenError, match=r"not of '\^/trunk'"):
            apply_edit(source(tasks), "01-Refinement", path.name, file_hash(path),
                       lambda t: add_note(t, "n", TODAY), main_branch="trunk", vcs="svn")
        assert path.read_bytes() == TASK.encode()

    def test_outside_any_working_copy(self, tmp_path):
        assert svnvcs.svn_location(str(tmp_path)) is None

    def test_the_guard_follows_the_configured_vcs(self, tmp_path, monkeypatch):
        """The git guard never runs on an svn board and the other way round."""
        tasks, path = self.make_tasks(tmp_path)
        monkeypatch.setattr(gitvcs, "git_branch", lambda p: "t066-feature")
        monkeypatch.setattr(gitvcs, "has_tracked_files", lambda p: True)
        monkeypatch.setattr(svnvcs, "svn_location", lambda p: "^/branches/x/doc/Tasks")
        with pytest.raises(ForbiddenError, match="git checkout"):
            apply_edit(source(tasks), "01-Refinement", path.name, file_hash(path),
                       lambda t: add_note(t, "n", TODAY), main_branch="master", vcs="git")
        with pytest.raises(ForbiddenError, match="SVN working copy"):
            apply_edit(source(tasks), "01-Refinement", path.name, file_hash(path),
                       lambda t: add_note(t, "n", TODAY), main_branch="trunk", vcs="svn")
        monkeypatch.setattr(gitvcs, "git_branch", lambda p: "master")
        apply_edit(source(tasks), "01-Refinement", path.name, file_hash(path),
                   lambda t: add_note(t, "n", TODAY), main_branch="master", vcs="git")
        assert "user (board): n" in path.read_text(encoding="utf-8")


def config_dict(root, **overrides) -> dict:
    config = {"sources": [{"name": "public", "root": str(root), "editable": True}], "main_branch": "master"}
    config.update(overrides)
    return config


class TestConfig:
    def test_defaults(self, root):
        config = validate_board_config(config_dict(root))
        assert (config["host"], config["port"], config["refresh_seconds"]) == ("127.0.0.1", 5070, 5)
        assert config["sources"][0]["root_path"] == str(root)

    def test_editable_defaults_to_false(self, root):
        config = validate_board_config({"sources": [{"name": "a", "root": str(root)}]})
        assert config["sources"][0]["editable"] is False

    @pytest.mark.parametrize("change, message", [
        ({"colour": "red"}, "Unknown config key"),
        ({"host": "0.0.0.0"}, "loopback"),
        ({"host": "example.com"}, "loopback"),
        ({"port": 0}, "port"),
        ({"port": True}, "port"),
        ({"refresh_seconds": 0}, "refresh_seconds"),
        ({"sources": []}, "non-empty"),
        ({"sources": [{"name": "a b", "root": "x"}]}, "name must match"),
        ({"sources": [{"name": "a", "root": "root:does/not/exist"}]}, "not found"),
        ({"sources": [{"name": "a", "root": "ROOT", "writable": True}]}, "unknown key"),
        ({"sources": [{"name": "a", "root": "ROOT"}, {"name": "a", "root": "ROOT"}]}, "duplicate"),
        ({"sources": [{"name": "a", "root": "ROOT", "editable": "yes"}]}, "editable"),
        ({"sources": [{"name": "a", "root": "ROOT"}, {"name": "b", "root": "ROOT"}]}, "prefix T is used"),
        ({"sources": [{"name": "a", "root": "ROOT", "prefix": "t1"}]}, "uppercase"),
        ({"roles": ["executor"]}, "must contain supervisor, user"),
        ({"types": []}, "types"),
        ({"main_branch": ""}, "main_branch"),
        ({"vcs": "hg"}, "vcs must be one of git, svn"),
        ({"vcs": 1}, "vcs must be one of"),
        ({"ownership": "loose"}, "ownership must be one of strict, open"),
        ({"watch": {"board_idle_minutes": -1}}, "board_idle_minutes"),
        ({"watch": {"quiet": 3}}, "watch"),
        ({"reload": {"budget": 3}}, "reload must be an object"),
        ({"reload": {"budget_bytes": 0}}, "reload.budget_bytes"),
        ({"reload": {"budget_bytes": True}}, "reload.budget_bytes"),
        ({"reload": 5}, "reload must be an object"),
    ])
    def test_invalid(self, root, change, message):
        change = json.loads(json.dumps(change).replace('"ROOT"', json.dumps(str(root))))
        with pytest.raises(BoardConfigError, match=message):
            validate_board_config(config_dict(root, **change))

    def test_reload_budget_is_accepted(self, root):
        validate_board_config(config_dict(root, reload={"budget_bytes": 1000}))

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_hosts(self, root, host):
        assert validate_board_config(config_dict(root, host=host))["host"] == host

    def test_packaged_config(self):
        config = packaged_config()
        assert config["sources"] and config["sources"][0]["editable"]
        assert config["host"] == "127.0.0.1" and config["vcs"] in ("git", "svn")
        assert config["main_branch"] and config["ownership"] in ("strict", "open")

    def test_vcs_and_ownership_defaults(self, root):
        config = validate_board_config(config_dict(root))
        assert config["vcs"] == "git" and config["ownership"] == "strict"
        config = validate_board_config({**config_dict(root), "vcs": "svn", "ownership": "open"})
        assert config["ownership"] == "open" and config["watch"]["board_idle_minutes"] == 15

    def test_paths_are_relative_to_the_repo_root(self, tmp_path):
        (tmp_path / "repo" / "tasks").mkdir(parents=True)
        (tmp_path / "repo" / "cfg").mkdir()
        path = tmp_path / "repo" / "cfg" / "board.json"
        path.write_text(json.dumps({"repo_root": "..", "sources": [{"name": "a", "root": "tasks"}]}),
                        encoding="utf-8")
        config = load_board_config(str(path))
        assert config["sources"][0]["root_path"] == str(tmp_path / "repo" / "tasks")

    def test_missing_file(self, tmp_path):
        with pytest.raises(BoardConfigError):
            load_board_config(str(tmp_path / "missing.json"))


class FixedDate:
    @staticmethod
    def today():
        import datetime
        return datetime.date.fromisoformat(TODAY)


@pytest.fixture
def client(root, tmp_path, monkeypatch):
    monkeypatch.setattr("taskboard.web.app.date", FixedDate)
    private = tmp_path / "Private"
    for state in STATES:
        (private / state).mkdir(parents=True)
    (private / "02-Ready" / "P001-secret.md").write_bytes(
        TASK.replace("T001", "P001").replace("priority: high", "priority: low").encode())
    (private / "00-Conception" / "P002-broken.md").write_text("no header\n", encoding="utf-8")
    config = validate_board_config({"sources": [
        {"name": "public", "root": str(root), "editable": True},
        {"name": "private", "root": str(private), "prefix": "P"},
    ], "main_branch": "master"})
    return LocalClient(create_app(config).test_client())


BASE_URL = "http://127.0.0.1:5070"


class LocalClient:
    """The Flask test client, addressing the board as the browser does (loopback host and its port)."""

    def __init__(self, client):
        self.client = client

    def get(self, url, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        return self.client.get(url, **kwargs)

    def post(self, url, **kwargs):
        kwargs.setdefault("base_url", BASE_URL)
        return self.client.post(url, **kwargs)


def form_fields(html: str) -> dict:
    return {"token": re.search(r'name="token" value="([^"]+)"', html).group(1),
            "hash": re.search(r'name="hash" value="([^"]+)"', html).group(1)}


TASK_URL = "/task/public/01-Refinement/T001-a-task.md"


class TestApp:
    def column(self, html: str, state: str) -> str:
        start = html.index(f"<h2>{state} ")
        end = html.find('<section class="column"', start)
        return html[start:end if end >= 0 else len(html)]

    def test_board_shows_every_task_in_its_column(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "T001" in self.column(html, "01-Refinement")
        assert "T002" in self.column(html, "03-Implementation")
        assert "P001" in self.column(html, "02-Ready")
        assert "P002-broken.md" in self.column(html, "00-Conception")
        assert "private: 00-Conception/P002-broken.md" in html

    def test_card_fields(self, client):
        html = self.column(client.get("/").get_data(as_text=True), "01-Refinement")
        for text in ("A task", "bug", "high", "public", "waiting on you", "2026-10-02",
                     "2026-10-01 supervisor: created."):
            assert text in html

    def test_filters(self, client, root):
        html = client.get("/?source=private").get_data(as_text=True)
        assert "P001" in html and "T001" not in html
        html = client.get("/?priority=low").get_data(as_text=True)
        assert "P001" in html and "T001" not in html
        (root / "02-Ready" / "T003-other.md").write_bytes(
            TASK.replace("T001", "T003").replace("waiting_on: user", "waiting_on: supervisor").replace(
                "(open, for user)", "(open, for supervisor)").encode())
        html = client.get("/?waiting=everyone").get_data(as_text=True)
        assert "T001" in html and "T003" not in html and "P001" in html

    def test_cards_carry_their_search_text(self, client, root):
        path = root / "01-Refinement" / "T001-a-task.md"
        path.write_bytes(TASK.replace("Something.", 'Only-In-Body "quoted" <b>&amp;</b>').encode())
        html = client.get("/").get_data(as_text=True)
        texts = card_search_texts(html)
        assert sorted(texts) == ["P001", "P002-broken.md", "T001", "T002"]
        assert "only-in-body" in texts["T001"] and "only-in-body" not in texts["T002"]
        assert '"quoted"' in texts["T001"] and "<b>&amp;</b>" in texts["T001"]
        assert "<b>" not in html
        assert texts["T001"].startswith("t001 t001-a-task.md a task ")
        assert texts["P002-broken.md"] == "p002-broken.md"

    def test_board_has_the_search_field_in_the_header_outside_the_refreshed_part(self, client):
        html = client.get("/").get_data(as_text=True)
        header = re.search(r'<header class="tb-header">.*?</header>', html, flags=re.DOTALL).group(0)
        assert '<div class="search" hidden>' in header and 'id="search"' in header
        assert html.count('id="search"') == 1
        assert html.index("</header>") < html.index('id="board-live"') < html.index('class="board"')
        assert 'data-state="00-Conception"' in html
        assert 'id="search"' not in client.get(TASK_URL).get_data(as_text=True)

    def test_search_field_is_not_part_of_the_filters_form(self, client):
        html = client.get("/").get_data(as_text=True)
        filters = re.search(r'<form class="filters.*?</form>', html, flags=re.DOTALL).group(0)
        assert 'id="search"' not in filters
        field = re.search(r'<input type="search"[^>]*>', html).group(0)
        assert "name=" not in field and "form=" not in field

    def test_detail_view_escapes_and_offers_forms(self, client, root):
        path = root / "01-Refinement" / "T001-a-task.md"
        path.write_bytes(TASK.replace("Something.", "<script>alert(1)</script>").encode())
        html = client.get(TASK_URL).get_data(as_text=True)
        assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
        assert "Answer Q2" in html and "Answer Q3" in html and "Add note" in html

    def test_no_forms_for_read_only_source_or_locked_state(self, client):
        html = client.get("/task/private/02-Ready/P001-secret.md").get_data(as_text=True)
        assert "read-only" in html and "Add note" not in html
        html = client.get("/task/public/03-Implementation/T002-busy.md").get_data(as_text=True)
        assert "Add note" not in html

    def test_answer_and_note_are_written(self, client, root):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        response = client.post(TASK_URL + "/answer", data={**fields, "question": "Q2", "answer": "the second"})
        assert response.status_code == 302
        path = root / "01-Refinement" / "T001-a-task.md"
        text = path.read_text(encoding="utf-8")
        assert text.replace("updated: 2026-10-08", "").replace("2026-10-08", "D") == \
            ANSWERED_Q2.replace("updated: 2026-10-08", "").replace("2026-10-08", "D").replace(
                "updated: " + TODAY, "") or "Which one? → the second" in text
        assert "Answer saved." in client.get(response.headers["Location"]).get_data(as_text=True)

        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        assert client.post(TASK_URL + "/note", data={**fields, "note": "thanks"}).status_code == 302
        assert "user (board): thanks" in path.read_text(encoding="utf-8")
        assert check_tasks(load_tasks(str(root))) == []

    def test_stale_hash_keeps_the_typed_text(self, client, root):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        path = root / "01-Refinement" / "T001-a-task.md"
        path.write_bytes(TASK.replace("Something.", "Changed meanwhile.").encode())
        response = client.post(TASK_URL + "/note", data={**fields, "note": "my long note"})
        assert response.status_code == 409
        html = response.get_data(as_text=True)
        assert "changed since you opened it" in html and "my long note" in html and "Changed meanwhile." in html
        assert "my long note" not in path.read_text(encoding="utf-8")

    def test_bad_token_is_refused(self, client, root):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        response = client.post(TASK_URL + "/note", data={**fields, "token": "guess", "note": "x"})
        assert response.status_code == 403
        assert (root / "01-Refinement" / "T001-a-task.md").read_bytes() == TASK.encode()

    @pytest.mark.parametrize("base_url, status", [
        ("http://127.0.0.1:5070", 200), ("http://localhost:5070", 200), ("http://[::1]:5070", 200),
        ("http://attacker.example:5070", 403), ("http://127.0.0.1:5071", 403), ("http://localhost", 403),
    ])
    def test_host_and_port_are_checked(self, client, base_url, status):
        assert client.get("/", base_url=base_url).status_code == status

    def test_request_body_is_limited(self, client, root):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        response = client.post(TASK_URL + "/note", data={**fields, "note": "x" * 70000})
        assert response.status_code == 413
        assert (root / "01-Refinement" / "T001-a-task.md").read_bytes() == TASK.encode()

    def test_unknown_source_or_file(self, client):
        assert client.get("/task/nope/01-Refinement/T001-a-task.md").status_code == 404
        assert client.get("/task/public/01-Refinement/T999-none.md").status_code == 404
        assert client.get("/task/public/Other/T001-a-task.md").status_code == 404

    def test_fingerprint_changes_with_the_files(self, client, root):
        first = client.get("/api/fingerprint").get_json()["fingerprint"]
        assert client.get("/api/fingerprint").get_json()["fingerprint"] == first
        (root / "02-Ready" / "T003-new.md").write_bytes(TASK.replace("T001", "T003").encode())
        assert client.get("/api/fingerprint").get_json()["fingerprint"] != first


PEOPLE = ["Alex", "Robin", "Owens"]


class TestAnswerAsPerson:
    """textedit: anyone may answer any open question; the answer is recorded under their name."""

    def test_person_answers_a_user_question(self):
        result = answer_question(TASK, "Q2", "the second", TODAY, by="Robin", actor="Robin (board)")
        assert result == ANSWERED_Q2.replace("(answered 2026-10-08, user, for user)",
                                             "(answered 2026-10-08, Robin, for user)").replace(
            "- 2026-10-08, user: Q2", "- 2026-10-08, Robin: Q2").replace(
            "- 2026-10-08 user (board): answered Q2.", "- 2026-10-08 Robin (board): answered Q2.")
        assert problems_of_people(result) == []

    def test_person_answers_a_supervisor_question(self):
        result = answer_question(TASK, "Q3", "no", TODAY, by="Alex", actor="Alex (board)")
        assert "- **Q3** (answered 2026-10-08, Alex, for supervisor): Internal? → no\n" in result
        assert "- 2026-10-08, Alex: Q3 → no\n" in result
        assert result.endswith("- 2026-10-08 Alex (board): answered Q3.\n")
        assert "waiting_on: user" in result  # Q2 for the user is still open
        assert problems_of_people(result) == []

    def test_recorded_by_someone_else(self):
        result = answer_question(TASK, "Q2", "x", TODAY, by="Alex", actor="supervisor", via="chat")
        assert result.endswith("- 2026-10-08 supervisor: recorded Alex's answer to Q2 (chat).\n")
        result = answer_question(TASK, "Q2", "x", TODAY, by="Robin", actor="coordinator (Robin)")
        assert result.endswith("- 2026-10-08 coordinator (Robin): recorded Robin's answer to Q2.\n")

    def test_already_answered(self):
        with pytest.raises(EditError, match="Q1 is already answered"):
            answer_question(TASK, "Q1", "x", TODAY, by="Robin", actor="Robin (board)")
        answered = answer_question(TASK, "Q2", "x", TODAY, by="Robin", actor="Robin (board)")
        with pytest.raises(EditError, match="Q2 is already answered"):
            answer_question(answered, "Q2", "y", TODAY, by="Alex", actor="Alex (board)")

    def test_multi_line_question_and_crlf(self):
        text = TASK.replace("- **Q3** (open, for supervisor): Internal?",
                            "- **Q3** (open, for supervisor): Internal,\n  or external?").replace("\n", "\r\n")
        result = answer_question(text, "Q3", "internal", TODAY, by="Owens", actor="Owens (board)")
        expected = text.replace("updated: 2026-10-02", "updated: 2026-10-08").replace(
            "- **Q3** (open, for supervisor): Internal,\r\n  or external?",
            "- **Q3** (answered 2026-10-08, Owens, for supervisor): Internal,\r\n  or external? → internal").replace(
            "- 2026-10-02, user: old decision.\r\n",
            "- 2026-10-02, user: old decision.\r\n- 2026-10-08, Owens: Q3 → internal\r\n").replace(
            "- 2026-10-01 supervisor: created.\r\n",
            "- 2026-10-01 supervisor: created.\r\n- 2026-10-08 Owens (board): answered Q3.\r\n")
        assert result == expected

    def test_note_with_a_name(self):
        result = add_note(TASK, "please do T015 first", TODAY, actor="Alex (board)")
        assert result.endswith("- 2026-10-01 supervisor: created.\n"
                               "- 2026-10-08 Alex (board): please do T015 first\n")


def problems_of_people(text: str) -> list[str]:
    schema = {"types": ["bug"], "roles": ["user", "supervisor"], "people": PEOPLE}
    return check_tasks({"states": STATES, "tasks": [parse_task(text, "01-Refinement", "T001-a.md", "x")],
                        "errors": []}, schema=schema)


class TestPeopleConfig:
    def test_people_default_to_none(self, root):
        config = validate_board_config(config_dict(root))
        assert config["people"] == [] and config["me"] is None

    def test_people(self, root):
        assert validate_board_config(config_dict(root, people=PEOPLE))["people"] == PEOPLE

    @pytest.mark.parametrize("people, message", [
        (["user"], "must not repeat a role: user"),
        (["Robin", "Robin"], "Robin more than once"),
        (["Anna Maria"], "single words"),
        (["Anna:"], "single words"),
        ("Robin", "single words"),
        ([""], "single words"),
    ])
    def test_invalid_people(self, root, people, message):
        with pytest.raises(BoardConfigError, match=message):
            validate_board_config(config_dict(root, people=people))

    def write_config(self, tmp_path, root, local=None):
        folder = tmp_path / "cfg"
        folder.mkdir()
        (folder / "board.json").write_text(json.dumps(config_dict(root, people=PEOPLE)), encoding="utf-8")
        if local is not None:
            (folder / "local.json").write_text(local, encoding="utf-8")
        return str(folder / "board.json")

    def test_local_json_missing_is_fine(self, tmp_path, root):
        assert load_board_config(self.write_config(tmp_path, root))["me"] is None

    def test_local_json_names_me(self, tmp_path, root):
        assert load_board_config(self.write_config(tmp_path, root, '{"me": "Robin"}'))["me"] == "Robin"

    def test_local_json_accepts_the_reload_list(self, tmp_path, root):
        config = load_board_config(self.write_config(tmp_path, root, '{"me": "Robin", "reload": ["a.md", "d/b.md"]}'))
        assert config["me"] == "Robin"

    @pytest.mark.parametrize("local, message", [
        ('{"me": "Bob"}', "me must be one of the people"),
        ('{"me": "Robin", "colour": 1}', "unknown key"),
        ('["Robin"]', "must be an object"),
        ("{not json", "Cannot read"),
        ('{"reload": "x.md"}', "reload must be a list"),
        ('{"reload": [1]}', "reload entries must be non-empty strings"),
        ('{"reload": ["/etc/x.md"]}', "must be repo-relative"),
        ('{"reload": ["a/../../x.md"]}', "must be repo-relative"),
        ('{"reload": ["\\\\x.md"]}', "must be repo-relative"),
        ('{"reload": ["C:x.md"]}', "must be repo-relative"),
        ('{"reload": ["C:/x.md"]}', "must be repo-relative"),
    ])
    def test_invalid_local_json(self, tmp_path, root, local, message):
        with pytest.raises(BoardConfigError, match=message):
            load_board_config(self.write_config(tmp_path, root, local))

    def test_default_owner(self, root):
        assert validate_board_config(config_dict(root, people=PEOPLE))["default_owner"] is None
        config = validate_board_config(config_dict(root, people=PEOPLE, default_owner="Alex"))
        assert config["default_owner"] == "Alex"
        with pytest.raises(BoardConfigError, match="default_owner must be one of the people"):
            validate_board_config(config_dict(root, people=PEOPLE, default_owner="Bob"))
        with pytest.raises(BoardConfigError, match=r"default_owner must be one of the people \(none configured\)"):
            validate_board_config(config_dict(root, default_owner="Alex"))

    def test_packaged_people(self):
        config = packaged_config()
        assert isinstance(config["people"], list)
        assert config["default_owner"] is None or config["default_owner"] in config["people"]


def people_app(root, monkeypatch, people=PEOPLE, me=None):
    monkeypatch.setattr("taskboard.web.app.date", FixedDate)
    config = validate_board_config(config_dict(root, people=people))
    config["me"] = me
    return LocalClient(create_app(config).test_client())


def answering_as(html: str) -> list[str]:
    """The "Answering as" lines of every form on the page."""
    return re.findall(r'<p class="answering-as tb-muted">(.*?)</p>', html, flags=re.DOTALL)


OPTION_QUESTION = ("Hit definition: (a) score >= 80, with 70 and 90 as sensitivity (recommended, because it is "
                   "the stricter one) (b) top 10 per tier per day (c) score >= 70")


class TestOptionParser:
    def test_three_options_and_the_text_before_them(self):
        intro, options = parse_options(OPTION_QUESTION)
        assert intro == "Hit definition:"
        assert [(o.letter, o.text, o.recommended) for o in options] == [
            ("a", "score >= 80, with 70 and 90 as sensitivity", True),
            ("b", "top 10 per tier per day", False),
            ("c", "score >= 70", False)]

    def test_recommended_marker_with_nested_parentheses_and_in_any_option(self):
        _, options = parse_options("Which? (a) one (b) two (recommended by the planner: the (cheaper) one; "
                                   "reusable) (c) three")
        assert [(o.letter, o.text, o.recommended) for o in options] == [
            ("a", "one", False), ("b", "two", True), ("c", "three", False)]

    def test_options_spanning_lines(self):
        section = ("- **Q2** (open, for user): Which one? (a) the first,\n  which is long (recommended)\n"
                   "  (b) the second\n- **Q3** (open, for user): Other?")
        texts = question_texts(section)
        assert texts == {"Q2": "Which one? (a) the first, which is long (recommended) (b) the second",
                         "Q3": "Other?"}
        intro, options = parse_options(texts["Q2"])
        assert intro == "Which one?"
        assert [(o.letter, o.text, o.recommended) for o in options] == [
            ("a", "the first, which is long", True), ("b", "the second", False)]

    @pytest.mark.parametrize("text", [
        "Which one?", "Only (a) one option", "Letters (b) out (c) of order", "Glued (a)one (b)two",
        "Empty (a) (b) option", "A (a) case but no b"])
    def test_no_options(self, text):
        assert parse_options(text)[1] == []

    def test_a_reference_before_the_options_is_skipped(self):
        intro, options = parse_options("In case (a) of T1: (a) yes (b) no")
        assert intro == "In case (a) of T1:" and [o.letter for o in options] == ["a", "b"]


OPTIONS_TASK = TASK.replace(
    "- **Q2** (open, for user): Which one?",
    "- **Q2** (open, for user): Pick one: (a) approve and start (recommended, because it is quick)\n"
    "  (b) approve, start later (c) drop it")


class TestOptionAnswers:
    @pytest.fixture
    def path(self, root):
        path = root / "01-Refinement" / "T001-a-task.md"
        path.write_bytes(OPTIONS_TASK.encode())
        return path

    def test_three_options_render_three_buttons_and_a_comment_field(self, client, path):
        html = client.get(TASK_URL).get_data(as_text=True)
        form = html[html.index('data-form="answer:Q2"'):html.index('data-form="answer:Q3"')]
        assert len(re.findall(r'<button type="submit" name="option"', form)) == 3
        for letter, text in [("a", "approve and start"), ("b", "approve, start later"), ("c", "drop it")]:
            assert f'name="option" value="{letter}"' in form and text in form
        assert form.count("option-recommended") == 1 and "(recommended" not in form
        assert 'name="comment"' in form and 'name="answer"' not in form
        assert "Pick one:" in form
        # Q3 has no options and keeps the free-text form.
        assert 'name="answer"' in html[html.index('data-form="answer:Q3"'):]

    def test_option_with_a_comment_is_recorded(self, client, path):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        response = client.post(TASK_URL + "/answer", data={**fields, "question": "Q2", "option": "b",
                                                           "comment": "after the\n holidays"})
        assert response.status_code == 302
        text = path.read_text(encoding="utf-8")
        answer = "b: approve, start later; after the holidays"
        assert f"(answered 2026-10-08, user, for user): Pick one: (a) approve and start (recommended, because it " \
               f"is quick)\n  (b) approve, start later (c) drop it → {answer}\n" in text
        assert f"- 2026-10-08, user: Q2 → {answer}\n" in text
        assert text.endswith("- 2026-10-08 user (board): answered Q2.\n")
        assert check_tasks(load_tasks(str(path.parent.parent))) == []

    def test_option_without_a_comment_and_the_recommended_one(self, client, path):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        assert client.post(TASK_URL + "/answer", data={**fields, "question": "Q2", "option": "a",
                                                       "comment": "  "}).status_code == 302
        assert "→ a: approve and start\n" in path.read_text(encoding="utf-8")

    def test_free_text_for_a_question_without_options(self, client, path):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        assert client.post(TASK_URL + "/answer", data={**fields, "question": "Q3", "answer": "no, internal"}
                           ).status_code == 302
        assert "Internal? → no, internal\n" in path.read_text(encoding="utf-8")

    def test_answered_as_the_checkouts_person(self, root, path, monkeypatch):
        client = people_app(root, monkeypatch, me="Robin")
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        assert client.post(TASK_URL + "/answer", data={**fields, "question": "Q2", "option": "c"}
                           ).status_code == 302
        text = path.read_text(encoding="utf-8")
        assert "(answered 2026-10-08, Robin, for user)" in text and "- 2026-10-08, Robin: Q2 → c: drop it\n" in text

    @pytest.mark.parametrize("data", [{"question": "Q2", "option": "d"}, {"question": "Q3", "option": "a"}])
    def test_unknown_option_is_refused_and_the_comment_kept(self, client, path, data):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        before = path.read_bytes()
        response = client.post(TASK_URL + "/answer", data={**fields, **data, "comment": "my comment"})
        assert response.status_code == 400
        assert path.read_bytes() == before
        assert "my comment" in response.get_data(as_text=True)

    def test_stale_hash_is_refused(self, client, path):
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        path.write_bytes(OPTIONS_TASK.replace("Something.", "Changed.").encode())
        response = client.post(TASK_URL + "/answer", data={**fields, "question": "Q2", "option": "a"})
        assert response.status_code == 409 and "→ a:" not in path.read_text(encoding="utf-8")


class TestBoardAnswerer:
    def test_all_open_questions_with_their_role_user_questions_first(self, root, monkeypatch):
        path = root / "01-Refinement" / "T001-a-task.md"
        path.write_bytes(TASK.replace("- **Q2** (open, for user): Which one?\n- **Q3** (open, for supervisor): "
                                      "Internal?", "- **Q2** (open, for supervisor): Internal?\n"
                                      "- **Q3** (open, for user): Which one?").encode())
        html = people_app(root, monkeypatch).get(TASK_URL).get_data(as_text=True)
        assert html.index("Answer Q3") < html.index("Answer Q2")
        assert '<span class="tb-badge tb-badge-accent">for user</span> Which one?' in html
        assert '<span class="tb-badge">for supervisor</span> Internal?' in html
        assert "Answer Q1" not in html

    def test_answer_is_written_as_the_checkouts_person(self, root, monkeypatch):
        client = people_app(root, monkeypatch, me="Robin")
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        response = client.post(TASK_URL + "/answer",
                               data={**fields, "question": "Q2", "answer": "the second", "by": "Owens"})
        assert response.status_code == 302
        text = (root / "01-Refinement" / "T001-a-task.md").read_text(encoding="utf-8")
        assert "- **Q2** (answered 2026-10-08, Robin, for user): Which one? → the second\n" in text
        assert "- 2026-10-08, Robin: Q2 → the second\n" in text
        assert text.endswith("- 2026-10-08 Robin (board): answered Q2.\n")
        assert "Set-Cookie" not in response.headers
        assert check_tasks(load_tasks(str(root)), schema={"types": ["bug"], "roles": ["user", "supervisor"],
                                                          "people": PEOPLE}) == []

    def test_answer_to_a_supervisor_question(self, root, monkeypatch):
        client = people_app(root, monkeypatch, me="Owens")
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        assert client.post(TASK_URL + "/answer", data={**fields, "question": "Q3", "answer": "no"}
                           ).status_code == 302
        text = (root / "01-Refinement" / "T001-a-task.md").read_text(encoding="utf-8")
        assert "- **Q3** (answered 2026-10-08, Owens, for supervisor): Internal? → no\n" in text
        assert "waiting_on: user" in text

    def test_note_with_a_name(self, root, monkeypatch):
        client = people_app(root, monkeypatch, me="Alex")
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        response = client.post(TASK_URL + "/note", data={**fields, "note": "do T015 first"})
        assert response.status_code == 302
        text = (root / "01-Refinement" / "T001-a-task.md").read_text(encoding="utf-8")
        assert text.endswith("- 2026-10-08 Alex (board): do T015 first\n")

    @pytest.mark.parametrize("kind", ["answer", "note"])
    def test_without_tb_me_the_board_refuses_to_write_and_keeps_the_text(self, root, monkeypatch, kind):
        client = people_app(root, monkeypatch)
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        data = {**fields, "question": "Q2", "answer": "my typed answer"} if kind == "answer" else {
            **fields, "note": "my typed answer"}
        response = client.post(f"{TASK_URL}/{kind}", data=data)
        assert response.status_code == 400
        html = response.get_data(as_text=True)
        assert "python .claude/board/tb.py me &lt;name&gt;" in html or "python .claude/board/tb.py me <name>" in html
        assert "my typed answer" in html
        assert (root / "01-Refinement" / "T001-a-task.md").read_bytes() == TASK.encode()

    def test_a_posted_name_or_cookie_is_ignored(self, root, monkeypatch):
        client = people_app(root, monkeypatch, me="Robin")
        client.client.set_cookie("tb_me", "Owens", domain="127.0.0.1")
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        response = client.post(TASK_URL + "/note", data={**fields, "note": "n", "by": "Owens"})
        assert response.status_code == 302 and "Set-Cookie" not in response.headers
        text = (root / "01-Refinement" / "T001-a-task.md").read_text(encoding="utf-8")
        assert text.endswith("- 2026-10-08 Robin (board): n\n")

    def test_answering_as_is_shown_read_only(self, root, monkeypatch):
        html = people_app(root, monkeypatch).get(TASK_URL).get_data(as_text=True)
        assert 'name="by"' not in html and "You: not set" in html
        assert all("not set" in line for line in answering_as(html)) and len(answering_as(html)) == 3
        client = people_app(root, monkeypatch, me="Robin")
        client.client.set_cookie("tb_me", "Owens", domain="127.0.0.1")
        html = client.get(TASK_URL).get_data(as_text=True)
        assert answering_as(html) == ["Answering as Robin"] * 3 and "You: Robin" in html
        assert 'name="by"' not in html and "You: Robin" in client.get("/").get_data(as_text=True)

    def test_without_people_the_board_writes_user_board_lines(self, client, root):
        """Regression: with no people in board.json there is no name choice, and the board writes exactly the
        lines it wrote before answerers were named (the question line in the new form)."""
        html = client.get(TASK_URL).get_data(as_text=True)
        assert 'name="by"' not in html and "You:" not in html
        fields = form_fields(html)
        response = client.post(TASK_URL + "/answer", data={**fields, "question": "Q2", "answer": "the second",
                                                           "by": "Robin"})
        assert response.status_code == 302 and "Set-Cookie" not in response.headers
        path = root / "01-Refinement" / "T001-a-task.md"
        assert path.read_text(encoding="utf-8") == ANSWERED_Q2
        fields = form_fields(client.get(TASK_URL).get_data(as_text=True))
        assert client.post(TASK_URL + "/note", data={**fields, "note": "thanks"}).status_code == 302
        assert path.read_text(encoding="utf-8").endswith(
            "- 2026-10-08 user (board): answered Q2.\n- 2026-10-08 user (board): thanks\n")


OWNERS = ["Alex", "Robin"]


def owner_app(root, monkeypatch, me=None):
    monkeypatch.setattr("taskboard.web.app.date", FixedDate)
    config = validate_board_config(config_dict(root, people=OWNERS, default_owner="Alex"))
    config["me"] = me
    return LocalClient(create_app(config).test_client())


def cards(html: str) -> dict[str, str]:
    """The HTML of every card on the board by task id."""
    out = {}
    for chunk in html.split('<a class="card')[1:]:
        out[re.search(r'<span class="id">([^<]+)</span>', chunk).group(1)] = chunk.split("</a>")[0]
    return out


class TestOwnerOnTheBoard:
    @pytest.fixture
    def owned(self, root):
        """T001: null owner, unaddressed user question; T003: Robin's, unaddressed user question; T004: null
        owner, a user question addressed to Robin; T005: Robin's, priority low, no question."""
        (root / "01-Refinement" / "T003-hers.md").write_bytes(
            TASK.replace("T001", "T003").replace("owner: null", "owner: Robin").encode())
        (root / "01-Refinement" / "T004-addressed.md").write_bytes(
            TASK.replace("T001", "T004").replace("Which one?", "@Robin: Which one?").encode())
        (root / "02-Ready" / "T005-quiet.md").write_bytes(
            TASK.replace("T001", "T005").replace("owner: null", "owner: Robin").replace(
                "priority: high", "priority: low").replace("waiting_on: user", "waiting_on: null").replace(
                "- **Q2** (open, for user): Which one?\n", "").encode())
        return root

    def test_card_label_and_owner_filter(self, owned, monkeypatch):
        client = owner_app(owned, monkeypatch)
        html = client.get("/").get_data(as_text=True)
        assert '<select name="owner">' in html
        by_id = cards(html)
        assert "<span>owner Robin</span>" in by_id["T003"] and "<span>owner" not in by_id["T001"]
        assert set(cards(client.get("/?owner=Alex").get_data(as_text=True))) == {"T001", "T002", "T004"}
        assert set(cards(client.get("/?owner=Robin").get_data(as_text=True))) == {"T003", "T005"}
        assert set(cards(client.get("/?owner=Robin&priority=high").get_data(as_text=True))) == {"T003"}
        assert set(cards(client.get("/?owner=Bob").get_data(as_text=True))) == {"T001", "T002", "T003", "T004",
                                                                                "T005"}
        assert '<option value="Robin" selected>' in client.get("/?owner=Robin").get_data(as_text=True)

    def test_no_owner_filter_without_people(self, client):
        assert 'name="owner"' not in client.get("/").get_data(as_text=True)

    def test_task_page_owner_line(self, owned, monkeypatch):
        client = owner_app(owned, monkeypatch)
        html = client.get("/task/public/01-Refinement/T003-hers.md").get_data(as_text=True)
        assert "01-Refinement / T003-hers.md · owner Robin</p>" in html
        html = client.get(TASK_URL).get_data(as_text=True)
        assert "01-Refinement / T001-a-task.md · owner Alex (default)</p>" in html

    def test_search_finds_the_owner(self, owned, monkeypatch):
        texts = card_search_texts(owner_app(owned, monkeypatch).get("/").get_data(as_text=True))
        assert "robin" in texts["T003"].split() and "robin" not in texts["T001"].split()

    def test_waiting_on_me_follows_the_checkouts_person(self, owned, monkeypatch):
        client = owner_app(owned, monkeypatch, me="Alex")
        html = client.get("/?waiting=me").get_data(as_text=True)
        assert set(cards(html)) == {"T001", "T002"}
        all_cards = cards(client.get("/").get_data(as_text=True))
        assert "waiting on you" in all_cards["T001"] and "waiting on you" not in all_cards["T003"]
        assert "<span>waiting on user</span>" in all_cards["T003"]
        client = owner_app(owned, monkeypatch, me="Robin")
        client.client.set_cookie("tb_me", "Alex", domain="127.0.0.1")
        assert set(cards(client.get("/?waiting=me").get_data(as_text=True))) == {"T003", "T004"}

    def test_waiting_on_everyone_is_every_open_user_question(self, owned, monkeypatch):
        for me in ("Alex", "Robin", None):
            html = owner_app(owned, monkeypatch, me=me).get("/?waiting=everyone").get_data(as_text=True)
            assert set(cards(html)) == {"T001", "T002", "T003", "T004"}

    def test_waiting_on_me_without_tb_me_is_any_user_question(self, owned, monkeypatch):
        html = owner_app(owned, monkeypatch).get("/?waiting=me").get_data(as_text=True)
        assert set(cards(html)) == {"T001", "T002", "T003", "T004"}

    def test_waiting_dropdown_and_its_selection(self, owned, monkeypatch):
        client = owner_app(owned, monkeypatch, me="Alex")
        html = client.get("/").get_data(as_text=True)
        assert '<select name="waiting">' in html and 'type="checkbox"' not in html
        assert set(cards(html)) == {"T001", "T002", "T003", "T004", "T005"}
        assert '<option value="everyone" selected>' in client.get("/?waiting=everyone").get_data(as_text=True)
        assert '<option value="me" selected>' in client.get("/?waiting=me").get_data(as_text=True)
        assert '<option value="me" selected>' in client.get("/?waiting=user").get_data(as_text=True)


def live_data(html: str) -> dict[str, str]:
    """The data-* attributes of the task page's #task-live wrapper."""
    tag = re.search(r'<div id="task-live"([^>]*)>', html).group(1)
    return dict(re.findall(r'data-([a-z-]+)="([^"]*)"', tag))


class TestTaskPageRefresh:
    """What the task page's in-place refresh relies on: the lookup by task id and the wrapper's data."""

    def test_task_page_carries_its_urls_and_version(self, client):
        html = client.get(TASK_URL).get_data(as_text=True)
        data = live_data(html)
        assert data == {"task-url": TASK_URL, "locate-url": "/task/public/T001", "state": "01-Refinement",
                        "hash": form_fields(html)["hash"]}
        assert html.index('id="task-head"') < html.index('id="task-live"') < html.index('class="input')
        assert 'data-form="answer:Q2"' in html and 'data-form="note"' in html

    def test_messages_stay_outside_the_refreshed_part(self, client):
        html = client.get(TASK_URL + "?saved=note").get_data(as_text=True)
        assert html.index("Note saved.") < html.index('id="task-live"')

    def test_lookup_redirects_to_the_task_page(self, client):
        response = client.get("/task/public/T001")
        assert response.status_code == 302 and response.headers["Location"] == TASK_URL
        response = client.get("/task/private/P002")
        assert response.headers["Location"] == "/task/private/00-Conception/P002-broken.md"

    def test_lookup_follows_a_moved_or_renamed_task(self, client, root):
        os.replace(root / "01-Refinement" / "T001-a-task.md", root / "02-Ready" / "T001-renamed.md")
        response = client.get("/task/public/T001")
        assert response.headers["Location"] == "/task/public/02-Ready/T001-renamed.md"
        assert client.get(TASK_URL).status_code == 404
        assert live_data(client.get(response.headers["Location"]).get_data(as_text=True))["state"] == "02-Ready"

    def test_lookup_of_a_missing_or_doubled_task(self, client, root):
        assert client.get("/task/public/T999").status_code == 404
        assert client.get("/task/nope/T001").status_code == 404
        (root / "07-Done" / "T001-copy.md").write_bytes(TASK.encode())
        response = client.get("/task/public/T001")
        assert response.status_code == 409 and "07-Done/T001-copy.md" in response.get_data(as_text=True)

    def test_lookup_ignores_files_that_only_start_alike(self, client, root):
        (root / "02-Ready" / "T0010-other.md").write_bytes(TASK.replace("T001", "T0010").encode())
        (root / "02-Ready" / "T001-notes.txt").write_text("x", encoding="utf-8")
        assert client.get("/task/public/T001").headers["Location"] == TASK_URL

    def test_answer_after_a_swap_uses_the_new_version(self, client, root):
        """The page swaps in the fetched content's hash: a post with it is accepted, one with the old refused."""
        old = form_fields(client.get(TASK_URL).get_data(as_text=True))
        path = root / "01-Refinement" / "T001-a-task.md"
        path.write_bytes(TASK.replace("Something.", "Changed meanwhile.").encode())
        swapped = client.get(client.get("/task/public/T001").headers["Location"]).get_data(as_text=True)
        new = form_fields(swapped)
        assert new["hash"] != old["hash"] and live_data(swapped)["hash"] == new["hash"]
        refused = client.post(TASK_URL + "/answer", data={**old, "question": "Q2", "answer": "kept text"})
        assert refused.status_code == 409 and "kept text" in refused.get_data(as_text=True)
        accepted = client.post(TASK_URL + "/answer", data={**new, "question": "Q2", "answer": "the second"})
        assert accepted.status_code == 302
        assert "the second" in path.read_text(encoding="utf-8")
        assert "Changed meanwhile." in path.read_text(encoding="utf-8")


def card_search_texts(html: str) -> dict[str, str]:
    """The data-search text of every card on the board, by the card's id (HTML entities decoded)."""
    import html as html_lib
    out = {}
    for m in re.finditer(r'<a class="card[^"]*"\s+data-search="([^"]*)".*?<span class="id">([^<]*)</span>',
                         html, flags=re.DOTALL):
        out[html_lib.unescape(m.group(2))] = html_lib.unescape(m.group(1))
    return out


class TestSearchText:
    def task(self, text: str, file: str = "T001-a-task.md") -> dict:
        return parse_task(text, "01-Refinement", file, "x")

    def test_id_file_title_and_every_section(self):
        text = search_text(self.task(TASK))
        for word in ("t001", "t001-a-task.md", "a", "task", "something.", "internal?", "old", "decision.",
                     "created.", "problem", "log"):
            assert word in text.split(), word

    def test_lowercased_and_each_word_once(self):
        text = search_text(self.task(TASK.replace("Something.", "Keyword KEYWORD keyword other")))
        words = text.split()
        assert "keyword" in words and words.count("keyword") == 1
        assert text == text.lower()
        assert len(words) == len(set(words))

    def test_substring_of_the_full_text_is_a_substring_of_the_search_text(self):
        task = self.task(TASK.replace("Something.", "The yf_trailing_pe field\n  and data_access/sqlite.py"))
        text = search_text(task)
        for query in ("trailing_pe", "access/sql", "yf_", ".py", "field"):
            assert query in text

    def test_unreadable_file_still_matches_its_file_name(self):
        assert search_text(parse_task(None, "00-Conception", "P002-broken.md", "x")) == "p002-broken.md"


_COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|\b(?:rgba?|hsla?)\(|style=|"
                             r"(?<![\w-])(?:white|black|red|green|blue)(?![\w-])")


class TestSharedStyles:
    def test_pages_load_the_base_stylesheet_before_the_board_rules(self, client):
        for url in ("/", TASK_URL):
            html = client.get(url).get_data(as_text=True)
            base = html.find('href="/static/base.css"')
            board = html.find('href="/static/board.css"')
            assert 0 < base < board, url

    def test_static_files_are_served(self, client):
        response = client.get("/static/board.css")
        assert response.status_code == 200 and response.mimetype == "text/css"

    def test_static_files_also_need_a_loopback_host(self, client):
        assert client.get("/static/board.css", base_url="http://attacker.example:5070").status_code == 403

    @pytest.mark.parametrize("name", ["static/board.css", "templates/base.html", "templates/board.html",
                                      "templates/task.html"])
    def test_no_colours_or_inline_styles_in_board_files(self, name):
        with open(os.path.join(BOARD_WEB_DIR, name), encoding="utf-8") as f:
            text = re.sub(r"/\*.*?\*/", "", f.read(), flags=re.DOTALL)
        assert _COLOUR_LITERAL.findall(text) == []

    def test_board_css_declares_no_tokens_of_its_own(self):
        with open(os.path.join(BOARD_WEB_DIR, "static", "board.css"), encoding="utf-8") as f:
            css = f.read()
        assert ":root" not in css and "prefers-color-scheme" not in css


class TestRender:
    def test_subset(self):
        html = str(render_markdown("## Head\nText **bold** and `<code>`.\n\n- item one\n  continued\n- two\n"))
        assert "<h2>Head</h2>" in html
        assert "<p>Text <strong>bold</strong> and <code>&lt;code&gt;</code>.</p>" in html
        assert "<ul><li>item one continued</li><li>two</li></ul>" in html

    def test_html_is_escaped(self):
        assert "<img" not in str(render_markdown("<img src=x onerror=alert(1)>"))

    def test_fenced_block(self):
        assert "<pre><code>a &lt; b\n**x**</code></pre>" in str(render_markdown("```\na < b\n**x**\n```"))


class TestServe:
    def test_bad_config_returns_two(self, tmp_path):
        path = tmp_path / "board.json"
        path.write_text(json.dumps({"sources": [], "host": "127.0.0.1"}), encoding="utf-8")
        assert cli.main(["--config", str(path), "serve"]) == 2

    def test_valid_config_runs_the_app(self, root, tmp_path, monkeypatch):
        path = tmp_path / "board.json"
        path.write_text(json.dumps(config_dict(root, port=5999)), encoding="utf-8")
        calls = []
        monkeypatch.setattr(cli, "run_board", lambda app, host, port, *a: calls.append({"host": host, "port": port}))
        assert cli.main(["--config", str(path), "serve", "--no-update"]) == 0
        assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 5999

    def test_bind_failure_returns_one(self, root, tmp_path, monkeypatch, capsys):
        with socket.socket() as held:  # a port this test reserves itself, never the default 5070
            held.bind(("127.0.0.1", 0))
            port = held.getsockname()[1]
            path = tmp_path / "board.json"
            path.write_text(json.dumps(config_dict(root, port=port)), encoding="utf-8")

            def fail(*args):
                raise OSError("address in use")
            monkeypatch.setattr(cli, "run_board", fail)
            monkeypatch.setattr(cli, "port_problem", lambda host, port: None)  # reach the run_board failure path
            assert cli.main(["--config", str(path), "serve", "--no-update"]) == 1
        err = capsys.readouterr().err
        assert f"Cannot start the server on 127.0.0.1:{port}: address in use" in err
        assert "is not free" not in err

    def test_occupied_port_returns_one_before_running(self, root, tmp_path, monkeypatch, capsys):
        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            port = held.getsockname()[1]
            path = tmp_path / "board.json"
            path.write_text(json.dumps(config_dict(root, port=port)), encoding="utf-8")
            monkeypatch.setattr(cli, "run_board", lambda *a: pytest.fail("run_board must not be reached"))
            assert cli.main(["--config", str(path), "serve", "--no-update"]) == 1
        assert f"127.0.0.1:{port} is not free" in capsys.readouterr().err

PLAN = """# A plan

Task: [T001](../Tasks/01-Refinement/T001-a-task.md), next [other plan](2610-02-Other.md#part),
[outside](../secret.md) and [web](https://example.com).

| Col | Value |
| --- | --- |
| a | <b>x</b> |
"""


@pytest.fixture
def plan_client(root, tmp_path, monkeypatch):
    """A board whose repo is tmp_path, with the tasks in Tasks/ and two plans in Plans/; T001 names the first."""
    plans = tmp_path / "Plans"
    plans.mkdir()
    (plans / "2610-01-A-Plan.md").write_text(PLAN, encoding="utf-8")
    (plans / "2610-02-Other.md").write_text("# Other\n", encoding="utf-8")
    (tmp_path / "secret.md").write_text("secret\n", encoding="utf-8")
    (root / "01-Refinement" / "T001-a-task.md").write_bytes(TASK.replace(
        "plan: null", "plan: Plans/2610-01-A-Plan.md").replace(
        "Something.", "See `Plans/2610-01-A-Plan.md` and Plans/2610-02-Other.md, not Plans/missing.md.").replace(
        "Which one?", "Approve Plans/2610-02-Other.md <b>?").encode())
    config = validate_board_config({"repo_root": str(tmp_path), "plans_dir": "Plans", "main_branch": "master",
                                    "sources": [{"name": "public", "root": "Tasks", "editable": True}]})
    return LocalClient(create_app(config).test_client())


class TestPlanView:
    def test_task_header_and_body_link_the_plans(self, plan_client):
        html = plan_client.get(TASK_URL).get_data(as_text=True)
        assert '<td><a href="/plan/2610-01-A-Plan.md">Plans/2610-01-A-Plan.md</a></td>' in html
        assert '<code><a href="/plan/2610-01-A-Plan.md">Plans/2610-01-A-Plan.md</a></code>' in html
        assert '<a href="/plan/2610-02-Other.md">Plans/2610-02-Other.md</a>' in html
        assert "not Plans/missing.md." in html
        assert 'Approve <a href="/plan/2610-02-Other.md">Plans/2610-02-Other.md</a> &lt;b&gt;?' in html

    def test_plan_is_rendered_read_only(self, plan_client):
        response = plan_client.get("/plan/2610-01-A-Plan.md")
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "<h1>A plan</h1>" in html and "<table>" in html and "&lt;b&gt;x&lt;/b&gt;" in html
        assert "<form" not in html.split("</header>", 1)[1] and 'data-mode="plan"' in html

    def test_links_in_a_plan_lead_to_tasks_and_plans(self, plan_client):
        html = plan_client.get("/plan/2610-01-A-Plan.md").get_data(as_text=True)
        assert '<a href="/task/public/T001">T001</a>' in html
        assert '<a href="/plan/2610-02-Other.md">other plan</a>' in html
        assert '<a href="https://example.com">web</a>' in html
        assert "<a>outside</a>" in html

    @pytest.mark.parametrize("name", ["missing.md", "..%2Fsecret.md", ".hidden.md", "2610-01-A-Plan.txt"])
    def test_only_existing_plan_files(self, plan_client, name):
        assert plan_client.get(f"/plan/{name}").status_code == 404

    def test_plan_view_needs_a_loopback_host(self, plan_client):
        assert plan_client.get("/plan/2610-01-A-Plan.md",
                               base_url="http://attacker.example:5070").status_code == 403


class TestTheme:
    def test_every_page_has_the_toggle_and_applies_the_stored_theme_early(self, plan_client):
        for url in ("/", TASK_URL, "/plan/2610-01-A-Plan.md"):
            html = plan_client.get(url).get_data(as_text=True)
            assert 'id="theme-toggle"' in html, url
            assert html.index("taskBoardTheme") < html.index('href="/static/base.css"'), url

    def test_base_css_has_a_light_token_block(self):
        base = os.path.join(BOARD_WEB_DIR, "static", "base.css")
        if not os.path.exists(base):
            pytest.skip("base.css (the colour tokens) is the project's theme file, written by its build")
        with open(base, encoding="utf-8") as f:
            css = f.read()
        assert ':root[data-theme="light"]' in css
