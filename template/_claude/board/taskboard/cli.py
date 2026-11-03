"""
Task board command line: the board, the consistency check, and every task-file change the supervisor makes.

Each write command reads the file, applies one edit (textedit), refuses it when it would break the format
(model.check_tasks) or the flow (workflow), and writes atomically on the main branch only (store). It sets
`updated` and appends the Log line itself, so a task change is one deterministic command, and it prints a
suggested commit message. It never commits: the supervisor commits task changes on the main line (git or
SVN, chosen by `vcs` in board.json; taskboard/vcs holds the backends).

Run `python .claude/board/tb.py --help` (from anywhere in the repo) for the commands.
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
from datetime import date

from . import textedit, workflow
from .config import (BoardConfigError, check_idle_minutes, check_update_seconds, load_board_config, local_path_of,
                     schema_of, validate_local)
from .model import (FILE_PATTERN, STATES, check_tasks, effective_owner, filter_board, format_board, is_id,
                    latest_owner_decision, load_tasks, parse_task, task_ids)
from .store import (apply_edit, atomic_write, content_hash, find_task_files, main_branch_block, move_file,
                    new_problems, next_id, task_write_lock)
from .textedit import EditError
from .vcs import REVIEW_FIELDS, backend, review_field

BASE_SETTABLE = ["title", "type", "priority", "owner", "waiting_on", "blocked_by", "plan", "branch"]
SETTABLE = BASE_SETTABLE + [r.name for r in REVIEW_FIELDS.values()]  # for the help text; settable() per config
OWNER_DECISION_TEXT = re.compile(r"^\s*owner\s*:", re.IGNORECASE)
EDITABLE_SECTIONS = ["Problem", "Approach", "Acceptance"]


class CliError(Exception):
    pass


def today() -> str:
    return date.today().isoformat()


def rel(config: dict, path: str) -> str:
    return os.path.relpath(path, config["repo_root_path"]).replace(os.sep, "/")


def settable(config: dict) -> list[str]:
    """The header fields `tb set` takes for this board: the base fields and the vcs's review field."""
    return BASE_SETTABLE + [review_field(config).name]


def strict_ownership(config: dict) -> bool:
    """board.json `ownership`: strict (the default) or open; see config.OWNERSHIP_MODES."""
    return config.get("ownership", "strict") == "strict"


def vcs_of(config: dict) -> str:
    return config.get("vcs") or "git"


def guard(config: dict, source: dict) -> None:
    """Refuse a write outside a checkout of the main line (the backend's guard)."""
    block = main_branch_block(source["root_path"], config["main_branch"], vcs_of(config))
    if block:
        raise CliError(block)


def source_for_id(config: dict, task_id: str) -> dict:
    if not is_id(task_id):
        raise CliError(f"{task_id!r} is not a task id (e.g. T012)")
    prefix = re.match(r"[A-Z]+", task_id).group(0)
    for source in config["sources"]:
        if source["prefix"] == prefix:
            return source
    raise CliError(f"No source has the prefix {prefix} (sources: "
                   + ", ".join(f"{s['name']}={s['prefix']}" for s in config["sources"]) + ")")


def source_by_name(config: dict, name: str | None) -> dict:
    if name is None:
        return config["sources"][0]
    for source in config["sources"]:
        if source["name"] == name:
            return source
    raise CliError(f"No source named {name!r}")


def locate(config: dict, task_id: str) -> tuple[dict, str, str]:
    """(source, state, file name) of a task."""
    task_id = task_id.upper()
    source = source_for_id(config, task_id)
    found = find_task_files(source["root_path"], task_id)
    if not found:
        raise CliError(f"No task file for {task_id} in {rel(config, source['root_path'])}")
    if len(found) > 1:
        raise CliError(f"More than one file for {task_id}: " + ", ".join(f"{s}/{n}" for s, n in found))
    return source, found[0][0], found[0][1]


def read_task(source: dict, state: str, name: str) -> tuple[str, dict]:
    with open(os.path.join(source["root_path"], state, name), encoding="utf-8") as f:
        text = f.read()
    return text, parse_task(text, state, name, f"{state}/{name}")


def all_ids(config: dict) -> set[str]:
    ids: set[str] = set()
    for source in config["sources"]:
        ids |= task_ids(load_tasks(source["root_path"]))
    return ids


def check_all(config: dict) -> tuple[list[tuple[dict, dict]], list[str]]:
    boards = [(s, load_tasks(s["root_path"])) for s in config["sources"]]
    known = set().union(*(task_ids(b) for _, b in boards))
    problems: list[str] = []
    many = len(boards) > 1
    for source, board in boards:
        for p in check_tasks(board, config["repo_root_path"], schema_of(config), source["prefix"], known):
            problems.append(f"{source['name']}: {p}" if many else p)
    return boards, problems


def handover_warnings(config: dict) -> list[str]:
    """One line per measure of the handover file that is over its limit. Silent without the key or the file."""
    limits = config.get("handover")
    if not limits:
        return []
    try:
        with open(limits["path_abs"], encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    name = limits["path"]
    lines = text.splitlines()
    warnings = []
    if len(lines) > limits["max_lines"]:
        warnings.append(f"{name} has {len(lines)} lines, {len(lines) - limits['max_lines']} over "
                        f"the limit of {limits['max_lines']}")
    if len(text) > limits["max_chars"]:
        warnings.append(f"{name} has {len(text)} characters, {len(text) - limits['max_chars']} over "
                        f"the limit of {limits['max_chars']}")
    long = [len(line) for line in lines if len(line) > limits["max_line_chars"]]
    if long:
        warnings.append(f"{name} has {len(long)} line(s) over {limits['max_line_chars']} characters "
                        f"(longest {max(long)})")
    return warnings


def report_edit(config: dict, source: dict, state: str, name: str, what: str, commit: str,
                problems: list[str]) -> int:
    print(f"{what}\nfile: {rel(config, os.path.join(source['root_path'], state, name))}")
    for p in problems:
        print(f"  problem: {p}", file=sys.stderr)
    print(f"commit: tasks: {commit}")
    return 1 if problems else 0


def edit_task(config: dict, task_id: str, edit, what: str, commit: str) -> int:
    source, state, name = locate(config, task_id)
    problems = apply_edit(source, state, name, None, edit, config["main_branch"], schema_of(config),
                          config["repo_root_path"], vcs=vcs_of(config))
    return report_edit(config, source, state, name, what, commit, problems)


def read_text_arg(text: str | None, file: str | None) -> str:
    if file is not None:
        if file == "-":
            return sys.stdin.read()
        with open(file, encoding="utf-8") as f:
            return f.read()
    return text or ""


def person_or_role(config: dict, name: str | None, option: str) -> str:
    """
    Who answered or decided: the name given. With strict ownership the flag is mandatory (no default to
    `tb me`); with open ownership it defaults to this checkout's person (`tb me`), else `user`.
    """
    if name is None and not strict_ownership(config):
        name = config.get("me") or "user"
    if name is None:
        raise CliError(f"{option} is needed: a person in board.json people "
                       f"({', '.join(config.get('people', [])) or 'none configured'}) or a role "
                       f"({', '.join(config['roles'])})")
    if name not in config["roles"] and name not in config.get("people", []):
        raise CliError(f"{option} must be a person in board.json people "
                       f"({', '.join(config.get('people', [])) or 'none configured'}) or a role "
                       f"({', '.join(config['roles'])}), got {name!r}")
    return name


def person(config: dict, name: str | None, option: str) -> str:
    """A person from board.json people: name when given, else this checkout's `tb me`; never a role."""
    people = config.get("people", [])
    name = name if name is not None else config.get("me")
    if name is None:
        raise CliError(f"{option} is needed (or set this checkout's person: tb me <name>)")
    if name not in people:
        raise CliError(f"{option} must be a person in board.json people "
                       f"({', '.join(people) or 'none configured'}), got {name!r}")
    return name


def owner_value(config: dict, raw: str | None) -> str | None:
    """The owner to store for raw: None for null/none/empty or for default_owner, else a person."""
    value = (raw or "").strip()
    if value.lower() in ("null", "none", ""):
        return None
    if value not in config.get("people", []):
        raise CliError(f"owner must be null or a person in board.json people "
                       f"({', '.join(config.get('people', [])) or 'none configured'}), got {raw!r}")
    return None if value == config.get("default_owner") else value


def parse_value(key: str, raw: str, review=None):
    """The header value for a `key=raw` pair; review is the board's review field (vcs.review_field)."""
    value = raw.strip()
    if value.lower() in ("null", "none", ""):
        return [] if key == "blocked_by" else None
    if review is not None and key == review.name:
        try:
            return review.parse(value)
        except ValueError as exc:
            raise CliError(str(exc)) from None
    if key == "blocked_by":
        items = [v.strip().upper() for v in value.strip("[]").split(",") if v.strip()]
        bad = [v for v in items if not is_id(v)]
        if bad:
            raise CliError(f"blocked_by: not task ids: {', '.join(bad)}")
        return items
    if key == "waiting_on" and is_id(value.upper()):
        return value.upper()
    return value


# ---- commands ----

def cmd_board(config: dict, args) -> int:
    owner = args.owner
    if args.mine:
        if not config.get("me"):
            raise CliError("--mine needs this checkout's person: tb me <name>")
        owner = config["me"]
    elif owner is not None and owner not in config.get("people", []):
        raise CliError(f"--owner must be a person in board.json people "
                       f"({', '.join(config.get('people', [])) or 'none configured'}), got {owner!r}")
    boards, problems = check_all(config)  # the check always covers every task
    for source, board in boards:
        if args.source and source["name"] != args.source:
            continue
        if owner is not None:
            board = filter_board(board, owner, config.get("default_owner"))
        print(format_board(board, source["name"] if len(boards) > 1 else None, review_field(config).name))
    return print_problems(config, boards, problems)


def print_problems(config: dict, boards, problems: list[str]) -> int:
    total = sum(len(b["tasks"]) for _, b in boards)
    for w in handover_warnings(config):
        print(f"\nwarning: {w}")
    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"\n{total} tasks, no problems.")
    return 0


def cmd_check(config: dict, args) -> int:
    boards, problems = check_all(config)
    return print_problems(config, boards, problems)


def owner_text(config: dict, header: dict) -> str:
    """`Robin`, `- (Alex)` for null with a default owner, `-` when unowned."""
    if "owner" not in header:
        return "missing (run: tb migrate-owner)"
    if header["owner"]:
        return str(header["owner"])
    default = config.get("default_owner")
    return f"- ({default})" if default else "-"


def cmd_show(config: dict, args) -> int:
    source, state, name = locate(config, args.id)
    text, task = read_task(source, state, name)
    h = task["header"]
    print(f"{h.get('id')}: {h.get('title')}")
    print(f"file: {rel(config, os.path.join(source['root_path'], state, name))}")
    print(f"state: {state}   priority: {h.get('priority')}   type: {h.get('type')}   "
          f"waiting_on: {h.get('waiting_on')}   blocked_by: {', '.join(h.get('blocked_by') or []) or '-'}")
    print(f"owner: {owner_text(config, h)}")
    review = review_field(config)
    print(f"plan: {h.get('plan') or '-'}   branch: {h.get('branch') or '-'}   "
          f"{review.name}: {review.label(h.get(review.name)) or '-'}")
    open_q = [q for q in task["questions"] if q["status"] == "open"]
    print("open questions: " + ("none" if not open_q else ""))
    for q in open_q:
        print(f"  {q['id']} (for {q['for']}): {q['text']}")
    log = [s for s in task["sections"].get("Log", "").split("\n") if s.startswith("- ")]
    print(f"last log lines ({len(log)} total):")
    for line in log[-args.log_lines:]:
        print(f"  {line}")
    allowed = workflow.allowed_moves(h.get("type"), state, config["type_rules"])
    print("next states: " + (", ".join(allowed) or "none (final)"))
    if args.full:
        print("\n" + text)
    return 0


def cmd_next_id(config: dict, args) -> int:
    source = source_by_name(config, args.source)
    print(next_id(source["root_path"], source["prefix"]))
    return 0


def cmd_new(config: dict, args) -> int:
    source = source_by_name(config, args.source)
    if not source["editable"]:
        raise CliError(f"The source '{source['name']}' is read-only.")
    guard(config, source)
    state = workflow.resolve_state(args.state)
    if state not in (workflow.C, workflow.R):
        raise CliError("A new task starts in 00-Conception or 01-Refinement; move it from there.")
    if args.type not in config["types"]:
        raise CliError(f"type must be one of {', '.join(config['types'])}")
    task_id = next_id(source["root_path"], source["prefix"])
    slug = args.slug or textedit.slugify(args.title)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise CliError(f"slug must be lowercase words joined by '-', got {slug!r}")
    name = f"{task_id}-{slug}.md"
    owner = owner_value(config, args.owner) if args.owner is not None else None
    text = textedit.new_task_text(task_id, args.title, args.type, args.priority, today(), state, args.by,
                                  read_text_arg(args.problem, args.problem_file), workflow.WAITING_ON[state],
                                  owner=owner, review_field=review_field(config).name)
    added = new_problems(None, text, state, name, schema_of(config), source["prefix"])
    if added:
        raise CliError("The new task would be inconsistent: " + "; ".join(added))
    path = os.path.join(source["root_path"], state, name)
    atomic_write(os.path.join(source["root_path"], state), path, text.encode("utf-8"), None)
    return report_edit(config, source, state, name, f"{task_id} created in {state}.",
                       f"{task_id} created ({textedit.clean_input(args.title, 'title', 200)})", [])


def cmd_move(config: dict, args) -> int:
    source, src, name = locate(config, args.id)
    dst = workflow.resolve_state(args.state)
    guard(config, source)
    if not source["editable"]:
        raise CliError(f"The source '{source['name']}' is read-only.")
    text, task = read_task(source, src, name)
    task_id = task["header"].get("id")
    review = review_field(config)

    fields: dict = {}
    if args.branch is not None:
        fields["branch"] = parse_value("branch", args.branch)
    for vcs_name, other in REVIEW_FIELDS.items():
        given = getattr(args, other.name, None)
        if given is None:
            continue
        if other.name != review.name:
            raise CliError(f"{other.flag} is the review field of {vcs_name} boards; this board's vcs is "
                           f"{vcs_of(config)}, use {review.flag}")
        fields[review.name] = parse_value(review.name, given, review)
    preview = parse_task(textedit.set_fields(text, fields, today(), args.by, log=False) if fields else text,
                         src, name, task["path"])
    problems = workflow.move_problems(preview, src, dst, args.reason, args.tokens, config["type_rules"], review)
    if problems and not args.force:
        print(f"{task_id}: {src} -> {dst} refused:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Fix these (or pass --force with a --reason, which is logged).", file=sys.stderr)
        return 1
    if args.force and problems and not args.reason:
        raise CliError("--force needs a --reason")

    original_text = text
    closed: list[str] = []
    if dst == workflow.DROP:
        text, closed = close_open_questions(config, text, args.reason, args.by)
        preview = parse_task(textedit.set_fields(text, fields, today(), args.by, log=False) if fields else text,
                             src, name, task["path"])
    fields["waiting_on"] = workflow.waiting_after_move(preview, dst)
    entry = f"{src} → {dst}"
    details = []
    if closed:
        details.append(f"closed {', '.join(closed)} as obsolete")
    if "branch" in fields:
        details.append(f"branch {fields['branch']}")
    if review.name in fields:
        details.append(review.detail(fields[review.name]))
    if args.reason:
        details.append(textedit.clean_input(args.reason, "reason"))
    if details:
        entry += f" ({'; '.join(details)})"
    if args.force and problems:
        entry += " [forced past: " + "; ".join(problems) + "]"
    new_text = textedit.set_fields(text, fields, today(), args.by, log=False)
    doc = textedit.Lines(new_text)
    textedit.append_to_section(doc, "Log", textedit.log_line(today(), args.by, entry + "."))
    if args.tokens:
        textedit.append_to_section(doc, "Log", textedit.log_line(
            today(), args.by, "tokens: " + textedit.clean_input(args.tokens, "tokens").rstrip(".") + "."))
    textedit.set_header(doc.lines, "updated", today())
    new_text = doc.text()
    added = new_problems(original_text, new_text, dst, name, schema_of(config), source["prefix"], old_state=src)
    if added and not args.force:  # --force overrides the move preconditions; the log line names what it passed
        raise CliError("The move would make the file inconsistent: " + "; ".join(added))

    src_path = os.path.join(source["root_path"], src, name)
    dst_path = os.path.join(source["root_path"], dst, name)
    with task_write_lock():  # no periodic update between the rewrite and the move
        atomic_write(os.path.join(source["root_path"], src), src_path, new_text.encode("utf-8"),
                     _hash_of(original_text))
        how = move_file(config["repo_root_path"], src_path, dst_path, vcs_of(config))
    after = [p for p in check_tasks(load_tasks(source["root_path"]), config["repo_root_path"], schema_of(config),
                                    source["prefix"], all_ids(config))
             if p.startswith(f"{dst}/{name}:")]
    return report_edit(config, source, dst, name, f"{task_id}: {src} → {dst} ({how}).",
                       f"{task_id} {src} → {dst}", after)


def answered_by_mover(config: dict, by: str) -> str:
    """
    The person or role that closes the questions of a dropped task: --by when it is one, else its first word
    ("supervisor (T187)" is the supervisor), else this checkout's person (`tb me`), else user. A move takes any
    --by text for the Log, with or without open questions, so it must not fail here.
    """
    valid = set(config["roles"]) | set(config.get("people", []))
    if by in valid:
        return by
    words = by.split()
    if words and words[0] in valid:
        return words[0]
    return config.get("me") or "user"


def close_open_questions(config: dict, text: str, reason: str, by: str) -> tuple[str, list[str]]:
    """Answer every open question of a task that is being dropped: "obsolete: task dropped (<reason>)", answered
    by the mover, no Decisions line. Returns (new text, the closed question ids)."""
    open_ids = [q["id"] for q in textedit.open_questions(text)]
    if not open_ids:
        return text, []
    answerer = answered_by_mover(config, by)
    answer = f"obsolete: task dropped ({textedit.clean_input(reason, 'reason')})"
    for qid in open_ids:
        text = textedit.answer_question(text, qid, answer, today(), by=answerer, actor=by, decision=False,
                                        waiting_after=None, waiting_always=True)
    return text, open_ids


def _hash_of(text: str) -> str:
    from .store import content_hash
    return content_hash(text.encode("utf-8"))


def cmd_log(config: dict, args) -> int:
    task_id = args.id.upper()
    return edit_task(config, task_id, lambda t: textedit.add_log(t, args.by, args.text, today()),
                     f"{task_id}: log line added.", f"{task_id} log")


def cmd_ask(config: dict, args) -> int:
    task_id = args.id.upper()
    question = args.text
    if args.to is not None:
        if args.to not in config.get("people", []):
            raise CliError(f"--to must be a person in board.json people "
                           f"({', '.join(config.get('people', [])) or 'none configured'}), got {args.to!r}")
        args.for_role = "user"
        question = textedit.addressed(args.to, question)
    if args.for_role not in config["roles"]:
        raise CliError(f"--for must be one of {', '.join(config['roles'])}")
    asked = {}

    def edit(text: str) -> str:
        new_text, qid = textedit.ask_question(text, args.for_role, question, today(), args.by)
        asked["id"] = qid
        return new_text
    source, state, name = locate(config, task_id)
    if state in (workflow.DONE, workflow.DROP):
        raise CliError(f"{task_id} is in {state}; reopen it first or create a new task.")
    problems = apply_edit(source, state, name, None, edit, config["main_branch"], schema_of(config),
                          config["repo_root_path"], vcs=vcs_of(config))
    return report_edit(config, source, state, name, f"{task_id}: {asked['id']} asked (for {args.for_role}).",
                       f"{task_id} {asked['id']} for {args.for_role}", problems)


def cmd_answer(config: dict, args) -> int:
    task_id = args.id.upper()
    qid = args.question.upper()
    waiting = None if args.waiting in (None, "null") else args.waiting
    if waiting is not None and waiting not in config["roles"] and not is_id(waiting):
        raise CliError(f"--waiting must be null, a role or a task id, got {waiting!r}")
    by = person_or_role(config, args.answered_by, "--answered-by")
    return edit_task(
        config, task_id,
        lambda t: textedit.answer_question(t, qid, args.text, today(), by=by, actor=args.by,
                                           decision=not args.no_decision, waiting_after=waiting,
                                           via=args.via, waiting_always=args.waiting is not None),
        f"{task_id}: {qid} answered by {by}.", f"{task_id} {qid} answered ({by})")


def cmd_decide(config: dict, args) -> int:
    task_id = args.id.upper()
    if OWNER_DECISION_TEXT.match(args.text):
        raise CliError(f"ownership is the owner header field; use: tb set {task_id} owner=<name> "
                       "[--decided-by <current owner>] (it writes the decision line)")
    by = person_or_role(config, args.decided_by, "--decided-by")
    return edit_task(config, task_id,
                     lambda t: textedit.add_decision(t, by, args.text, today(), args.by),
                     f"{task_id}: decision by {by} recorded.", f"{task_id} decision ({by})")


def read_local(path: str) -> dict:
    """The checkout's local.json as an object ({} when missing or unreadable: a new file starts)."""
    try:
        with open(path, encoding="utf-8") as f:
            local = json.load(f)
    except (OSError, ValueError):
        return {}
    return local if isinstance(local, dict) else {}


def cmd_me(config: dict, args) -> int:
    """
    Print or set the person working in this checkout, its board commits opt-in and its role (local.json next to
    board.json, never versioned). The other keys of local.json (private sources, the reload list) are kept.
    """
    path = local_path_of(config)
    people = config.get("people", [])
    if args.name is None and args.board_commits is None and args.role is None:
        try:
            with open(path, encoding="utf-8") as f:
                local = json.load(f)
            me = validate_local(local, people)
        except FileNotFoundError:
            local, me = {}, None
        except (OSError, ValueError) as exc:
            raise CliError(f"{path}: {exc}") from exc
        if me is None:
            print(f"not set (run: tb me <name>, one of {', '.join(people) or 'none configured in board.json'})")
            return 1
        print(me)
        if local.get("board_commits") is True:
            print("board commits on")
        if local.get("role") == "supervisor":
            print("role supervisor")
        return 0
    local = read_local(path)
    if args.name is not None:
        if args.name not in people:
            raise CliError(f"{args.name!r} is not in board.json people ({', '.join(people) or 'none configured'})")
        local["me"] = args.name
    elif local.get("me") not in people:
        raise CliError("set the person first: tb me <name> --board-commits on|off / --role supervisor|none")
    if args.board_commits is not None:
        local["board_commits"] = args.board_commits == "on"
    if args.role == "none":
        local.pop("role", None)
    elif args.role is not None:
        local["role"] = args.role
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(local, indent=2) + "\n")
        os.replace(temp, path)
    except OSError as exc:
        raise CliError(f"Cannot write {path}: {exc}") from exc
    commits = " (board commits on)" if local.get("board_commits") is True else ""
    role = " (role supervisor)" if local.get("role") == "supervisor" else ""
    print(f"me = {local['me']}{commits}{role} ({rel(config, path)}, not versioned)")
    return 0


def cmd_set(config: dict, args) -> int:
    task_id = args.id.upper()
    values = {}
    raw_values = {}
    keys = settable(config)
    review = review_field(config)
    for pair in args.pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if sep and key in SETTABLE and key not in keys:
            raise CliError(f"{key}= is the review field of another version control; this board's vcs is "
                           f"{vcs_of(config)}, its review field is {review.name}=")
        if not sep or key not in keys:
            raise CliError(f"expected key=value with key one of {', '.join(keys)}, got {pair!r}")
        raw_values[key] = raw
        values[key] = None if key == "owner" else parse_value(key, raw, review)
    if "owner" in values:
        if len(values) > 1:
            raise CliError("owner= can't be combined with other keys (a transfer is a decision of its own)")
        return set_owner(config, task_id, owner_value(config, raw_values["owner"]), args)
    if args.decided_by is not None or args.force or args.reason is not None:
        raise CliError("--decided-by, --force and --reason only apply to owner=")
    if "plan" in values and values["plan"] and not os.path.isfile(
            os.path.join(config["repo_root_path"], values["plan"])):
        raise CliError(f"plan {values['plan']} does not exist (path relative to the repo root)")
    known = all_ids(config)
    for blocker in values.get("blocked_by") or []:
        if blocker not in known:
            raise CliError(f"blocked_by: unknown task {blocker}")
    return edit_task(config, task_id, lambda t: textedit.set_fields(t, values, today(), args.by),
                     f"{task_id}: set {', '.join(values)}.", f"{task_id} set {', '.join(values)}")


def set_owner(config: dict, task_id: str, owner: str | None, args) -> int:
    """
    `tb set <id> owner=<name>`: header, Decisions and Log in one write. With strict ownership (board.json
    `ownership`) a transfer by the current owner (--decided-by, default `tb me`); --force --reason lets someone
    else transfer it (e.g. for a dev who left). With open ownership anyone sets the owner (--decided-by, default
    `tb me`), the decision line says who did (`set by <who>, from <old>`), and --force is not needed.
    """
    default = config.get("default_owner")
    strict = strict_ownership(config)
    if args.force and not args.reason:
        raise CliError("--force needs a --reason")
    if not strict and (args.force or args.reason is not None):
        raise CliError("--force and --reason are for strict ownership; with ownership \"open\" anyone sets the "
                       "owner (tb set <id> owner=<name> [--decided-by <who>])")
    source, state, name = locate(config, task_id)
    _, current = read_task(source, state, name)
    if "owner" not in current["header"]:
        raise CliError(f"{task_id} has no owner field yet; run: tb migrate-owner")
    new = owner or default
    if effective_owner(current["header"], default) == new:
        print(f"{task_id}: owner is already {new or 'null'}; nothing changed.")
        return 0
    giver = person(config, args.decided_by, "--decided-by")
    outcome = {}

    def edit(text: str) -> str:
        old = effective_owner(parse_task(text, state, name, "")["header"], default)
        forced = None
        if strict and old is not None and giver != old:
            if not args.force:
                raise CliError(f"{task_id} is owned by {old}; only {old} can transfer it (--decided-by {old}), "
                               "or --force --reason \"...\"")
            forced = args.reason
        outcome["old"] = old
        return textedit.set_owner(text, owner, giver, today(), args.by, default, forced=forced,
                                  transfer=strict)
    problems = apply_edit(source, state, name, None, edit, config["main_branch"], schema_of(config),
                          config["repo_root_path"], vcs=vcs_of(config))
    return report_edit(config, source, state, name,
                       f"{task_id}: owner {outcome.get('old') or 'null'} → {new or 'null'} (decided by {giver}).",
                       f"{task_id} owner {new or 'null'}", problems)


def cmd_migrate_owner(config: dict, args) -> int:
    """
    One-off format conversion: give every task file without an `owner` header line one (see
    textedit.migrate_owner). Idempotent; --dry-run writes nothing. No Log line, no `updated` change.
    """
    default = config.get("default_owner")
    people = config.get("people", [])
    changed = kept = 0
    notes: list[str] = []
    for source in config["sources"]:
        if not source["editable"]:
            continue
        if not args.dry_run:
            guard(config, source)
        with task_write_lock():
            for state in STATES:
                folder = os.path.join(source["root_path"], state)
                if not os.path.isdir(folder):
                    continue
                for file_name in sorted(os.listdir(folder)):
                    path = os.path.join(folder, file_name)
                    if not FILE_PATTERN.match(file_name) or not os.path.isfile(path):
                        continue
                    shown = rel(config, path)
                    with open(path, "rb") as f:
                        data = f.read()
                    try:
                        text = data.decode("utf-8")
                        new_text, value = textedit.migrate_owner(text, default)
                    except (UnicodeDecodeError, EditError) as exc:
                        notes.append(f"{shown}: skipped ({exc})")
                        continue
                    if value is not None and value not in people:
                        fix = " --force --reason \"...\"" if strict_ownership(config) else ""
                        notes.append(f"{shown}: owner {value} is not in board.json people "
                                     f"(fix: tb set <id> owner=<name>{fix})")
                    if new_text == text:
                        kept += 1
                        task = parse_task(text, state, file_name, shown)
                        found, decided = latest_owner_decision(task["sections"].get("Decisions", ""))
                        effective = effective_owner(task["header"], default)
                        if found and decided != effective:
                            notes.append(f"{shown}: Decisions name owner {decided or 'null'}, header owner is "
                                         f"{effective or 'null'} (left as is)")
                        continue
                    changed += 1
                    print(f"{'would set' if args.dry_run else 'set'} owner: {value or 'null'}  {shown}")
                    if not args.dry_run:
                        atomic_write(folder, path, new_text.encode("utf-8"), content_hash(data))
    for note in notes:
        print(f"  note: {note}", file=sys.stderr)
    verb = "would get" if args.dry_run else "got"
    print(f"{changed} file(s) {verb} an owner line, {kept} already had one"
          + (" (dry run, nothing written)." if args.dry_run else "."))
    if changed and not args.dry_run:
        print("commit: tasks: owner header migration")
    return 0


def cmd_section(config: dict, args) -> int:
    task_id = args.id.upper()
    name = args.name.capitalize()
    if name not in EDITABLE_SECTIONS:
        raise CliError(f"only {', '.join(EDITABLE_SECTIONS)} can be rewritten; Questions, Decisions and Log "
                       "change through ask/answer/decide/log")
    body = read_text_arg(args.text, args.file)
    if re.search(r"^## ", body, re.MULTILINE):
        raise CliError("the text must not contain '## ' headings (use ### inside a section)")
    return edit_task(config, task_id, lambda t: textedit.set_section(t, name, body, today(), args.by),
                     f"{task_id}: {name} rewritten.", f"{task_id} {name}")


def cmd_next_plan(config: dict, args) -> int:
    plans = os.path.join(config["repo_root_path"], config["plans_dir"])
    stamp = date.today().strftime("%y%m")
    highest = 0
    if os.path.isdir(plans):
        for entry in os.listdir(plans):
            m = re.match(rf"^{stamp}-(\d+)-", entry)
            if m:
                highest = max(highest, int(m.group(1)))
    title = "-".join(w.capitalize() for w in re.findall(r"[A-Za-z0-9]+", args.title)) or "Plan"
    print(rel(config, os.path.join(plans, f"{stamp}-{highest + 1:02d}-{title}.md")))
    return 0


def port_problem(host: str, port: int) -> str | None:
    """Why the board can't listen on host:port (e.g. another board runs there), or None."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError as exc:
            return (f"{host}:{port} is not free ({exc}). Another task board (e.g. a coordinator's or the "
                    f"supervisor's on this machine) or another program is probably listening there. Stop it "
                    f"({pid_hint(port)}), or use another port: tb serve --port N (tb watch --port N), or set "
                    f"\"port\" in board.json.")
    return None


def pid_hint(port: int) -> str:
    """How to find the process listening on port (to stop it by its pid, never by image name)."""
    if os.name == "nt":
        return f"its pid: netstat -ano | findstr :{port}"
    return f"its pid: lsof -i :{port}"


QUIET_PATHS = ("/api/fingerprint", "/api/update", "/static/")


def quiet_handler_class():
    """The request handler of the board: werkzeug's, minus the log line of successful routine polls."""
    from werkzeug.serving import WSGIRequestHandler

    class QuietHandler(WSGIRequestHandler):
        def log_request(self, code="-", size="-"):
            path = (self.path or "").split("?", 1)[0]
            if str(code)[:1] == "2" or str(code) == "304":
                if any(path == q or (q.endswith("/") and path.startswith(q)) for q in QUIET_PATHS):
                    return
            super().log_request(code, size)

    return QuietHandler


def run_board(app, host: str, port: int, activity=None, idle_seconds: float = 0.0) -> None:
    """Serve app until it is stopped (Ctrl+C, or the idle exit when idle_seconds > 0)."""
    from werkzeug.serving import make_server

    from .idle import start_idle_thread
    from .store import _WRITE_LOCK
    try:
        server = make_server(host, port, app, threaded=True, request_handler=quiet_handler_class())
    except SystemExit as exc:  # werkzeug reports a failed bind on stderr and exits
        raise OSError(f"could not listen ({exc})") from exc
    if idle_seconds:
        start_idle_thread(activity, idle_seconds, _WRITE_LOCK, server.shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def cmd_serve(config: dict, args) -> int:
    try:
        from .web.app import create_app
    except ImportError as exc:
        print(f"The web board needs Flask (pip install -r .claude/board/requirements.txt): {exc}",
              file=sys.stderr)
        return 1
    from .idle import Activity, describe, idle_limit_seconds
    from .store import _WRITE_LOCK
    from .update import Updater, serve_updates
    interval = 0 if args.no_update else config["watch"]["update_seconds"]
    updater = Updater(config, interval) if interval else None
    activity = Activity()
    idle_seconds = idle_limit_seconds(args.idle_exit or 0)
    commits = bool(config.get("board_commits")) and not args.no_commit
    app = create_app(config, updater=updater, activity=activity, commits=commits)
    vcs = backend(config)
    print(f"Task board on http://{config['host']}:{config['port']}/ "
          f"({', '.join(s['name'] for s in config['sources'])})"
          + (f"; {vcs.update_description(config, interval)}" if updater
             else f"; no automatic {vcs.name} update")
          + (f"; stops after {describe(idle_seconds)} without a request" if idle_seconds else "")
          + ("; commits board answers and notes" if commits else ""), flush=True)
    # Start (and update) only once it is clear the board can run here (the port is free).
    problem = port_problem(config["host"], config["port"])
    if problem:
        print(f"Cannot start the server: {problem}", file=sys.stderr)
        return 1
    if updater:
        threading.Thread(target=serve_updates, args=(updater, _WRITE_LOCK), name="periodic-update",
                         daemon=True).start()
    try:
        run_board(app, config["host"], config["port"], activity, idle_seconds)
    except OSError as exc:
        print(f"Cannot start the server on {config['host']}:{config['port']}: {exc}. Stop the other program "
              f"({pid_hint(config['port'])}) or use another port: tb serve --port N, or set \"port\" in "
              f"board.json.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def done_paths(config: dict) -> list[str]:
    """The repo-relative paths `tb done` commits: the update paths, plus the handover file when it lies outside
    them."""
    paths = list(config["watch"]["update_paths"])
    handover = config.get("handover")
    if handover:
        path = handover["path"].replace("\\", "/").strip("/")
        if not any(path == p or path.startswith(p + "/") for p in paths):
            paths.append(path)
    return paths


def done_message(message: str) -> str:
    """The commit message with the `tasks: ` prefix (kept when already there)."""
    text = textedit.clean_input(message, "message", 500)
    return text if re.match(r"^tasks:\s", text) else f"tasks: {text}"


def cmd_done(config: dict, args) -> int:
    """
    The supervisor's end of a wake: the check (any problem: printed, exit 1, nothing committed), then one commit
    of the task files (done_paths) through the backend, pushed with git unless --no-push. Refused outside a
    checkout of the main line like every write.
    """
    for source in config["sources"]:
        if source["editable"]:
            guard(config, source)
    message = done_message(args.message)
    boards, problems = check_all(config)
    for w in handover_warnings(config):
        print(f"warning: {w}")
    if problems:
        print(f"{len(problems)} problem(s), nothing committed:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    vcs = backend(config)
    if not vcs.available():
        raise CliError(f"{vcs.name} is not on PATH; commit the task files by hand")
    paths = done_paths(config)
    with task_write_lock():  # no periodic update between the add and the commit
        revision, note, error = vcs.commit_paths(config, paths, message, push=not args.no_push)
    if revision is None and not error:
        print(f"nothing to commit under {', '.join(paths)}")
        return 0
    if revision is None:
        raise CliError(error)
    shown = vcs.format_revision(revision)
    if error:
        print(f"committed {shown} ({message}); {error}", file=sys.stderr)
        return 1
    print(f"committed {shown} ({message})" + (f"; {note}" if note else ""))
    return 0


def cmd_watch(config: dict, args) -> int:
    from .session import SessionGuard, ancestors
    from .watch import watch
    person = args.for_person
    if person is not None and person not in config.get("people", []):
        print(f"error: --for {person!r} is not in board.json people "
              f"({', '.join(config.get('people', [])) or 'none configured'})", file=sys.stderr)
        return 2
    update_seconds = None
    if args.update_seconds is not None:
        update_seconds = check_update_seconds(args.update_seconds, "--update-seconds")
    session_guard = None
    if not args.once and not args.no_parent_check:
        chain = ancestors()
        if chain:
            session_guard = SessionGuard(chain)
        else:
            print("note: the parent processes could not be determined; the watcher won't stop with its session",
                  flush=True)
    return watch(config, start_server=not args.no_server, quiet_seconds=args.quiet, once=args.once,
                 update=not args.no_update, update_seconds=update_seconds, guard=session_guard, person=person)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tb", description="Task board: list, check and change task files (AI/Tasks/README.md).")
    parser.add_argument("--config", help="board config (default: .claude/board/board.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    def by(p, default="supervisor"):
        p.add_argument("--by", default=default, help=f"who makes the change, for the Log line (default {default})")

    p = sub.add_parser("board", help="list the tasks per state and run the check")
    p.add_argument("--source")
    owned = p.add_mutually_exclusive_group()
    owned.add_argument("--owner", help="only the tasks this person owns (null tasks count for default_owner)")
    owned.add_argument("--mine", action="store_true", help="only the tasks of this checkout's person (tb me)")
    p.set_defaults(func=cmd_board)
    p = sub.add_parser("check", help="consistency check only (exit 1 on problems)")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("show", help="one task: state, header, open questions, last log lines, next states")
    p.add_argument("id")
    p.add_argument("--log-lines", type=int, default=5)
    p.add_argument("--full", action="store_true", help="also print the whole file")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("next-id", help="the id the next new task gets")
    p.add_argument("--source")
    p.set_defaults(func=cmd_next_id)
    p = sub.add_parser("next-plan", help="the path for a new plan (<plans_dir>/<YYMM>-<NN>-<Title>.md)")
    p.add_argument("title")
    p.set_defaults(func=cmd_next_plan)

    p = sub.add_parser("new", help="create a task with the next id")
    p.add_argument("--title", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--priority", required=True, choices=["high", "medium", "low"])
    p.add_argument("--problem", help="text of the Problem section")
    p.add_argument("--problem-file", help="read the Problem section from a file ('-' = stdin)")
    p.add_argument("--state", default="00-Conception", help="00-Conception (default) or 01-Refinement")
    p.add_argument("--slug", help="file name slug (default: from the title)")
    p.add_argument("--source", help="source name (default: the first source)")
    p.add_argument("--owner", help="a person from board.json people (default: null = default_owner)")
    by(p)
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("move", help="move a task to another state (checks the flow, logs, moves the file with "
                                    "the version control)")
    p.add_argument("id")
    p.add_argument("state", help="target state: 02-Ready, 02, 2 or ready")
    p.add_argument("--reason", help="why; required for backward moves, 08-Dropped and landing a branch")
    p.add_argument("--branch", help="set branch (needed for 03-Implementation)")
    p.add_argument("--pr", help="git boards: set the pull request number handed to review (needed for "
                                "04-CodeReview)")
    p.add_argument("--rev", help="svn boards: set the branch revision handed to review (needed for 04-CodeReview)")
    p.add_argument("--tokens", help="agent token counts, logged at 07-Done")
    p.add_argument("--force", action="store_true", help="move despite failed preconditions (logged)")
    by(p)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("log", help="append a Log line")
    p.add_argument("id")
    p.add_argument("text")
    by(p)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("ask", help="add an open question (next Q number); for the user sets waiting_on user")
    p.add_argument("id")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--for", dest="for_role", help="the role that must answer: user, supervisor, ...")
    who.add_argument("--to", help="a person (board.json people): same as --for user with the text "
                                  "prefixed '@<name>: '")
    p.add_argument("text")
    by(p)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("answer", help="record the answer to an open question (+ Decisions line)")
    p.add_argument("id")
    p.add_argument("question", help="Q1, Q2, ...")
    p.add_argument("text")
    p.add_argument("--answered-by", help="who answered: a person or a role (required with strict ownership; "
                                         "default tb me, else user, with open ownership); anyone may answer "
                                         "any open question")
    p.add_argument("--via", help="where the answer came from, e.g. chat")
    p.add_argument("--no-decision", action="store_true", help="don't add a Decisions line")
    p.add_argument("--waiting", help="waiting_on afterwards when no user question is left (default: null if it "
                                     "was user, else unchanged)")
    by(p)
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("decide", help="record a decision")
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--decided-by", help="who decided: a person or a role (required with strict ownership; "
                                        "default tb me, else user, with open ownership)")
    by(p)
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("set", help="set header fields: key=value ... (" + ", ".join(BASE_SETTABLE)
                                   + ", pr on git boards, rev on svn boards); owner= alone sets the owner and "
                                   "records the decision")
    p.add_argument("id")
    p.add_argument("pairs", nargs="+")
    p.add_argument("--decided-by", help="owner= only: who gives the task away (default: tb me); the current "
                                        "owner with strict ownership, anyone with open ownership")
    p.add_argument("--force", action="store_true", help="owner= only, strict ownership: transfer although "
                                                        "--decided-by is not the current owner (needs --reason, "
                                                        "recorded)")
    p.add_argument("--reason", help="owner= only: why --force")
    by(p)
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("migrate-owner", help="add the owner header line to task files without one (from the "
                                             "latest owner decision, else null); idempotent")
    p.add_argument("--dry-run", action="store_true", help="only print what would change")
    p.set_defaults(func=cmd_migrate_owner)

    p = sub.add_parser("section", help="rewrite Problem, Approach or Acceptance")
    p.add_argument("id")
    p.add_argument("name")
    p.add_argument("--text")
    p.add_argument("--file", help="read the text from a file ('-' = stdin)")
    by(p)
    p.set_defaults(func=cmd_section)

    p = sub.add_parser("me", help="print or set who works in this checkout (local.json, not versioned)")
    p.add_argument("name", nargs="?", help="a person from board.json people")
    p.add_argument("--board-commits", choices=["on", "off"],
                   help="the web board commits its own answers and notes (coordinator checkouts only, never the "
                        "supervisor's)")
    p.add_argument("--role", choices=["supervisor", "none"],
                   help="supervisor: the reload hook injects the supervisor role into every Claude Code session "
                        "of this checkout, also after /clear (the supervisor's checkout only); none removes it")
    p.set_defaults(func=cmd_me)

    p = sub.add_parser("serve", help="run the local web board (and update the checkout periodically)")
    p.add_argument("--no-update", action="store_true", help="no periodic update (tb watch starts its board so)")
    p.add_argument("--port", type=int, help="listen on this port instead of board.json port")
    p.add_argument("--idle-exit", type=float, metavar="MIN", help="stop after MIN minutes without any request "
                                                                  "(tb watch starts its board so; default: never)")
    p.add_argument("--no-commit", action="store_true", help="never commit board answers and notes, whatever "
                                                            "local.json board_commits says (the supervisor's "
                                                            "watcher starts its board so)")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("done", help="the supervisor's end of a wake: run the check (problems: exit 1, nothing "
                                    "committed), then commit the task files (watch.update_paths and the handover "
                                    "file) with the message prefixed 'tasks: ' and push (git)")
    p.add_argument("message", help="the commit message ('tasks: ' is prefixed unless already there)")
    p.add_argument("--no-push", action="store_true", help="git: commit without pushing")
    p.set_defaults(func=cmd_done)
    p = sub.add_parser("watch", help="wait for input for the supervisor (web board, other devs' commits), print "
                                     "it and exit (3: update conflict or repeated failure); with --for <dev>, "
                                     "for a coordinator")
    p.add_argument("--for", dest="for_person", metavar="DEV",
                   help="the coordinator's watcher: wake only for input that concerns this person (board.json "
                        "people); its board commits per local.json")
    p.add_argument("--no-update", action="store_true", help="no periodic update of the checkout")
    p.add_argument("--update-seconds", type=float, help="seconds between updates (default "
                                                        "watch.update_seconds; 0 = off, else at least 10)")
    p.add_argument("--no-server", action="store_true", help="don't start the web board")
    p.add_argument("--port", type=int, help="the board's port instead of board.json port (passed to tb serve)")
    p.add_argument("--quiet", type=float, help="seconds without further input before reporting")
    p.add_argument("--once", action="store_true", help="report pending input once and exit (no waiting)")
    p.add_argument("--no-parent-check", action="store_true", help="keep running when the session (the parent "
                                                                  "processes) that started the watcher ends")
    p.set_defaults(func=cmd_watch)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        if "(board)" in (getattr(args, "by", None) or ""):
            raise CliError("--by must not contain '(board)': that marks input through the web board, "
                           "which wakes the watcher")
        config = load_board_config(args.config, with_local=args.func is not cmd_me)
        if getattr(args, "port", None) is not None:
            if not 1 <= args.port <= 65535:
                raise CliError(f"--port must be a number from 1 to 65535, got {args.port}")
            config["port"] = args.port
            config["port_override"] = True
        if getattr(args, "idle_exit", None) is not None:
            check_idle_minutes(args.idle_exit, "--idle-exit")
        return args.func(config, args)
    except (BoardConfigError, CliError, EditError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, BoardConfigError) else 1
