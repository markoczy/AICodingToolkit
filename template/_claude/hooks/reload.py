#!/usr/bin/env python3
"""SessionStart hook (startup|resume|compact|clear): re-inject the supervisor role and handover.

After the public files it prints the files listed under "reload" in .claude/board/local.json (untracked, per
checkout; a list of repo-relative paths), in the same framing.

Active only for a supervisor session: one launched with CLAUDE_ROLE=supervisor, or one whose own transcript shows
that the `supervisor` skill was invoked (Skill tool call or /supervisor). The transcript check makes it work after
a compaction without any setup; a /clear or a new session has a new transcript, so there the role comes from
CLAUDE_ROLE or from invoking /supervisor again.

Two rules keep the injection small enough for Claude Code to hold it inline (a larger hook output is persisted to a
file with a short preview, so the files would not be in context):

1. No duplicate skill bodies. On `compact` the harness re-lists the body of every skill the session invoked. A file
   in the list that is a skill body (`.claude/skills/<name>/SKILL.md`) is therefore not printed again when the
   transcript shows that skill invoked (Skill tool call with `skill: <name>`, or
   `<command-name>/<name></command-name>`); a one-line note `skill <name>: already in context` takes its place.
   `<name>` is the skill's folder name or the `name:` of its front matter. On startup, resume and clear every skill
   body is printed.
2. Total budget. `reload.budget_bytes` in .claude/board/board.json (default 20000) limits the printed file blocks
   (header plus content, UTF-8 bytes). Handover files (names ending in `Handover.md`) are never dropped: their size
   is reserved first, although they still print in list order. The other files are tried in list order; one that
   does not fit is replaced by a one-line pointer (path, size, "read it") and the next, smaller one may still fit.
   The last line of the output lists what was printed (block sizes, the same total the budget counts) and what was
   pointed to (content size). Notes and pointers are not counted against the budget.

Stdout is added to Claude's context. Stdlib only, Python 3.7+. Never blocks the session: always exits 0.
"""
import json
import os
import re
import sys
from pathlib import Path

ROLE = "supervisor"
SKILL = "supervisor"
FILES = (
    Path(".claude/skills/supervisor/SKILL.md"),
    Path("AI/SupervisorHandover.md"),
)
LOCAL_CONFIG = Path(".claude/board/local.json")
BOARD_CONFIG = Path(".claude/board/board.json")
DEFAULT_BUDGET = 20000
HANDOVER_SUFFIX = "Handover.md"
COMMAND_MARK = re.compile(r"<command-name>/([^<>\s]+)</command-name>")


def _invoked_skills(entry: dict) -> set:
    """The skills one transcript entry invokes (Skill tool call or slash command)."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return set()
    content = message.get("content")
    user = message.get("role") == "user"
    if isinstance(content, str):
        return set(COMMAND_MARK.findall(content)) if user else set()
    if not isinstance(content, list):
        return set()
    names = set()
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use" and item.get("name") == "Skill":
            name = (item.get("input") or {}).get("skill")
            if isinstance(name, str):
                names.add(name)
        if user and item.get("type") == "text":
            names.update(COMMAND_MARK.findall(item.get("text", "")))
    return names


def invoked_skills(transcript_path) -> set:
    """The names of all skills the transcript shows invoked; empty when there is no readable transcript."""
    names = set()
    if not transcript_path:
        return names
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                # Cheap filter first: most lines are tool output.
                if "Skill" not in line and "<command-name>" not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    names |= _invoked_skills(entry)
    except OSError:
        pass
    return names


def local_reload_list(root: Path) -> list:
    """The repo-relative paths listed under "reload" in the untracked local board config.

    A missing or unreadable file, a non-object, a missing key or a malformed list changes nothing: only the
    valid string entries are used (the board validates the file; the hook never fails on it).
    """
    try:
        with open(root / LOCAL_CONFIG, encoding="utf-8") as handle:
            local = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = local.get("reload") if isinstance(local, dict) else None
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        norm = entry.replace("\\", "/")
        # Same rule as the board's local.json validation: no leading slash, no drive (":"), no "..".
        if norm.startswith("/") or ":" in norm or ".." in norm.split("/"):
            continue
        path = Path(norm)
        # Each file once: not a public file, not a repeat.
        if path in FILES or path in result:
            continue
        result.append(path)
    return result


def budget_bytes(root: Path) -> int:
    """`reload.budget_bytes` of board.json; the default when the file, the key or the value is missing or invalid."""
    try:
        with open(root / BOARD_CONFIG, encoding="utf-8") as handle:
            config = json.load(handle)
        value = config["reload"]["budget_bytes"]
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_BUDGET
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return DEFAULT_BUDGET


def skill_names(rel: Path, text: str) -> set:
    """The names a skill body goes by (folder name, front-matter name); empty when `rel` is not a skill body."""
    parts = rel.parts
    if len(parts) != 4 or parts[:2] != (".claude", "skills") or parts[3] != "SKILL.md":
        return set()
    names = {parts[2]}
    front = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    if front:
        found = re.search(r"^name:\s*(\S.*?)\s*$", front.group(1), re.M)
        if found:
            names.add(found.group(1).strip("\"'"))
    return names


def size_of(text: str) -> int:
    return len(text.encode("utf-8"))


def build_blocks(root: Path, rels: list, budget: int, skip_skills: set) -> tuple:
    """(blocks, printed, pointed, skipped): the output text per file, plus (rel, size) lists and skill names."""
    loaded = []  # (rel, text or None, error or None, skill name already in context or None)
    for rel in rels:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            loaded.append((rel, None, exc.strerror or str(exc), None))
            continue
        covered = sorted(skill_names(rel, text) & skip_skills)
        loaded.append((rel, text, None, covered[0] if covered else None))

    def block_size(rel, text):
        return size_of(f"\n===== {rel.as_posix()} =====\n{text}\n")

    # Handover files are never dropped: reserve their size before anything else may use the budget.
    used = sum(block_size(rel, text) for rel, text, _, skill in loaded
               if text is not None and skill is None and rel.name.endswith(HANDOVER_SUFFIX))
    blocks, printed, pointed, skipped = [], [], [], []
    for rel, text, error, skill in loaded:
        head = f"\n===== {rel.as_posix()} =====\n"
        if text is None:
            # Never block the session; tell the model what is missing instead.
            blocks.append(head + f"(could not read {rel.as_posix()}: {error})\n")
        elif skill is not None:
            blocks.append(head + f"skill {skill}: already in context\n")
            skipped.append(skill)
        else:
            size = block_size(rel, text)
            if rel.name.endswith(HANDOVER_SUFFIX):
                fits = True  # reserved above
            elif used + size <= budget:
                used += size
                fits = True
            else:
                fits = False
            if fits:
                blocks.append(head + text + "\n")
                printed.append((rel.as_posix(), size))
            else:
                blocks.append(head + f"(not printed: {size_of(text)} bytes, over the {budget}-byte reload budget; "
                              f"read {rel.as_posix()})\n")
                pointed.append((rel.as_posix(), size_of(text)))
    return blocks, printed, pointed, skipped


def summary_line(printed: list, pointed: list, skipped: list, budget: int) -> str:
    def listing(items):
        return ", ".join(f"{name} ({size} B)" for name, size in items) or "none"
    total = sum(size for _, size in printed)
    line = (f"[reload hook: printed {listing(printed)} = {total} B of {budget} B budget; "
            f"pointers to {listing(pointed)}")
    if skipped:
        line += "; skills already in context: " + ", ".join(skipped)
    return line + "]"


def main() -> int:
    # Drain the hook payload; "source" tells us startup/resume/compact/clear.
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    source = payload.get("source", "unknown")

    invoked = invoked_skills(payload.get("transcript_path"))
    if os.environ.get("CLAUDE_ROLE") == ROLE:
        why = "CLAUDE_ROLE"
    elif SKILL in invoked:
        why = "transcript"
    else:
        return 0

    # Force UTF-8 so umlauts/accents and arrows survive on Windows consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or Path.cwd())
    budget = budget_bytes(root)
    # Only a compaction makes the harness re-list the bodies of the invoked skills.
    skip_skills = invoked if source == "compact" else set()

    print(f"[reload hook: session {source}, role={ROLE} (from {why})]")
    print(f"Role: {ROLE}")
    blocks, printed, pointed, skipped = build_blocks(root, list(FILES) + local_reload_list(root), budget,
                                                     skip_skills)
    for block in blocks:
        print(block, end="")
    print("\n" + summary_line(printed, pointed, skipped, budget))
    return 0


if __name__ == "__main__":
    sys.exit(main())
