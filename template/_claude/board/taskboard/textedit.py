"""
Pure edits of task file text: text in, edited text out, EditError when the edit is not possible.

Every edit changes only the lines it touches (each line keeps its own line ending; a new line gets the file's
dominant one), sets `updated`, and appends a Log line. Used by the CLI (the supervisor's commands) and by the
web board (the user's answers and notes).
"""

import re

from .model import QUESTION, effective_owner, latest_owner_decision, parse_task

MAX_INPUT_LENGTH = 2000
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class EditError(Exception):
    """An edit that can't be made; the message is meant for the person or agent who asked for it."""
    status = 400


class ForbiddenError(EditError):
    status = 403


class NotFoundError(EditError):
    status = 404


class ConflictError(EditError):
    status = 409


class WriteError(EditError):
    status = 500


def clean_input(value: str | None, what: str, max_length: int = MAX_INPUT_LENGTH) -> str:
    """
    Text as one line: whitespace runs (newlines and line separators included) become one space, other control
    characters are removed; non-empty, bounded. One line per entry keeps a Log or Decisions item one item and
    makes it impossible to inject a `## ` heading.
    """
    text = " ".join(_CONTROL.sub("", " ".join((value or "").split())).split())
    if not text:
        raise EditError(f"The {what} is empty.")
    if len(text) > max_length:
        raise EditError(f"The {what} is longer than {max_length} characters.")
    return text


def yaml_value(value) -> str:
    """A header value as written: null, a number, a [list] or text (quoted when the header parser needs it)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        raise EditError("Header values can't be true/false.")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    text = str(value)
    needs_quotes = (": " in text or " #" in text or text[:1] in ('"', "'", "`", "[", "#", "-", " ")
                    or text != text.strip() or text in ("", "null", "~") or re.fullmatch(r"-?\d+", text))
    if not needs_quotes:
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Lines:
    """A file as lines plus each line's own ending, so an edit changes only the lines it touches."""

    def __init__(self, text: str):
        parts = re.split(r"(\r\n|\n)", text)
        self.lines: list[str] = parts[0::2]
        self.endings: list[str] = parts[1::2] + [""]
        crlf = self.endings.count("\r\n")
        self.newline = "\r\n" if crlf > self.endings.count("\n") else "\n"

    def insert(self, index: int, line: str) -> None:
        """Insert a line before index; a new line gets the file's dominant ending."""
        if index >= len(self.lines):
            self.endings[-1] = self.newline
            self.lines.append(line)
            self.endings.append("")
        else:
            self.lines.insert(index, line)
            self.endings.insert(index, self.newline)

    def text(self) -> str:
        return "".join(line + ending for line, ending in zip(self.lines, self.endings))


def _header_end(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        raise EditError("The file has no header.")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i
    raise EditError("The file header is not closed.")


def get_header_raw(lines: list[str], key: str) -> str | None:
    end = _header_end(lines)
    for i in range(1, end):
        name, sep, rest = lines[i].partition(":")
        if sep and name.strip() == key:
            return rest.strip()
    return None


def set_header(lines: list[str], key: str, value: str) -> None:
    """Replace a header value (already formatted, see yaml_value) in place; a trailing `# comment` stays."""
    end = _header_end(lines)
    for i in range(1, end):
        name, sep, rest = lines[i].partition(":")
        if sep and name.strip() == key:
            m = re.search(r"\s+#.*$", rest) if rest.strip()[:1] not in ('"', "'") else None
            comment = m.group(0) if m else ""
            lines[i] = f"{key}: {value}{comment}"
            return
    raise EditError(f"The header has no '{key}' field.")


def _section(lines: list[str], name: str) -> tuple[int, int]:
    """(heading index, index after the section's last line) of `## name`; headings inside ``` are skipped."""
    start = _header_end(lines) + 1
    headings: list[int] = []
    in_code = False
    for i in range(start, len(lines)):
        if lines[i].lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and lines[i].startswith("## "):
            headings.append(i)
    heading = next((i for i in headings if lines[i].rstrip() == f"## {name}"), None)
    if heading is None:
        raise EditError(f"The file has no '## {name}' section.")
    end = next((i for i in headings if i > heading), len(lines))
    return heading, end


def append_to_section(doc: Lines, name: str, new_line: str) -> None:
    lines = doc.lines
    heading, end = _section(lines, name)
    last = max((i for i in range(heading + 1, end) if lines[i].strip()), default=heading)
    doc.insert(last + 1, new_line)


def replace_section_body(doc: Lines, name: str, body: str) -> None:
    """Replace everything below `## name` up to the next section with body (one blank line before the next)."""
    lines = doc.lines
    heading, end = _section(lines, name)
    for _ in range(heading + 1, end):
        del doc.lines[heading + 1]
        del doc.endings[heading + 1]
    new = body.replace("\r\n", "\n").strip("\n").split("\n") if body.strip() else []
    has_next = heading + 1 < len(doc.lines)
    for offset, line in enumerate(new + ([""] if has_next else [])):
        doc.insert(heading + 1 + offset, line)


def open_questions(text: str, for_role: str | None = None) -> list[dict]:
    task = parse_task(text, "", "", "")
    return [q for q in task["questions"] if q["status"] == "open" and (for_role is None or q["for"] == for_role)]


def _touch(doc: Lines, today: str) -> None:
    set_header(doc.lines, "updated", today)


def _sync_waiting_on(doc: Lines, otherwise: str | None, always: bool = False) -> None:
    """waiting_on is user while a question for the user is open; else `otherwise` when it was user (or always)."""
    if open_questions(doc.text(), "user"):
        set_header(doc.lines, "waiting_on", "user")
        return
    current = get_header_raw(doc.lines, "waiting_on") or ""
    if always or current.split("#")[0].strip() == "user":
        set_header(doc.lines, "waiting_on", yaml_value(otherwise))


def log_line(today: str, actor: str, text: str) -> str:
    return f"- {today} {actor}: {text}"


def add_log(text: str, actor: str, entry: str, today: str) -> str:
    entry = clean_input(entry, "log entry")
    doc = Lines(text)
    append_to_section(doc, "Log", log_line(today, actor, entry))
    _touch(doc, today)
    return doc.text()


def add_note(text: str, note: str, today: str, actor: str = "user (board)") -> str:
    """A free note through the board (actor `<name> (board)`): a Log line, and waiting_on supervisor unless the
    user owes an answer."""
    note = clean_input(note, "note")
    doc = Lines(text)
    append_to_section(doc, "Log", log_line(today, actor, note))
    _touch(doc, today)
    _sync_waiting_on(doc, "supervisor", always=True)
    return doc.text()


def addressed(name: str, question: str) -> str:
    """A question for the user addressed to one person: `@<name>: <question>` (kept if already addressed)."""
    question = clean_input(question, "question")
    prefix = f"@{name}:"
    return question if question.startswith(prefix) else f"{prefix} {question}"


def ask_question(text: str, for_role: str, question: str, today: str, actor: str) -> tuple[str, str]:
    """Add `- **Q<n>** (open, for <role>): <question>` with the next number; returns (text, question id)."""
    question = clean_input(question, "question")
    task = parse_task(text, "", "", "")
    numbers = [int(q["id"][1:]) for q in task["questions"]]
    for line in task["sections"].get("Questions", "").split("\n"):
        m = re.match(r"^- \*\*Q(\d+)\*\*", line)
        if m:
            numbers.append(int(m.group(1)))
    qid = f"Q{max(numbers, default=0) + 1}"
    doc = Lines(text)
    append_to_section(doc, "Questions", f"- **{qid}** (open, for {for_role}): {question}")
    append_to_section(doc, "Log", log_line(today, actor, f"asked {qid} (for {for_role})."))
    _touch(doc, today)
    if for_role == "user":
        set_header(doc.lines, "waiting_on", "user")
    return doc.text(), qid


def answer_question(text: str, question_id: str, answer: str, today: str, by: str = "user",
                    actor: str = "user (board)", decision: bool = True, waiting_after: str | None = "supervisor",
                    via: str | None = None, waiting_always: bool = True) -> str:
    """
    Answer an open question as `by` (a person or a role; anyone may answer any open question, the callers check
    the name): the question becomes `(answered <today>, <by>, for <role>): ... → <answer>`, a Decisions line
    (when decision) and a Log line are added, `updated` is set, and waiting_on stays user while another question
    for the user is open, else becomes waiting_after (when waiting_always, or when it was user).
    """
    answer = clean_input(answer, "answer")
    doc = Lines(text)
    lines = doc.lines
    heading, end = _section(lines, "Questions")
    first = role = None
    for i in range(heading + 1, end):
        m = QUESTION.match(lines[i])
        if m and m.group(1) == question_id:
            if m.group(2) is None:
                raise EditError(f"{question_id} is already answered.")
            first, role = i, m.group(2)
            break
    if first is None:
        raise EditError(f"There is no question {question_id}.")
    last = first
    while last + 1 < end and lines[last + 1].strip() and lines[last + 1][:1] in (" ", "\t"):
        last += 1
    lines[first] = lines[first].replace(f"(open, for {role})", f"(answered {today}, {by}, for {role})", 1)
    lines[last] = f"{lines[last].rstrip()} → {answer}"
    if decision:
        append_to_section(doc, "Decisions", f"- {today}, {by}: {question_id} → {answer}")
    suffix = f" ({via})" if via else ""
    append_to_section(doc, "Log", log_line(today, actor, f"answered {question_id}{suffix}."
                                           if actor in (by, f"{by} (board)") else
                                           f"recorded {by}'s answer to {question_id}{suffix}."))
    _touch(doc, today)
    _sync_waiting_on(doc, waiting_after, waiting_always)
    return doc.text()


def add_decision(text: str, by: str, decision: str, today: str, actor: str) -> str:
    decision = clean_input(decision, "decision")
    doc = Lines(text)
    append_to_section(doc, "Decisions", f"- {today}, {by}: {decision}")
    append_to_section(doc, "Log", log_line(today, actor, f"recorded a decision by {by}."))
    _touch(doc, today)
    return doc.text()


def set_fields(text: str, values: dict, today: str, actor: str, log: bool = True) -> str:
    """Set header fields (values already parsed: None, int, list or text) and log which ones changed."""
    doc = Lines(text)
    changed = []
    for key, value in values.items():
        new = yaml_value(value)
        old = (get_header_raw(doc.lines, key) or "")
        if old.split(" #")[0].strip() != new:
            set_header(doc.lines, key, new)
            changed.append(f"{key} = {new}")
    if not changed:
        return text
    if log:
        append_to_section(doc, "Log", log_line(today, actor, "set " + ", ".join(changed) + "."))
    _touch(doc, today)
    return doc.text()


def set_section(text: str, name: str, body: str, today: str, actor: str) -> str:
    doc = Lines(text)
    replace_section_body(doc, name, body)
    append_to_section(doc, "Log", log_line(today, actor, f"rewrote {name}."))
    _touch(doc, today)
    return doc.text()


def _header_index(lines: list[str], key: str) -> int | None:
    end = _header_end(lines)
    for i in range(1, end):
        name, sep, _ = lines[i].partition(":")
        if sep and name.strip() == key:
            return i
    return None


def set_owner(text: str, owner: str | None, giver: str, today: str, actor: str, default_owner: str | None,
              forced: str | None = None, transfer: bool = True) -> str:
    """
    Set the owner of a task to owner (None: null, i.e. default_owner): the header, a Decisions line and the Log
    lines. Unchanged text when the effective owner stays the same. With transfer (strict ownership) the line is
    `- <today>, <giver>: owner: <new> (transferred from <old>)` with effective names (`; forced: <reason>` when
    forced past the owner rule; the caller checks that giver may transfer it); without (open ownership, anyone
    may set the owner) it is `- <today>, <giver>: owner: <new> (set by <giver>, from <old>)`.
    """
    doc = Lines(text)
    if _header_index(doc.lines, "owner") is None:
        raise EditError("The header has no 'owner' field yet; run: tb migrate-owner")
    old = effective_owner(parse_task(text, "", "", "")["header"], default_owner)
    new = owner or default_owner
    if old == new:
        return text
    stored = None if owner == default_owner else owner
    set_header(doc.lines, "owner", yaml_value(stored))
    if transfer:
        details = f"transferred from {old}" if old else ""
        if forced:
            details += ("; " if details else "") + "forced: " + clean_input(forced, "reason")
    else:
        details = f"set by {giver}" + (f", from {old}" if old else "")
    decision = f"owner: {new or 'null'}" + (f" ({details})" if details else "")
    append_to_section(doc, "Decisions", f"- {today}, {giver}: {decision}")
    append_to_section(doc, "Log", log_line(today, actor, f"set owner = {yaml_value(stored)}."))
    append_to_section(doc, "Log", log_line(today, actor, f"recorded a decision by {giver}."))
    _touch(doc, today)
    return doc.text()


def migrate_owner(text: str, default_owner: str | None) -> tuple[str, str | None]:
    """
    (text with an `owner` header line, the value written) for a file without one: the owner the latest
    ownership line in Decisions names, null when there is none or it names default_owner; inserted after
    `priority` (else before the closing `---`). Nothing else changes (no Log line, no `updated`). A file that
    already has the field comes back unchanged with its value.
    """
    doc = Lines(text)
    if _header_index(doc.lines, "owner") is not None:
        return text, parse_task(text, "", "", "")["header"].get("owner")
    _, decided = latest_owner_decision(parse_task(text, "", "", "")["sections"].get("Decisions", ""))
    value = None if decided == default_owner else decided
    after = _header_index(doc.lines, "priority")
    doc.insert(after + 1 if after is not None else _header_end(doc.lines), f"owner: {yaml_value(value)}")
    return doc.text(), value


def new_task_text(task_id: str, title: str, task_type: str, priority: str, today: str, state: str,
                  actor: str, problem: str = "", waiting_on: str | None = None, newline: str = "\n",
                  owner: str | None = None, review_field: str = "pr") -> str:
    """The text of a new task file; review_field is the header field of the code review (`pr` or `rev`)."""
    lines = [
        "---",
        f"id: {task_id}",
        f"title: {yaml_value(clean_input(title, 'title', 200))}",
        f"type: {task_type}",
        f"priority: {priority}",
        f"owner: {yaml_value(owner)}",
        f"created: {today}",
        f"updated: {today}",
        f"waiting_on: {yaml_value(waiting_on)}",
        "blocked_by: []",
        "plan: null",
        "branch: null",
        f"{review_field}: null",
        "---",
        "",
        "## Problem",
        *(problem.replace("\r\n", "\n").strip("\n").split("\n") if problem.strip() else []),
        "",
        "## Approach",
        "",
        "## Acceptance",
        "",
        "## Questions",
        "",
        "## Decisions",
        "",
        "## Log",
        log_line(today, actor, f"created in {state}" + (f" (owner {owner})." if owner else ".")),
        "",
    ]
    return newline.join(lines)


def slugify(title: str, max_words: int = 6) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    stop = {"a", "an", "the", "of", "for", "to", "in", "on", "and", "or", "is", "are", "with"}
    kept = [w for w in words if w not in stop] or words
    return "-".join(kept[:max_words]) or "task"
