"""The SessionStart reload hook (.claude/hooks/reload.py): public files first, then local.json's "reload" list."""
import json
import os
import subprocess
import sys

import pytest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "hooks", "reload.py")


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".claude" / "skills" / "supervisor").mkdir(parents=True)
    (tmp_path / ".claude" / "board").mkdir(parents=True)
    (tmp_path / "AI").mkdir()
    (tmp_path / ".claude" / "skills" / "supervisor" / "SKILL.md").write_text("SKILL TEXT", encoding="utf-8")
    (tmp_path / "AI" / "SupervisorHandover.md").write_text("HANDOVER TEXT", encoding="utf-8")
    return tmp_path


def run_hook(project, local=None, source="compact", transcript=None, role="supervisor"):
    if local is not None:
        (project / ".claude" / "board" / "local.json").write_text(local, encoding="utf-8")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    env.pop("CLAUDE_ROLE", None)
    if role:
        env["CLAUDE_ROLE"] = role
    payload = {"source": source}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    done = subprocess.run([sys.executable, HOOK], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0
    return done.stdout


def test_listed_files_follow_the_public_ones(project):
    (project / "x.md").write_text("X TEXT", encoding="utf-8")
    out = run_hook(project, json.dumps({"reload": ["x.md", "missing.md"]}))
    heads = [line for line in out.splitlines() if line.startswith("=====")]
    assert heads == ["===== .claude/skills/supervisor/SKILL.md =====", "===== AI/SupervisorHandover.md =====",
                     "===== x.md =====", "===== missing.md ====="]
    assert out.index("SKILL TEXT") < out.index("HANDOVER TEXT") < out.index("X TEXT")
    note = out.split("===== missing.md =====\n", 1)[1].splitlines()[0]
    assert note.startswith("(could not read missing.md") and note.endswith(")")


def test_without_local_json_or_key_the_output_is_unchanged(project):
    plain = run_hook(project)
    assert [line for line in plain.splitlines() if line.startswith("=====")] == [
        "===== .claude/skills/supervisor/SKILL.md =====", "===== AI/SupervisorHandover.md ====="]
    assert run_hook(project, json.dumps({"me": "Robin"})) == plain


def test_a_non_utf8_file_is_replaced_not_fatal(project):
    (project / "bad.md").write_bytes(b"before \xff\xfe after")
    (project / "next.md").write_text("NEXT TEXT", encoding="utf-8")
    out = run_hook(project, json.dumps({"reload": ["bad.md", "next.md"]}))
    assert "before" in out and "after" in out and out.index("===== bad.md") < out.index("NEXT TEXT")


@pytest.mark.parametrize("entry", ["/x.md", "\\x.md", "C:x.md", "C:/x.md", "d\\..\\x.md"])
def test_absolute_drive_and_parent_entries_are_skipped(project, entry):
    (project / "x.md").write_text("X TEXT", encoding="utf-8")
    assert run_hook(project, json.dumps({"reload": [entry]})) == run_hook(project, "{}")


def test_each_file_is_printed_once(project):
    (project / "x.md").write_text("X TEXT", encoding="utf-8")
    out = run_hook(project, json.dumps({"reload": ["AI/SupervisorHandover.md", "x.md", "./x.md", "x.md"]}))
    heads = [line for line in out.splitlines() if line.startswith("=====")]
    assert heads == ["===== .claude/skills/supervisor/SKILL.md =====", "===== AI/SupervisorHandover.md =====",
                     "===== x.md ====="]


@pytest.mark.parametrize("local", ["{not json", "[]", '{"reload": "x.md"}', '{"reload": null}',
                                   '{"reload": [1, "", "/etc/passwd", "../up.md"]}'])
def test_invalid_local_json_changes_nothing(project, local):
    plain = run_hook(project)
    assert run_hook(project, local) == plain


# --- no duplicate skill bodies ---------------------------------------------------------------------------------

def add_skill(project, name, text, front_name=None):
    folder = project / ".claude" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {front_name}\n---\n" if front_name else ""
    (folder / "SKILL.md").write_text(front + text, encoding="utf-8")
    return f".claude/skills/{name}/SKILL.md"


def write_transcript(project, *entries):
    path = project / "transcript.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def tool_call(skill):
    return {"message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Skill",
                                                           "input": {"skill": skill}}]}}


def slash(skill, as_list=False):
    text = f"<command-message>{skill}</command-message>\n<command-name>/{skill}</command-name>"
    return {"message": {"role": "user", "content": [{"type": "text", "text": text}] if as_list else text}}


NOISE = {"message": {"role": "user", "content": "a Skill is mentioned here, but nothing is invoked"}}


def test_invoked_supervisor_skill_is_skipped_on_compact_but_printed_on_startup(project):
    for entry in (tool_call("supervisor"), slash("supervisor"), slash("supervisor", as_list=True)):
        transcript = write_transcript(project, NOISE, entry)
        compact = run_hook(project, source="compact", transcript=transcript)
        assert "SKILL TEXT" not in compact and "HANDOVER TEXT" in compact
        assert "skill supervisor: already in context" in compact
        assert "skills already in context: supervisor" in compact.splitlines()[-1]
        startup = run_hook(project, source="startup", transcript=transcript)
        assert "SKILL TEXT" in startup and "already in context" not in startup


def test_without_an_invocation_in_the_transcript_the_skill_is_printed(project):
    transcript = write_transcript(project, NOISE, tool_call("other"))
    out = run_hook(project, source="compact", transcript=transcript)
    assert "SKILL TEXT" in out and "already in context" not in out
    assert "SKILL TEXT" in run_hook(project, source="compact")


def test_a_second_skill_in_the_list_behaves_the_same(project):
    rel = add_skill(project, "helper", "HELPER TEXT")
    add_skill(project, "unused", "UNUSED TEXT")
    local = json.dumps({"reload": [rel, ".claude/skills/unused/SKILL.md"]})
    transcript = write_transcript(project, tool_call("supervisor"), slash("helper"))
    compact = run_hook(project, local, source="compact", transcript=transcript)
    assert "HELPER TEXT" not in compact and "skill helper: already in context" in compact
    assert "UNUSED TEXT" in compact
    startup = run_hook(project, source="startup", transcript=transcript)
    assert "HELPER TEXT" in startup and "UNUSED TEXT" in startup


def test_a_skill_goes_by_its_front_matter_name_too(project):
    rel = add_skill(project, "folder-name", "BODY TEXT", front_name="short")
    transcript = write_transcript(project, tool_call("supervisor"), tool_call("short"))
    out = run_hook(project, json.dumps({"reload": [rel]}), transcript=transcript)
    assert "BODY TEXT" not in out and "skill short: already in context" in out


def test_a_plain_file_named_like_an_invoked_skill_is_printed(project):
    (project / "supervisor.md").write_text("PLAIN TEXT", encoding="utf-8")
    out = run_hook(project, json.dumps({"reload": ["supervisor.md"]}),
                   transcript=write_transcript(project, tool_call("supervisor")))
    assert "PLAIN TEXT" in out


def test_the_transcript_still_activates_the_hook_without_the_role(project):
    transcript = write_transcript(project, tool_call("supervisor"))
    assert "HANDOVER TEXT" in run_hook(project, role=None, transcript=transcript)
    assert run_hook(project, role=None, transcript=write_transcript(project, tool_call("other"))) == ""
    assert run_hook(project, role=None, transcript=project / "missing.jsonl") == ""


# --- total budget ----------------------------------------------------------------------------------------------

def set_budget(project, value):
    (project / ".claude" / "board" / "board.json").write_text(json.dumps({"reload": {"budget_bytes": value}}),
                                                              encoding="utf-8")


def test_the_budget_drops_the_last_files_with_a_pointer(project):
    (project / "a.md").write_text("A" * 300, encoding="utf-8")
    (project / "b.md").write_text("B" * 300, encoding="utf-8")
    set_budget(project, 700)
    out = run_hook(project, json.dumps({"reload": ["a.md", "b.md"]}))
    assert "A" * 300 in out and "B" * 300 not in out
    note = out.split("===== b.md =====\n", 1)[1].splitlines()[0]
    assert "300 bytes" in note and "read b.md" in note
    last = out.splitlines()[-1]
    assert f"a.md ({len(chr(10) + '===== a.md =====' + chr(10)) + 301} B)" in last and "pointers to b.md (300 B)" in last and "of 700 B budget" in last


def test_a_later_small_file_still_fits_after_a_dropped_big_one(project):
    (project / "big.md").write_text("B" * 900, encoding="utf-8")
    (project / "small.md").write_text("small text", encoding="utf-8")
    set_budget(project, 500)
    out = run_hook(project, json.dumps({"reload": ["big.md", "small.md"]}))
    assert "B" * 900 not in out and "small text" in out


def test_the_budget_never_drops_a_handover_file(project):
    (project / "notes").mkdir()
    (project / "notes" / "Local Handover.md").write_text("LOCAL HANDOVER " * 20, encoding="utf-8")
    (project / "a.md").write_text("A" * 200, encoding="utf-8")
    local = json.dumps({"reload": ["a.md", "notes/Local Handover.md"]})
    # Far below the size of the handover files: they still print, everything else becomes a pointer.
    for budget in (1, 400):
        set_budget(project, budget)
        out = run_hook(project, local)
        assert "HANDOVER TEXT" in out and "LOCAL HANDOVER " * 20 in out
        assert "A" * 200 not in out
    # The handover files' size is reserved before the earlier a.md may use the budget.
    set_budget(project, 10000)
    assert "A" * 200 in run_hook(project, local)
    reserved = len("LOCAL HANDOVER " * 20) + len("HANDOVER TEXT") + 100
    set_budget(project, reserved)
    out = run_hook(project, local)
    assert "A" * 200 not in out and "LOCAL HANDOVER " * 20 in out and "HANDOVER TEXT" in out


def test_the_default_budget_applies_without_a_valid_key(project):
    (project / "big.md").write_text("B" * 30000, encoding="utf-8")
    (project / "ok.md").write_text("O" * 15000, encoding="utf-8")
    local = json.dumps({"reload": ["ok.md", "big.md"]})
    board = project / ".claude" / "board" / "board.json"
    for config in (None, "{not json", json.dumps({"reload": {"budget_bytes": 0}}),
                   json.dumps({"reload": {"budget_bytes": "5"}}), json.dumps({"reload": 7})):
        if config is None:
            if board.exists():
                board.unlink()
        else:
            board.write_text(config, encoding="utf-8")
        out = run_hook(project, local)
        assert "O" * 15000 in out and "B" * 30000 not in out
        assert "of 20000 B budget" in out.splitlines()[-1]


def test_a_missing_file_still_prints_its_one_line_error_with_a_budget(project):
    set_budget(project, 100)
    out = run_hook(project, json.dumps({"reload": ["missing.md"]}))
    assert "(could not read missing.md" in out and "HANDOVER TEXT" in out


def test_skipped_skills_do_not_use_the_budget(project):
    rel = add_skill(project, "helper", "H" * 5000)
    set_budget(project, 600)
    (project / "a.md").write_text("A" * 100, encoding="utf-8")
    transcript = write_transcript(project, tool_call("supervisor"), tool_call("helper"))
    out = run_hook(project, json.dumps({"reload": [rel, "a.md"]}), transcript=transcript)
    assert "H" * 5000 not in out and "A" * 100 in out and "pointers to none" in out


def test_the_summary_total_is_the_total_the_budget_counts(project):
    (project / "a.md").write_text("A" * 300, encoding="utf-8")
    out = run_hook(project, json.dumps({"reload": ["a.md"]}))
    head, tail = out.split("\n[reload hook: printed", 1)
    blocks = out[out.index("\n=====") : out.rindex("\n[reload hook: printed")]
    total = int(tail.split(" = ", 1)[1].split(" B of", 1)[0])
    assert total == len(blocks.encode("utf-8"))
