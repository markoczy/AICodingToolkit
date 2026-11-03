"""Tests for the harness builder: the template engine, answer validation and smoke builds of both variants."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build  # noqa: E402

CTX = {"git": True, "svn": False, "name": "World", "n": 3}


class TestRender:
    def test_variables_and_inline_if(self):
        assert build.render("hi {{name}}: {{#if git}}PR{{else}}rev{{/if}}", CTX) == "hi World: PR"

    def test_block_tags_take_their_line(self):
        text = "a\n{{#if svn}}\nsvn line\n{{else}}\ngit line\n{{/if}}\nb\n"
        assert build.render(text, CTX) == "a\ngit line\nb\n"

    def test_nested_blocks_and_expressions(self):
        text = "{{#if git and n > 2}}x{{#if not svn}}y{{/if}}{{/if}}{{#if svn}}z{{/if}}"
        assert build.render(text, CTX) == "xy"

    def test_crlf_lines_survive(self):
        assert build.render("a\r\n{{#if git}}\r\nb\r\n{{/if}}\r\n", CTX) == "a\r\nb\r\n"

    def test_unknown_variable_fails(self):
        with pytest.raises(build.BuildError, match="unknown template value"):
            build.render("{{nope}}", CTX)

    def test_unknown_flag_fails(self):
        with pytest.raises(build.BuildError, match="unknown name"):
            build.render("{{#if nope}}x{{/if}}", CTX)

    def test_unclosed_block_fails(self):
        with pytest.raises(build.BuildError, match="unclosed"):
            build.render("{{#if git}}x", CTX)

    def test_non_string_values_render_as_json(self):
        assert build.render("{{people}}", {"people": ["Alex", "Robin"]}) == '["Alex", "Robin"]'


class TestAnswers:
    def test_examples_validate(self):
        git = build.example_answers("git")
        svn = build.example_answers("svn")
        assert git["vcs"] == "git" and git["main_branch"] == "master"
        assert svn["vcs"] == "svn" and "main_branch" not in svn

    def test_missing_required_answer(self):
        with pytest.raises(build.BuildError, match="project_name: missing"):
            build.validate({"vcs": "git", "mode": "single", "user_name": "Alex"})

    def test_bad_choice_and_pattern(self):
        answers = build.example_answers("git")
        answers["deploy"] = "moon"
        answers["color_primary"] = "purple"
        with pytest.raises(build.BuildError) as info:
            build.validate(answers)
        assert "deploy:" in str(info.value) and "color_primary:" in str(info.value)

    def test_user_must_be_in_people(self):
        answers = build.example_answers("svn")
        answers["people"] = ["Robin"]
        with pytest.raises(build.BuildError, match="user_name must be one of people"):
            build.validate(answers)

    def test_context_flags(self):
        ctx = build.context(build.example_answers("svn"))
        assert ctx["svn"] and ctx["multiuser"] and ctx["open"] and not ctx["strict"]
        assert ctx["main"] == "trunk" and ctx["review_field"] == "rev" and ctx["dev"] == "Robin"
        ctx = build.context(build.example_answers("git"))
        assert ctx["git"] and ctx["single"] and ctx["analyst"] and ctx["review_field"] == "pr"
        assert "analyst" in json.loads(ctx["roles_json"])

    def test_advanced_defaults_and_bounds(self):
        answers = build.validate(build.example_answers("git"))
        assert answers["compact_window"] == 150000 and answers["watch_quiet_seconds"] == 120
        assert answers["watch_hold_seconds"] == 600
        ctx = build.context(answers)
        assert ctx["compact_window_k"] == "150k" and ctx["reload_budget"] == 20000
        answers["compact_window"] = 50
        with pytest.raises(build.BuildError, match="compact_window: 50 is not between 100000 and 1000000"):
            build.validate(answers)

    def test_shade(self):
        assert build.shade("#8b5cf6", 0.1) != "#8b5cf6"
        assert build.shade("#000000", -0.5) == "#000000"
        assert build.shade("#ffffff", 0.5) == "#ffffff"


@pytest.mark.parametrize("vcs", ["git", "svn"])
def test_smoke_build(tmp_path, vcs):
    answers = build.example_answers(vcs)
    out = tmp_path / "out"
    zip_path = tmp_path / "harness.zip"
    written = build.build(answers, out, zip_path)
    assert zip_path.exists() and "CLAUDE.md" in written and "harness-answers.json" in written
    assert (out / "AI" / "Methodology.md").exists()
    assert (out / ".claude" / "svn" / "sv.py").exists() == (vcs == "svn")
    assert (out / "harness.gitignore").exists() == (vcs == "git")
    assert (out / "svn-ignore.txt").exists() == (vcs == "svn")
    multiuser = answers["mode"] == "multi"
    assert (out / ".claude" / "skills" / "coordinator" / "SKILL.md").exists() == multiuser
    assert (out / ".claude" / "agents" / "analyst.md").exists() == bool(answers["analyst"])
    for state in ("00-Conception", "08-Dropped"):
        assert (out / "AI" / "Tasks" / state / ".gitkeep").exists()
    # Rendered files: valid JSON, no leftover tags, no template-only names.
    for path in out.rglob("*"):
        rel = path.relative_to(out).as_posix()
        if path.is_dir() or "web/templates" in rel:
            continue
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        if path.suffix in (".md", ".json", ".css", ".txt"):
            text = path.read_text(encoding="utf-8")
            assert "{{" not in text, rel
            assert not re.search(r"ChronoSys|BTLWebViewer|Ellishia|lignocam", text), rel
    board = json.loads((out / ".claude" / "board" / "board.json").read_text(encoding="utf-8"))
    assert board["vcs"] == vcs
    assert board["ownership"] == ("open" if vcs == "svn" else "strict")
    settings = json.loads((out / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert ("WorktreeCreate" in settings["hooks"]) == (vcs == "svn")


def test_advanced_answers_reach_the_output(tmp_path):
    answers = build.example_answers("git")
    answers.update({"compact_window": 200000, "watch_quiet_seconds": 300, "board_idle_minutes": 0,
                    "watch_hold_seconds": 0})
    out = tmp_path / "out"
    build.build(answers, out)
    settings = json.loads((out / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["autoCompactWindow"] == 200000
    assert settings["env"]["CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"] == "100000"
    board = json.loads((out / ".claude" / "board" / "board.json").read_text(encoding="utf-8"))
    assert board["watch"]["quiet_seconds"] == 300 and board["watch"]["board_idle_minutes"] == 0
    assert board["watch"]["hold_seconds"] == 0 and "hours" not in board["watch"]
    assert "200k tokens" in (out / ".claude" / "skills" / "supervisor" / "SKILL.md").read_text(encoding="utf-8")
    questions = build.load_questions()
    assert [q["id"] for q in questions if q.get("advanced")][0] == "compact_window"


def test_cli_questions_and_example():
    script = Path(__file__).resolve().parents[1] / "build.py"
    out = subprocess.run([sys.executable, str(script), "--questions"], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)[0]["id"] == "project_name"
    out = subprocess.run([sys.executable, str(script), "--example", "svn"], capture_output=True, text=True,
                         check=True)
    assert json.loads(out.stdout)["vcs"] == "svn"
