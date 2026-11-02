"""
Tests for taskboard/model.py: header and section parsing (load_tasks), the consistency check (check_tasks) and
the board output. All on task folders built in tmp_path.
"""
import os
import re
import shutil

import pytest

from taskboard.model import (REQUIRED_FIELDS, STATES, check_tasks, effective_owner, filter_board, format_board,
                             load_tasks, required_fields, waits_on_person)
from taskboard.textedit import migrate_owner

SECTIONS = ("\n## Problem\nP\n\n## Approach\n\n## Acceptance\n\n## Questions\n{questions}\n\n## Decisions\n\n"
            "## Log\n- x\n")


def task_text(task_id="T001", title="A task", questions="", **overrides):
    fields = {
        "id": task_id, "title": title, "type": "bug", "priority": "high", "owner": "null", "created": "2026-10-01",
        "updated": "2026-10-02", "waiting_on": "null", "blocked_by": "[]", "plan": "null",
        "branch": "null", "pr": "null",
    }
    fields.update(overrides)
    header = "\n".join(f"{k}: {v}" for k, v in fields.items() if v is not None)
    return f"---\n{header}\n---\n" + SECTIONS.format(questions=questions)


@pytest.fixture
def tasks_dir(tmp_path):
    for state in STATES:
        (tmp_path / state).mkdir()
    (tmp_path / "README.md").write_text("# Tasks\n", encoding="utf-8")
    return tmp_path


def write(tasks_dir, state, name, text):
    (tasks_dir / state / name).write_text(text, encoding="utf-8")


class TestLoadTasks:
    def test_returns_plain_data_per_state(self, tasks_dir):
        write(tasks_dir, "02-Ready", "T001-a-task.md", task_text(blocked_by="[T002, T003]", waiting_on="user"))
        write(tasks_dir, "07-Done", "T002-other.md", task_text("T002", pr="15", plan="doc/x.md"))
        (tasks_dir / "08-Dropped" / ".gitkeep").write_text("", encoding="utf-8")

        board = load_tasks(str(tasks_dir))

        assert board["states"] == STATES
        assert board["errors"] == []
        assert [(t["state"], t["file"]) for t in board["tasks"]] == [
            ("02-Ready", "T001-a-task.md"), ("07-Done", "T002-other.md")]
        first = board["tasks"][0]
        assert first["path"] == "02-Ready/T001-a-task.md"
        assert first["header"] == {
            "id": "T001", "title": "A task", "type": "bug", "priority": "high", "owner": None, "created": "2026-10-01",
            "updated": "2026-10-02", "waiting_on": "user", "blocked_by": ["T002", "T003"], "plan": None,
            "branch": None, "pr": None,
        }
        assert first["sections"]["Problem"] == "P"
        assert first["sections"]["Log"] == "- x"
        assert board["tasks"][1]["header"]["pr"] == 15
        assert board["tasks"][1]["header"]["plan"] == "doc/x.md"

    def test_comments_and_quoted_titles(self, tasks_dir):
        text = task_text(title='"Fix `x`: a \\"quoted\\" # not a comment"  # real comment',
                         type="feature   # bug | feature", priority="'low'")
        write(tasks_dir, "00-Conception", "T001-x.md", text)

        header = load_tasks(str(tasks_dir))["tasks"][0]["header"]

        assert header["title"] == 'Fix `x`: a "quoted" # not a comment'
        assert header["type"] == "feature"
        assert header["priority"] == "low"

    def test_unquoted_title_keeps_apostrophe(self, tasks_dir):
        write(tasks_dir, "00-Conception", "T001-x.md", task_text(title="Conditions don't fail"))
        assert load_tasks(str(tasks_dir))["tasks"][0]["header"]["title"] == "Conditions don't fail"

    def test_parses_questions(self, tasks_dir):
        questions = ("- **Q1** (open, for user): a?\n- **Q2** (answered 2026-10-02, supervisor): b? -> yes\n"
                     "- **Q3** (open, for supervisor): c?")
        write(tasks_dir, "01-Refinement", "T001-x.md", task_text(questions=questions, waiting_on="user"))
        task = load_tasks(str(tasks_dir))["tasks"][0]
        assert task["questions"] == [
            {"id": "Q1", "status": "open", "for": "user", "answered": None, "answered_by": None,
             "for_given": True, "text": "a?"},
            {"id": "Q2", "status": "answered", "for": "supervisor", "answered": "2026-10-02",
             "answered_by": "supervisor", "for_given": False, "text": "b? -> yes"},
            {"id": "Q3", "status": "open", "for": "supervisor", "answered": None, "answered_by": None,
             "for_given": True, "text": "c?"},
        ]
        assert task["errors"] == []

    def test_question_without_role_is_an_error(self, tasks_dir):
        write(tasks_dir, "01-Refinement", "T001-x.md", task_text(questions="- **Q1** (open): a?"))
        task = load_tasks(str(tasks_dir))["tasks"][0]
        assert task["questions"] == []
        assert task["errors"] == ["question not in the form '(open, for <role>)' or "
                                  "'(answered <date>, <who>, for <role>)': '- **Q1** (open): a?'"]

    def test_non_utf8_file_is_reported_not_raised(self, tasks_dir):
        (tasks_dir / "02-Ready" / "T001-x.md").write_bytes(b"---\nid: T001\ntitle: \xff\xfe bad\n---\n")
        write(tasks_dir, "02-Ready", "T002-y.md", task_text("T002"))
        board = load_tasks(str(tasks_dir))
        bad = board["tasks"][0]
        assert bad["header"] == {} and bad["sections"] == {}
        assert bad["errors"][0].startswith("cannot read file: ")
        assert board["tasks"][1]["header"]["id"] == "T002"
        problems = check_tasks(board)
        assert len(problems) == 1 and problems[0].startswith("02-Ready/T001-x.md: cannot read file: ")
        assert "T002" in format_board(board)

    def test_crlf_file(self, tasks_dir):
        write(tasks_dir, "00-Conception", "T001-x.md", task_text().replace("\n", "\r\n"))
        board = load_tasks(str(tasks_dir))
        assert board["tasks"][0]["header"]["id"] == "T001"
        assert check_tasks(board) == []

    def test_missing_header_is_a_parse_error(self, tasks_dir):
        write(tasks_dir, "00-Conception", "T001-x.md", "# no header\n")
        board = load_tasks(str(tasks_dir))
        assert board["tasks"][0]["errors"] == ["header: file does not start with '---'"]

    def test_unknown_and_missing_folders(self, tasks_dir):
        (tasks_dir / "09-Archive").mkdir()
        (tasks_dir / "06-Deploy").rmdir()
        errors = load_tasks(str(tasks_dir))["errors"]
        assert "09-Archive/: not a state folder" in errors
        assert "06-Deploy/: missing state folder" in errors


class TestCheckTasks:
    def check(self, tasks_dir, *files, repo_root=None):
        for state, name, text in files:
            write(tasks_dir, state, name, text)
        return check_tasks(load_tasks(str(tasks_dir)), repo_root)

    def test_valid_tasks_have_no_problems(self, tasks_dir):
        assert self.check(tasks_dir,
                          ("01-Refinement", "T001-a.md", task_text(blocked_by="[T002]", waiting_on="T002")),
                          ("02-Ready", "T002-b.md", task_text("T002"))) == []

    def test_id_must_match_file_name(self, tasks_dir):
        problems = self.check(tasks_dir, ("02-Ready", "T001-a.md", task_text("T002")))
        assert problems == ["02-Ready/T001-a.md: header id T002 does not match the file name"]

    def test_duplicate_ids_across_states(self, tasks_dir):
        problems = self.check(tasks_dir, ("02-Ready", "T001-a.md", task_text()),
                              ("07-Done", "T001-b.md", task_text()))
        assert problems == ["07-Done/T001-b.md: duplicate id T001 (also 02-Ready/T001-a.md)"]

    def test_bad_file_name(self, tasks_dir):
        problems = self.check(tasks_dir, ("02-Ready", "T1-Upper_Case.md", task_text("T001")))
        assert problems == ["02-Ready/T1-Upper_Case.md: file name is not '<id>-<slug>.md' (lowercase slug)"]

    def test_missing_fields_and_sections(self, tasks_dir):
        text = task_text(pr=None, branch=None).replace("## Acceptance\n", "")
        problems = self.check(tasks_dir, ("02-Ready", "T001-a.md", text))
        assert problems == ["02-Ready/T001-a.md: missing header field(s) branch, pr",
                            "02-Ready/T001-a.md: missing section(s) Acceptance"]

    @pytest.mark.parametrize("override, expected", [
        ({"type": "chore"}, "invalid type 'chore'"),
        ({"priority": "urgent"}, "invalid priority 'urgent'"),
        ({"created": "2026-13-01"}, "created is not a YYYY-MM-DD date: '2026-13-01'"),
        ({"updated": "2026-09-01"}, "updated is before created"),
        ({"waiting_on": "nobody"}, "invalid waiting_on 'nobody'"),
        ({"blocked_by": "T002"}, "blocked_by must be a list of task ids, got 'T002'"),
        ({"blocked_by": "[later]"}, "blocked_by must be a list of task ids, got ['later']"),
        ({"pr": "abc"}, "pr must be null or a pull request number, got 'abc'"),
        ({"pr": "0"}, "pr must be null or a pull request number, got 0"),
        ({"id": "X1"}, "invalid id 'X1'"),
        ({"owner": "me"}, "invalid owner 'me' (not in board.json people)"),
        ({"owner": "[a, b]"}, "owner must be null or a person, got ['a', 'b']"),
        ({"owner": None}, "missing header field(s) owner"),
        ({"assignee": "x"}, "unknown header field(s) assignee"),
        ({"title": '""'}, "title must be non-empty text, got ''"),
    ])
    def test_invalid_values(self, tasks_dir, override, expected):
        problems = self.check(tasks_dir, ("02-Ready", "T001-a.md", task_text(**override)))
        assert problems == [f"02-Ready/T001-a.md: {expected}"]

    @pytest.mark.parametrize("field", ["id", "type", "priority", "waiting_on", "title"])
    def test_list_values_are_reported_not_raised(self, tasks_dir, field):
        problems = self.check(tasks_dir, ("02-Ready", "T001-a.md", task_text(**{field: "[a, b]"})))
        assert len(problems) == 1
        assert problems[0].startswith("02-Ready/T001-a.md: ")

    def test_scalar_blocked_by_is_reported_not_raised(self, tasks_dir):
        write(tasks_dir, "02-Ready", "T001-a.md", task_text(blocked_by="5"))
        board = load_tasks(str(tasks_dir))
        assert check_tasks(board) == ["02-Ready/T001-a.md: blocked_by must be a list of task ids, got 5"]
        assert "blocked by 5" in format_board(board)

    def test_unquoted_title_with_colon(self, tasks_dir):
        problems = self.check(tasks_dir, ("02-Ready", "T001-a.md", task_text(title="Fix: something")),
                              ("02-Ready", "T002-b.md", task_text("T002", title='"Fix: something"')))
        assert problems == ["02-Ready/T001-a.md: title contains ': ' and must be quoted"]

    def test_open_question_for_user_requires_waiting_on_user(self, tasks_dir):
        problems = self.check(tasks_dir, ("01-Refinement", "T001-a.md",
                                          task_text(questions="- **Q1** (open, for user): a?",
                                                    waiting_on="supervisor")))
        assert problems == ["01-Refinement/T001-a.md: open question(s) for the user (Q1) but waiting_on is "
                            "'supervisor', not user"]

    def test_waiting_on_user_requires_open_question_for_user(self, tasks_dir):
        questions = "- **Q1** (open, for supervisor): a?\n- **Q2** (answered 2026-10-02, user): b? -> yes"
        problems = self.check(tasks_dir, ("01-Refinement", "T001-a.md",
                                          task_text(questions=questions, waiting_on="user")))
        assert problems == ["01-Refinement/T001-a.md: waiting_on is user but no open question is for the user"]

    def test_question_role_and_date_are_checked(self, tasks_dir):
        questions = "- **Q1** (open, for nobody): a?\n- **Q2** (answered 2026-02-30, user): b? -> yes"
        problems = self.check(tasks_dir, ("01-Refinement", "T001-a.md", task_text(questions=questions)))
        assert problems == ["01-Refinement/T001-a.md: Q1 names unknown role 'nobody'",
                            "01-Refinement/T001-a.md: Q2 has an invalid answer date '2026-02-30'"]

    def test_references_to_unknown_tasks(self, tasks_dir):
        problems = self.check(tasks_dir, ("02-Ready", "T001-a.md", task_text(blocked_by="[T009]",
                                                                            waiting_on="T008")))
        assert problems == ["02-Ready/T001-a.md: blocked_by names unknown task T009",
                            "02-Ready/T001-a.md: waiting_on names unknown task T008"]

    def test_plan_must_exist_when_repo_root_given(self, tasks_dir, tmp_path_factory):
        root = tmp_path_factory.mktemp("repo")
        (root / "plan.md").write_text("x", encoding="utf-8")
        assert self.check(tasks_dir, ("02-Ready", "T001-a.md", task_text(plan="plan.md")), repo_root=str(root)) == []
        write(tasks_dir, "02-Ready", "T002-b.md", task_text("T002", plan="gone.md"))
        assert check_tasks(load_tasks(str(tasks_dir)), str(root)) == ["02-Ready/T002-b.md: plan gone.md does not exist"]

    def test_any_prefix_is_accepted_without_a_source_prefix(self, tasks_dir):
        assert self.check(tasks_dir, ("02-Ready", "P001-a.md", task_text("P001"))) == []

    def test_source_prefix_is_enforced(self, tasks_dir):
        write(tasks_dir, "02-Ready", "P001-a.md", task_text("P001"))
        assert check_tasks(load_tasks(str(tasks_dir)), prefix="T") == [
            "02-Ready/P001-a.md: id P001 does not carry this source's prefix T"]

    def test_other_sources_ids_are_known(self, tasks_dir):
        write(tasks_dir, "02-Ready", "T001-a.md", task_text(blocked_by="[P004]"))
        assert check_tasks(load_tasks(str(tasks_dir)), known_ids={"P004"}) == []

    def test_schema_sets_types_and_roles(self, tasks_dir):
        write(tasks_dir, "01-Refinement", "T001-a.md",
              task_text(type="chore", questions="- **Q1** (open, for designer): a?", waiting_on="designer"))
        board = load_tasks(str(tasks_dir))
        assert check_tasks(board, schema={"types": ["chore"], "roles": ["user", "supervisor", "designer"]}) == []
        assert len(check_tasks(board)) == 3

    def test_duplicate_question_ids(self, tasks_dir):
        questions = "- **Q1** (open, for supervisor): a?\n- **Q1** (open, for supervisor): b?"
        problems = self.check(tasks_dir, ("01-Refinement", "T001-a.md", task_text(questions=questions)))
        assert problems == ["01-Refinement/T001-a.md: question Q1 appears twice"]

    def test_heading_inside_a_code_block_is_not_a_section(self, tasks_dir):
        text = task_text().replace("## Approach\n", "## Approach\n```\n## Log\n```\n")
        write(tasks_dir, "02-Ready", "T001-a.md", text)
        board = load_tasks(str(tasks_dir))
        assert check_tasks(board) == []
        assert board["tasks"][0]["sections"]["Approach"] == "```\n## Log\n```"


class TestBoard:
    def test_format_board(self, tasks_dir):
        write(tasks_dir, "01-Refinement", "T001-a.md",
              task_text(title="First", blocked_by="[T002]", waiting_on="user"))
        write(tasks_dir, "02-Ready", "T002-b.md", task_text("T002", title="Second", priority="low"))
        text = format_board(load_tasks(str(tasks_dir)))
        assert "01-Refinement (1)\n  T001   high   First  [waiting on user; blocked by T002]" in text
        assert "02-Ready (1)\n  T002   low    Second" in text
        assert "08-Dropped (0)" in text


SCHEMA = {"types": ["bug"], "roles": ["user", "supervisor", "executor"], "people": ["Alex", "Robin"]}
# The question format before answerers were named; files written with it stay valid.
OLD_QUESTION = re.compile(
    r"^- \*\*(Q\d+)\*\* \((?:open, for ([\w-]+)|answered (\d{4}-\d{2}-\d{2}), ([\w-]+))\):")
# A fixed copy of task files in the old formats (real files from another project before its T021,
# `rev` renamed to `pr`, plus one synthetic
# file with open questions). Never the live AI/Tasks, so board content cannot break the suite.
OLD_FORMAT_TASKS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "tasks")


class TestAnswerer:
    def check(self, tasks_dir, questions, schema=SCHEMA, waiting_on="null"):
        write(tasks_dir, "01-Refinement", "T001-a.md", task_text(questions=questions, waiting_on=waiting_on))
        return check_tasks(load_tasks(str(tasks_dir)), schema=schema)

    def test_parses_the_new_answered_form(self, tasks_dir):
        questions = ("- **Q1** (answered 2026-10-21, Robin, for user): @Robin: a? → CSV\n"
                     "- **Q2** (answered 2026-10-21, supervisor, for supervisor): b? → yes")
        write(tasks_dir, "01-Refinement", "T001-x.md", task_text(questions=questions))
        task = load_tasks(str(tasks_dir))["tasks"][0]
        assert [(q["id"], q["status"], q["for"], q["answered_by"], q["for_given"], q["answered"], q["text"])
                for q in task["questions"]] == [
            ("Q1", "answered", "user", "Robin", True, "2026-10-21", "@Robin: a? → CSV"),
            ("Q2", "answered", "supervisor", "supervisor", True, "2026-10-21", "b? → yes")]
        assert task["errors"] == []

    def test_old_form_means_answerer_and_role_are_the_same(self, tasks_dir):
        write(tasks_dir, "01-Refinement", "T001-x.md",
              task_text(questions="- **Q1** (answered 2026-10-20, user): a? → yes"))
        q = load_tasks(str(tasks_dir))["tasks"][0]["questions"][0]
        assert (q["for"], q["answered_by"], q["for_given"]) == ("user", "user", False)

    def test_both_forms_pass_the_check(self, tasks_dir):
        questions = ("- **Q1** (answered 2026-10-20, user): a? → yes\n"
                     "- **Q2** (answered 2026-10-21, Robin, for user): b? → no\n"
                     "- **Q3** (answered 2026-10-21, Alex, for supervisor): c? → maybe\n"
                     "- **Q4** (answered 2026-10-21, executor, for supervisor): d? → done\n"
                     "- **Q5** (answered 2026-10-21, supervisor): e? → old form, a role")
        assert self.check(tasks_dir, questions) == []

    @pytest.mark.parametrize("question, problem", [
        ("- **Q1** (answered 2026-10-21, Bob, for user): a? → x", "Q1 names unknown role or person 'Bob'"),
        ("- **Q1** (answered 2026-10-21, Bob): a? → x", "Q1 names unknown role 'Bob'"),
        ("- **Q1** (answered 2026-10-21, Owens): a? → x", "Q1 names unknown role 'Owens'"),
        ("- **Q1** (answered 2026-10-21, Robin, for designer): a? → x", "Q1 names unknown role 'designer'"),
        ("- **Q1** (answered 2026-10-21, Robin, for Alex): a? → x", "Q1 names unknown role 'Alex'"),
        ("- **Q1** (answered 2026-10-21, Robin, user): a? → x", "question not in the form"),
    ])
    def test_flags(self, tasks_dir, question, problem):
        problems = self.check(tasks_dir, question)
        assert len(problems) == 1 and problem in problems[0], problems

    def test_open_questions_are_still_for_a_role(self, tasks_dir):
        problems = self.check(tasks_dir, "- **Q1** (open, for Robin): a?")
        assert problems == ["01-Refinement/T001-a.md: Q1 names unknown role 'Robin'"]

    def test_people_default_to_none(self, tasks_dir):
        problems = self.check(tasks_dir, "- **Q1** (answered 2026-10-21, Robin, for user): a? → x",
                              schema={"types": ["bug"], "roles": ["user", "supervisor"]})
        assert problems == ["01-Refinement/T001-a.md: Q1 names unknown role or person 'Robin'"]

    def test_existing_task_files_parse_and_check_as_before(self, tmp_path):
        """Fixed task files in the old formats (tests/fixtures/tasks): every question line means what it meant to
        the old parser, and naming people changes no problem."""
        copy = tmp_path / "Tasks"
        shutil.copytree(OLD_FORMAT_TASKS, copy)
        for state in STATES:
            (copy / state).mkdir(exist_ok=True)
        board = load_tasks(str(copy))
        assert board["tasks"]
        statuses = {q["status"] for task in board["tasks"] for q in task["questions"]}
        assert statuses == {"open", "answered"}
        for task in board["tasks"]:
            old = []
            for line in task["sections"].get("Questions", "").split("\n"):
                m = OLD_QUESTION.match(line)
                if m:
                    old.append((m.group(1), "open" if m.group(2) else "answered", m.group(2) or m.group(4),
                                m.group(3)))
            new = [(q["id"], q["status"], q["for"], q["answered"]) for q in task["questions"]]
            assert new == old, task["path"]
        roles = ["user", "supervisor", "executor", "reviewer", "researcher"]
        types = ["feature", "processing", "bug", "refactor", "spike", "docs", "research", "ops"]
        without = check_tasks(board, None, {"types": types, "roles": roles}, "T")
        with_people = check_tasks(board, None, {"types": types, "roles": roles,
                                                         "people": ["Alex", "Robin", "Owens"]}, "T")
        assert with_people == without


OWNER_SCHEMA = {"types": ["bug"], "roles": ["user", "supervisor"], "people": ["Alex", "Robin"],
                "default_owner": "Alex"}


def owner_task(owner="null", decisions="", questions="", waiting_on="null", task_id="T001"):
    return task_text(task_id, owner=owner, questions=questions, waiting_on=waiting_on).replace(
        "## Decisions\n", "## Decisions\n" + (decisions + "\n" if decisions else ""))


class TestOwner:
    def check(self, tasks_dir, text, schema=OWNER_SCHEMA):
        write(tasks_dir, "01-Refinement", "T001-a.md", text)
        return check_tasks(load_tasks(str(tasks_dir)), schema=schema)

    @pytest.mark.parametrize("owner", ["null", "Robin", "Alex"])
    def test_null_or_a_person_is_valid(self, tasks_dir, owner):
        assert self.check(tasks_dir, owner_task(owner)) == []

    def test_unknown_person(self, tasks_dir):
        assert self.check(tasks_dir, owner_task("Owens")) == [
            "01-Refinement/T001-a.md: invalid owner 'Owens' (not in board.json people)"]

    def test_without_people_only_null_is_valid(self, tasks_dir):
        assert self.check(tasks_dir, owner_task("null"), schema=None) == []
        assert self.check(tasks_dir, owner_task("Robin"), schema=None) == [
            "01-Refinement/T001-a.md: invalid owner 'Robin' (not in board.json people)"]

    @pytest.mark.parametrize("owner, decisions", [
        ("Robin", "- 2026-10-02, Robin: owner: Robin"),
        ("Robin", "- 2026-10-02, Alex: owner: Robin (transferred from Alex)"),
        ("null", "- 2026-10-02, Alex: owner: Robin (transferred from Alex)\n"
                 "- 2026-10-03, Robin: owner: Alex (transferred from Robin; forced: left)"),
        ("Alex", "- 2026-10-02, Alex: owner: Alex"),
        ("null", "- 2026-10-02, Alex: Q1 → owner: Robin\n- 2026-10-02, Alex: the owner: later"),
        ("Robin", ""),
    ])
    def test_consistent_with_the_latest_owner_decision(self, tasks_dir, owner, decisions):
        assert self.check(tasks_dir, owner_task(owner, decisions)) == []

    @pytest.mark.parametrize("owner, decisions, expected", [
        ("null", "- 2026-10-02, Robin: owner: Robin", "Decisions name owner Robin, header owner is Alex"),
        ("Robin", "- 2026-10-02, Alex: owner: Robin\n- 2026-10-03, Robin: owner: Alex "
                     "(transferred from Robin)", "Decisions name owner Alex, header owner is Robin"),
    ])
    def test_mismatch_with_the_latest_owner_decision(self, tasks_dir, owner, decisions, expected):
        assert self.check(tasks_dir, owner_task(owner, decisions)) == [f"01-Refinement/T001-a.md: {expected}"]

    def test_null_without_default_owner_means_unowned(self, tasks_dir):
        schema = {**OWNER_SCHEMA, "default_owner": None}
        assert self.check(tasks_dir, owner_task("null", "- 2026-10-02, Robin: owner: null (transferred from "
                                                        "Robin)"), schema) == []
        assert self.check(tasks_dir, owner_task("null", "- 2026-10-02, Alex: owner: Alex"), schema) == [
            "01-Refinement/T001-a.md: Decisions name owner Alex, header owner is null"]

    def test_a_file_without_the_field_reports_only_the_missing_field(self, tasks_dir):
        text = owner_task(decisions="- 2026-10-02, Robin: owner: Robin").replace("owner: null\n", "")
        assert self.check(tasks_dir, text) == ["01-Refinement/T001-a.md: missing header field(s) owner"]

    def test_effective_owner(self):
        assert effective_owner({"owner": None}, "Alex") == "Alex"
        assert effective_owner({"owner": "Robin"}, "Alex") == "Robin"
        assert effective_owner({}, None) is None

    def test_filter_and_format_board(self, tasks_dir):
        write(tasks_dir, "01-Refinement", "T001-a.md", owner_task("Robin", task_id="T001"))
        write(tasks_dir, "02-Ready", "T002-b.md", owner_task("null", task_id="T002"))
        write(tasks_dir, "02-Ready", "T003-c.md", owner_task("Alex", task_id="T003"))
        board = load_tasks(str(tasks_dir))
        assert [t["header"]["id"] for t in filter_board(board, "Alex", "Alex")["tasks"]] == ["T002", "T003"]
        assert [t["header"]["id"] for t in filter_board(board, "Robin", "Alex")["tasks"]] == ["T001"]
        assert [t["header"]["id"] for t in filter_board(board, "Alex", None)["tasks"]] == ["T003"]
        text = format_board(board)
        assert "  T001   high   A task  [owner Robin]" in text
        assert "  T002   high   A task\n" in text

    @pytest.mark.parametrize("question, owner, person, expected", [
        ("@Robin: a?", "null", "Robin", True),
        ("@Robin: a?", "null", "Alex", False),
        ("a?", "null", "Alex", True),
        ("a?", "Robin", "Alex", False),
        ("a?", "Robin", "Robin", True),
        ("@Bob: a?", "Robin", "Robin", False),
    ])
    def test_waits_on_person(self, tasks_dir, question, owner, person, expected):
        write(tasks_dir, "01-Refinement", "T001-a.md",
              owner_task(owner, questions=f"- **Q1** (open, for user): {question}", waiting_on="user"))
        task = load_tasks(str(tasks_dir))["tasks"][0]
        assert waits_on_person(task, person, "Alex") is expected

    def test_answered_and_supervisor_questions_wait_on_nobody(self, tasks_dir):
        questions = "- **Q1** (answered 2026-10-02, Alex, for user): a? → x\n- **Q2** (open, for supervisor): b?"
        write(tasks_dir, "01-Refinement", "T001-a.md", owner_task(questions=questions))
        assert waits_on_person(load_tasks(str(tasks_dir))["tasks"][0], "Alex", "Alex") is False


class TestMigrateOwner:
    def test_old_format_fixtures_check_clean_after_migration(self, tmp_path):
        """The fixed copy of old task files (no owner field) passes the check once migrated; the fixture T012 has
        an `owner: Robin` decision and gets that owner, every other file null."""
        copy = tmp_path / "Tasks"
        shutil.copytree(OLD_FORMAT_TASKS, copy)
        for state in STATES:
            (copy / state).mkdir(exist_ok=True)
        schema = {"types": ["feature", "processing", "bug", "refactor", "spike", "docs", "research", "ops"],
                  "roles": ["user", "supervisor", "executor", "reviewer", "researcher"],
                  "people": ["Alex", "Robin"], "default_owner": "Alex"}
        before = check_tasks(load_tasks(str(copy)), None, schema, "T", {"T021"})
        assert before and all("missing header field(s) owner" in p for p in before)
        values = {}
        for path in sorted(copy.glob("*/*.md")):
            text = path.read_bytes().decode("utf-8")
            new_text, value = migrate_owner(text, "Alex")
            values[path.name[:4]] = value
            newline = "\r\n" if "\r\n" in text else "\n"
            assert new_text.replace(f"owner: {value or 'null'}{newline}", "", 1) == text
            path.write_bytes(new_text.encode("utf-8"))
        assert values == {"T003": None, "T004": None, "T005": None, "T012": "Robin", "T019": None,
                          "T029": None, "T901": None}
        assert check_tasks(load_tasks(str(copy)), None, schema, "T", {"T021"}) == []
        for path in copy.glob("*/*.md"):
            text = path.read_bytes().decode("utf-8")
            assert migrate_owner(text, "Alex")[0] == text
            lines = text.replace("\r\n", "\n").split("\n")
            assert lines[next(i for i, l in enumerate(lines) if l.startswith("priority:")) + 1].startswith("owner: ")


class TestReviewField:
    """The review header field is `pr` (git, the default) or `rev` (svn): the schema's review_field decides."""

    SVN = {**SCHEMA, "review_field": "rev"}

    def test_required_fields_follow_the_schema(self, tasks_dir):
        write(tasks_dir, "02-Ready", "T001-a.md", task_text(pr=None, rev="12"))
        board = load_tasks(str(tasks_dir))
        assert board["tasks"][0]["header"]["rev"] == 12
        assert check_tasks(board, schema=self.SVN) == []
        assert check_tasks(board, schema=SCHEMA) == ["02-Ready/T001-a.md: missing header field(s) pr",
                                                     "02-Ready/T001-a.md: unknown header field(s) rev"]
        assert required_fields("rev")[-1] == "rev" and required_fields() == REQUIRED_FIELDS

    @pytest.mark.parametrize("value, expected", [
        ("abc", "rev must be null or a revision number, got 'abc'"),
        ("0", "rev must be null or a revision number, got 0"),
    ])
    def test_invalid_rev(self, tasks_dir, value, expected):
        write(tasks_dir, "02-Ready", "T001-a.md", task_text(pr=None, rev=value))
        assert check_tasks(load_tasks(str(tasks_dir)), schema=self.SVN) == [f"02-Ready/T001-a.md: {expected}"]

    def test_format_board_labels_the_review_field(self, tasks_dir):
        write(tasks_dir, "04-CodeReview", "T001-a.md", task_text(pr="77"))
        write(tasks_dir, "04-CodeReview", "T002-b.md", task_text("T002", title="B", pr=None, rev="12"))
        board = load_tasks(str(tasks_dir))
        assert "  T001   high   A task  [PR #77]" in format_board(board)
        assert "  T002   high   B  [r12]" in format_board(board, review_field="rev")
        assert "[PR #77]" not in format_board(board, review_field="rev")
