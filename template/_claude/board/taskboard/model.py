"""
Task files: parsing and the consistency check.

A task source is a folder with one subfolder per state (STATES); every task is one Markdown file
`<ID>-<slug>.md` in the folder of its state. The format is defined in AI/Tasks/README.md.

`load_tasks()` is the one parsing function; it returns plain data (lists and dicts of strings, ints and None),
meant to be reused by every front end (CLI, web board, watcher). `check_tasks()` works on that data only.
Neither raises for a malformed file; problems are reported as text.
"""

import os
import re
from datetime import date
from typing import Any

STATES = [
    "00-Conception",
    "01-Refinement",
    "02-Ready",
    "03-Implementation",
    "04-CodeReview",
    "05-UserReview",
    "06-Deploy",
    "07-Done",
    "08-Dropped",
]
# The header fields every task file has; the last one names what is handed to the code review and depends on the
# version control: `pr` (a pull request number, git) or `rev` (a branch revision, svn), see required_fields().
BASE_FIELDS = ["id", "title", "type", "priority", "owner", "created", "updated", "waiting_on", "blocked_by",
               "plan", "branch"]
REVIEW_FIELD_NAMES = ("pr", "rev")
DEFAULT_REVIEW_FIELD = "pr"
REQUIRED_FIELDS = BASE_FIELDS + [DEFAULT_REVIEW_FIELD]
REVIEW_FIELD_WHAT = {"pr": "a pull request number", "rev": "a revision number"}
REVIEW_LABELS = {"pr": "PR #", "rev": "r"}


def required_fields(review_field: str = DEFAULT_REVIEW_FIELD) -> list[str]:
    """The header fields of a task file for a review field (`pr` or `rev`)."""
    return BASE_FIELDS + [review_field]
REQUIRED_SECTIONS = ["Problem", "Approach", "Acceptance", "Questions", "Decisions", "Log"]
PRIORITIES = ["high", "medium", "low"]
DEFAULT_TYPES = ["bug", "feature", "refactor", "docs", "research", "ops"]
DEFAULT_ROLES = ["user", "supervisor", "executor", "reviewer", "researcher"]

ID_PATTERN = re.compile(r"^([A-Z]+)(\d{3,})$")
FILE_PATTERN = re.compile(r"^([A-Z]+\d{3,})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
# Groups: 1 id; open: 2 the role; answered: 3 date, 4 who answered, 5 the role it was for (absent in the old
# form `(answered <date>, <who>)`, where who answered is also who it was for).
QUESTION = re.compile(
    r"^- \*\*(Q\d+)\*\* \((?:open, for ([\w-]+)|answered (\d{4}-\d{2}-\d{2}), ([\w-]+)(?:, for ([\w-]+))?)\):")
# An ownership decision: `- <date>, <who>: owner: <name>` with an optional ` (transferred from <old>...)`. Group 1
# is the new owner; `null` means unowned (a board without default_owner).
OWNER_DECISION = re.compile(r"^- \d{4}-\d{2}-\d{2}, [\w-]+: owner: ([\w-]+)(?=$|\s)")
# A question for the user addressed to one person: `@<name>: ...`.
ADDRESSED = re.compile(r"^@([\w-]+):")


def default_schema() -> dict:
    return {"types": list(DEFAULT_TYPES), "roles": list(DEFAULT_ROLES), "people": [], "default_owner": None}


def effective_owner(header: dict, default_owner: str | None) -> str | None:
    """Who owns a task: its `owner` when set, else default_owner (None: unowned)."""
    owner = header.get("owner")
    return owner if isinstance(owner, str) and owner else default_owner


def latest_owner_decision(decisions: str) -> tuple[bool, str | None]:
    """(found, owner) of the latest ownership line in a Decisions section; owner None for `owner: null`."""
    found, owner = False, None
    for line in decisions.split("\n"):
        m = OWNER_DECISION.match(line.strip())
        if m:
            found, owner = True, (None if m.group(1) == "null" else m.group(1))
    return found, owner


def waits_on_person(task: dict, person: str, default_owner: str | None) -> bool:
    """
    True when person has something to answer: an open question for the user addressed `@<person>:`, or one
    without an address on a task whose effective owner is person.
    """
    owner = effective_owner(task["header"], default_owner)
    for q in task.get("questions", []):
        if q["status"] != "open" or q["for"] != "user":
            continue
        m = ADDRESSED.match(q["text"])
        if (m.group(1) == person) if m else (owner == person):
            return True
    return False


def filter_board(board: dict, owner: str, default_owner: str | None) -> dict:
    """board with only the tasks whose effective owner is owner (with default_owner, its null tasks too)."""
    return {**board, "tasks": [t for t in board["tasks"] if effective_owner(t["header"], default_owner) == owner]}


def _strip_comment(raw: str) -> str:
    """Drop a trailing `# comment`; a value that starts with a quote ends at its closing quote."""
    value = raw.strip()
    if value[:1] in ('"', "'"):
        quote, i = value[0], 1
        while i < len(value):
            if quote == '"' and value[i] == "\\":
                i += 2
                continue
            if value[i] == quote:
                if quote == "'" and value[i + 1:i + 2] == "'":
                    i += 2
                    continue
                return value[:i + 1]
            i += 1
        return value
    m = re.search(r"(^|\s)#", value)
    return value[:m.start()].rstrip() if m else value


def _parse_scalar(value: str) -> Any:
    if value in ("", "null", "~"):
        return None
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [item.strip().strip("\"'") for item in inner.split(",")] if inner else []
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _parse_task_text(text: str) -> tuple[dict, dict, dict, list[str]]:
    """Split one task file into (header fields, raw header values, sections by heading, parse errors)."""
    lines = text.replace("\r\n", "\n").split("\n")
    errors: list[str] = []
    header: dict = {}
    raw: dict = {}
    if not lines or lines[0].strip() != "---":
        return header, raw, {}, ["header: file does not start with '---'"]
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return header, raw, {}, ["header: no closing '---'"]
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, rest = line.partition(":")
        key = key.strip()
        if not sep or not re.fullmatch(r"[a-z_]+", key):
            errors.append(f"header: cannot parse line {line!r}")
            continue
        if key in header:
            errors.append(f"header: field {key!r} appears twice")
        raw[key] = _strip_comment(rest)
        header[key] = _parse_scalar(raw[key])
    sections: dict = {}
    current = None
    in_code = False
    for line in lines[end + 1:]:
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^## (.+?)\s*$", line)
        if m:
            current = m.group(1)
            if current in sections:
                errors.append(f"section {current!r} appears twice")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return header, raw, {k: "\n".join(v).strip() for k, v in sections.items()}, errors


def parse_questions(text: str) -> tuple[list[dict], list[str]]:
    """
    Questions as [{"id", "status": "open"|"answered", "for", "answered", "answered_by", "for_given", "text"}],
    plus malformed lines. "for" is the role the question was asked for, "answered_by" who answered (None while
    open). In the old answered form `(answered <date>, <who>)` both are <who> and "for_given" is False.
    """
    questions: list[dict] = []
    errors: list[str] = []
    for line in text.split("\n"):
        if not line.startswith("- **Q"):
            continue
        m = QUESTION.match(line)
        if not m:
            errors.append("question not in the form '(open, for <role>)' or "
                          f"'(answered <date>, <who>, for <role>)': {line[:60]!r}")
        elif m.group(2):
            questions.append({"id": m.group(1), "status": "open", "for": m.group(2), "answered": None,
                              "answered_by": None, "for_given": True, "text": line[m.end():].strip()})
        else:
            questions.append({"id": m.group(1), "status": "answered", "for": m.group(5) or m.group(4),
                              "answered": m.group(3), "answered_by": m.group(4),
                              "for_given": m.group(5) is not None, "text": line[m.end():].strip()})
    return questions, errors


def parse_task(text: str | None, state: str, name: str, path: str, read_error: str | None = None) -> dict:
    """One load_tasks() entry for a task file's text, or for a file that could not be read."""
    if text is None:
        header, raw, sections, parse_errors = {}, {}, {}, [read_error or "cannot read file"]
    else:
        header, raw, sections, parse_errors = _parse_task_text(text)
    questions, question_errors = parse_questions(sections.get("Questions", ""))
    return {
        "state": state,
        "file": name,
        "path": path,
        "header": header,
        "header_raw": raw,
        "sections": sections,
        "questions": questions,
        "errors": parse_errors + question_errors,
    }


def load_tasks(tasks_dir: str) -> dict:
    """
    Read every task file under the state folders of tasks_dir.

    Returns plain data:
        {"states": [state folder names],
         "tasks": [{"state", "file", "path", "header", "header_raw", "sections", "questions", "errors"}, ...],
         "errors": [problems that belong to no single task, e.g. an unknown folder]}
    "header_raw" holds each header value as written (comment removed, quotes kept). Files directly in
    tasks_dir (README.md), hidden files (`.gitkeep`, temporary files) and subfolders of a state folder are
    ignored. A file that can't be read or decoded still gets an entry, with the reason in its "errors".
    """
    tasks: list[dict] = []
    errors: list[str] = []
    for entry in sorted(os.listdir(tasks_dir)):
        full = os.path.join(tasks_dir, entry)
        if os.path.isdir(full) and entry not in STATES and not entry.startswith("."):
            errors.append(f"{entry}/: not a state folder")
    for state in STATES:
        folder = os.path.join(tasks_dir, state)
        if not os.path.isdir(folder):
            errors.append(f"{state}/: missing state folder")
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.startswith(".") or os.path.isdir(path):
                continue
            rel_path = os.path.relpath(path, tasks_dir).replace(os.sep, "/")
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError) as exc:
                tasks.append(parse_task(None, state, name, rel_path, read_error=f"cannot read file: {exc}"))
            else:
                tasks.append(parse_task(text, state, name, rel_path))
    return {"states": list(STATES), "tasks": tasks, "errors": errors}


def is_date(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def is_id(value: Any) -> bool:
    return isinstance(value, str) and ID_PATTERN.match(value) is not None


def task_ids(board: dict) -> set[str]:
    return {t["header"]["id"] for t in board["tasks"] if is_id(t["header"].get("id"))}


class Problem(str):
    """
    A consistency problem: the message, plus `keys`, the stable (task file, kind, item) tuples it reports. A
    message that names several items (open questions Q1, Q2) has one key per item, so the same problem is
    recognised after its text changed (Q1 was answered, only Q2 is named now).
    """
    keys: frozenset


def problem(message: str, kind: str, file: str, items: tuple[str, ...]) -> Problem:
    result = Problem(message)
    result.keys = frozenset((file, kind, item) for item in items)
    return result


def check_tasks(board: dict, repo_root: str | None = None, schema: dict | None = None,
                prefix: str | None = None, known_ids: set[str] | None = None) -> list[str]:
    """
    Consistency problems of a load_tasks() result, one line each; empty when everything is valid.

    schema gives the allowed "types", "roles", "people" and the "default_owner" (default_schema()); who answered a
    question may be a role or a person, who it was asked for must be a role; `owner` is null or a person, and
    the latest ownership line in Decisions (if any) must name the effective owner. prefix, when given, is the id
    prefix every task of this source must carry. known_ids are ids of other sources that blocked_by and
    waiting_on may name. schema's "review_field" (`pr`, the default, or `rev`) names the review header field
    every task must have.
    """
    schema = schema or default_schema()
    types, roles = set(schema["types"]), set(schema["roles"])
    people = set(schema.get("people", []))
    default_owner = schema.get("default_owner")
    review = schema.get("review_field") or DEFAULT_REVIEW_FIELD
    fields = required_fields(review)
    names = roles | people
    problems: list[str] = [problem(e, "load", "", (e,)) for e in board["errors"]]

    def flag(task: dict, kind: str, message: str, *items: str) -> None:
        """Report one problem of task, keyed by (file, kind, item) for every item it names."""
        problems.append(problem(message, kind, task["file"], items or ("",)))

    ids: dict[str, str] = {}
    for task in board["tasks"]:
        where = task["path"]
        header = task["header"]
        for e in task["errors"]:
            flag(task, "parse", f"{where}: {e}", e)
        if not header and task["errors"]:
            continue
        m = FILE_PATTERN.match(task["file"])
        if not m:
            flag(task, "file-name", f"{where}: file name is not '<id>-<slug>.md' (lowercase slug)")
        missing = [f for f in fields if f not in header]
        if missing:
            flag(task, "missing-field", f"{where}: missing header field(s) {', '.join(missing)}", *missing)
        unknown = [f for f in header if f not in fields]
        if unknown:
            flag(task, "unknown-field", f"{where}: unknown header field(s) {', '.join(unknown)}", *unknown)
        task_id = header.get("id")
        if task_id is not None:
            if not is_id(task_id):
                flag(task, "invalid-id", f"{where}: invalid id {task_id!r}")
            else:
                if prefix and ID_PATTERN.match(task_id).group(1) != prefix:
                    flag(task, "id-prefix", f"{where}: id {task_id} does not carry this source's prefix {prefix}")
                if m and m.group(1) != task_id:
                    flag(task, "id-file-name", f"{where}: header id {task_id} does not match the file name")
                if task_id in ids:
                    flag(task, "duplicate-id", f"{where}: duplicate id {task_id} (also {ids[task_id]})", task_id)
                else:
                    ids[task_id] = where
        if "title" in header:
            title = header["title"]
            if not isinstance(title, str) or not title.strip():
                flag(task, "title-empty", f"{where}: title must be non-empty text, got {title!r}")
            else:
                raw_title = task.get("header_raw", {}).get("title", "")
                if raw_title[:1] not in ('"', "'") and ": " in raw_title:
                    flag(task, "title-quote", f"{where}: title contains ': ' and must be quoted")
        if "type" in header and not (isinstance(header["type"], str) and header["type"] in types):
            flag(task, "invalid-type", f"{where}: invalid type {header['type']!r}")
        if "priority" in header and not (isinstance(header["priority"], str)
                                         and header["priority"] in PRIORITIES):
            flag(task, "invalid-priority", f"{where}: invalid priority {header['priority']!r}")
        if "owner" in header:
            owner = header["owner"]
            if owner is not None and not isinstance(owner, str):
                flag(task, "owner-type", f"{where}: owner must be null or a person, got {owner!r}")
            elif owner is not None and owner not in people:
                flag(task, "owner-unknown", f"{where}: invalid owner {owner!r} (not in board.json people)")
            else:
                found, decided = latest_owner_decision(task["sections"].get("Decisions", ""))
                effective = effective_owner(header, default_owner)
                if found and decided != effective:
                    flag(task, "owner-decision", f"{where}: Decisions name owner {decided or 'null'}, header owner is "
                         f"{effective or 'null'}")
        for field in ("created", "updated"):
            if field in header and not is_date(header[field]):
                flag(task, "invalid-date", f"{where}: {field} is not a YYYY-MM-DD date: {header[field]!r}", field)
        if is_date(header.get("created")) and is_date(header.get("updated")) \
                and header["updated"] < header["created"]:
            flag(task, "updated-before-created", f"{where}: updated is before created")
        waiting = header.get("waiting_on")
        if waiting is not None and not (isinstance(waiting, str) and (waiting in roles or is_id(waiting))):
            flag(task, "invalid-waiting", f"{where}: invalid waiting_on {waiting!r}")
        blocked = header.get("blocked_by")
        if "blocked_by" in header and (not isinstance(blocked, list) or not all(is_id(b) for b in blocked)):
            flag(task, "invalid-blocked-by", f"{where}: blocked_by must be a list of task ids, got {blocked!r}")
        reviewed = header.get(review)
        if reviewed is not None and (not isinstance(reviewed, int) or isinstance(reviewed, bool) or reviewed <= 0):
            flag(task, "invalid-review", f"{where}: {review} must be null or {REVIEW_FIELD_WHAT[review]}, "
                 f"got {reviewed!r}")
        for field in ("plan", "branch"):
            if header.get(field) is not None and not isinstance(header[field], str):
                flag(task, "field-type", f"{where}: {field} must be null or text", field)
        plan = header.get("plan")
        if isinstance(plan, str) and repo_root is not None \
                and not os.path.isfile(os.path.join(repo_root, plan)):
            flag(task, "plan-missing", f"{where}: plan {plan} does not exist")
        missing_sections = [s for s in REQUIRED_SECTIONS if s not in task["sections"]]
        if missing_sections:
            flag(task, "missing-section", f"{where}: missing section(s) {', '.join(missing_sections)}",
                 *missing_sections)
        questions = task.get("questions", [])
        seen_q: set[str] = set()
        for q in questions:
            if q["id"] in seen_q:
                flag(task, "question-twice", f"{where}: question {q['id']} appears twice", q["id"])
            seen_q.add(q["id"])
            # The old answered form (no ", for <role>") predates named answerers: its answerer is a role.
            if q["for_given"] and q["answered_by"] is not None and q["answered_by"] not in names:
                flag(task, "answerer-unknown",
                     f"{where}: {q['id']} names unknown role or person {q['answered_by']!r}", q["id"])
            if q["for"] not in roles:
                flag(task, "for-unknown", f"{where}: {q['id']} names unknown role {q['for']!r}", q["id"])
            if q["answered"] is not None and not is_date(q["answered"]):
                flag(task, "answer-date", f"{where}: {q['id']} has an invalid answer date {q['answered']!r}", q["id"])
        open_for_user = [q["id"] for q in questions if q["status"] == "open" and q["for"] == "user"]
        if open_for_user and waiting != "user":
            flag(task, "open-for-user-not-waiting", f"{where}: open question(s) for the user "
                 f"({', '.join(open_for_user)}) but waiting_on is {waiting!r}, not user", *open_for_user)
        if waiting == "user" and not open_for_user:
            flag(task, "waiting-user-no-question",
                 f"{where}: waiting_on is user but no open question is for the user")
        if task["state"] in (STATES[-2], STATES[-1]):
            open_ids = [q["id"] for q in questions if q["status"] == "open"]
            if open_ids:
                flag(task, "finished-open-question",
                     f"{where}: open question(s) {', '.join(open_ids)} in a finished task", *open_ids)
            if waiting is not None:
                flag(task, "finished-waiting", f"{where}: waiting_on is {waiting!r} in a finished task, not null")
    known = set(ids) | (known_ids or set())
    for task in board["tasks"]:
        blocked = task["header"].get("blocked_by")
        for blocker in blocked if isinstance(blocked, list) else []:
            if is_id(blocker) and blocker not in known:
                flag(task, "blocker-unknown", f"{task['path']}: blocked_by names unknown task {blocker}", blocker)
        waiting = task["header"].get("waiting_on")
        if is_id(waiting) and waiting not in known:
            flag(task, "waiting-unknown", f"{task['path']}: waiting_on names unknown task {waiting}")
    return problems


def format_board(board: dict, label: str | None = None, review_field: str = DEFAULT_REVIEW_FIELD) -> str:
    """The board as text: one block per state, one line per task (a set owner shows as `owner <name>`, a set
    review field as `PR #77` or `r12`)."""
    out: list[str] = []
    if label:
        out.append(f"== {label} ==")
    for state in board["states"]:
        tasks = [t for t in board["tasks"] if t["state"] == state]
        out.append(f"{state} ({len(tasks)})")
        for t in tasks:
            h = t["header"]
            extra = []
            if h.get("owner"):
                extra.append(f"owner {h['owner']}")
            if h.get("waiting_on"):
                extra.append(f"waiting on {h['waiting_on']}")
            blocked = h.get("blocked_by")
            if blocked:
                items = blocked if isinstance(blocked, list) else [blocked]
                extra.append("blocked by " + ", ".join(str(b) for b in items))
            if h.get(review_field):
                extra.append(f"{REVIEW_LABELS[review_field]}{h[review_field]}")
            suffix = f"  [{'; '.join(extra)}]" if extra else ""
            task_id = str(h.get("id", t["file"]))
            out.append(f"  {task_id:<6} {str(h.get('priority', '?')):<6} {h.get('title', '')}{suffix}")
    return "\n".join(out)
