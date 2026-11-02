"""
Flask app of the task board: the board, the task detail view, the read-only plan view and the two write routes
(answer, note). With board commits on (commit.py) a write is committed right away; every accepted request is
noted for the idle exit (idle.py); `/api/fingerprint` carries the board's identity (config.board_id).
"""

import hashlib
import hmac
import os
import re
import secrets
from datetime import date

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

from ..commit import write_and_commit
from ..config import board_id, is_loopback, schema_of
from ..idle import Activity
from ..model import (FILE_PATTERN, STATES, check_tasks, effective_owner, load_tasks, parse_task, task_ids,
                     waits_on_person)
from ..store import apply_edit, content_hash, find_task_files, main_branch_block, task_file_path
from ..textedit import MAX_INPUT_LENGTH, EditError, ForbiddenError, add_note, answer_question
from ..update import banner
from ..vcs import backend
from .options import parse_options, question_texts
from .render import link_paths, render_document, render_markdown

HERE = os.path.dirname(os.path.abspath(__file__))
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
# The executor and the reviewer work from the task file while it is in these states; user input waits.
LOCKED_STATES = {"03-Implementation", "04-CodeReview"}
# A plan file name in plans_dir (no folders, no dot files).
PLAN_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md")


def fingerprint(sources: list[dict]) -> str:
    """A hash over name, size and modification time of every file in the state folders of all sources."""
    digest = hashlib.sha256()
    for source in sources:
        for state in STATES:
            folder = os.path.join(source["root_path"], state)
            try:
                entries = sorted(os.scandir(folder), key=lambda e: e.name)
            except OSError:
                digest.update(f"{source['name']}/{state}: missing\n".encode())
                continue
            for entry in entries:
                try:
                    st = entry.stat()
                except OSError:
                    continue
                digest.update(f"{source['name']}/{state}/{entry.name}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()


MAX_REQUEST_BYTES = 64 * 1024


def split_host(host_header: str) -> tuple[str, int | None]:
    """(host name, port or None) of a Host header; IPv6 addresses come in brackets."""
    if host_header.startswith("["):
        name, _, rest = host_header[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif host_header.count(":") == 1:
        name, port = host_header.split(":")
    else:
        name, port = host_header, ""
    return name, int(port) if port.isdigit() else None


def _last_log_line(task: dict) -> str:
    lines = [s.strip() for s in task["sections"].get("Log", "").split("\n") if s.strip().startswith("- ")]
    return lines[-1][2:] if lines else ""


def search_text(task: dict) -> str:
    """What the board's keyword search matches a card against: id, file name, title, owner and the text of every
    section, lowercased, each whitespace-separated word once (first occurrence order). A query word without
    whitespace is a substring of the full text exactly when it is a substring of one of these words."""
    header = task["header"]
    parts = [str(header.get("id") or ""), task["file"], str(header.get("title") or ""),
             str(header.get("owner") or "")]
    parts += [f"{name} {text}" for name, text in task["sections"].items()]
    return " ".join(dict.fromkeys(" ".join(parts).lower().split()))


def _card(source: dict, task: dict) -> dict:
    header = task["header"]
    blocked = header.get("blocked_by")
    return {
        "source": source["name"],
        "state": task["state"],
        "file": task["file"],
        "id": str(header.get("id") or task["file"]),
        "title": str(header.get("title") or ""),
        "type": header.get("type"),
        "priority": header.get("priority"),
        "owner": header.get("owner") if isinstance(header.get("owner"), str) else None,
        "waiting_on": header.get("waiting_on"),
        "blocked_by": blocked if isinstance(blocked, list) else ([str(blocked)] if blocked else []),
        "updated": header.get("updated"),
        "last_log": _last_log_line(task),
        "broken": bool(task["errors"]) or not header,
        "search": search_text(task),
    }


def _load_sources(config: dict) -> dict[str, tuple[dict, list[str]]]:
    """Every source's load_tasks() result and its check problems (ids of all sources count as known)."""
    loaded: dict[str, dict] = {}
    failed: dict[str, str] = {}
    for source in config["sources"]:
        try:
            loaded[source["name"]] = load_tasks(source["root_path"])
        except OSError as exc:
            failed[source["name"]] = f"cannot read the folder: {exc}"
    known = set().union(*(task_ids(b) for b in loaded.values())) if loaded else set()
    result = {}
    for source in config["sources"]:
        name = source["name"]
        if name in failed:
            result[name] = ({"states": list(STATES), "tasks": [], "errors": []}, [failed[name]])
        else:
            result[name] = (loaded[name], check_tasks(loaded[name], config["repo_root_path"], schema_of(config),
                                                      source["prefix"], known))
    return result


def _open_questions(task: dict) -> list[dict]:
    """
    Every open question ({"id", "for", "text", "options"}); anyone may answer any of them. Questions for the user
    first. text is the question as far as its options start (all of it without options), options the parsed
    (a)/(b)/(c) options (Option tuples; empty for a question answered in free text).
    """
    full = question_texts(task["sections"].get("Questions", ""))
    questions = []
    for q in task["questions"]:
        if q["status"] == "open":
            text, options = parse_options(full.get(q["id"], q["text"]))
            questions.append({"id": q["id"], "for": q["for"], "text": text, "options": options})
    return sorted(questions, key=lambda q: q["for"] != "user")


def option_answer(task: dict, question_id: str, letter: str, comment: str) -> str:
    """The answer text for the option `letter` of an open question: `<letter>: <option text>`, then
    `; <comment>` when a comment was given. Raises EditError for a question without such an option."""
    question = next((q for q in _open_questions(task) if q["id"] == question_id), None)
    if question is None:
        raise EditError(f"There is no open question {question_id}.")
    option = next((o for o in question["options"] if o.letter == letter), None)
    if option is None:
        raise EditError(f"{question_id} has no option '{letter}'.")
    comment = " ".join(comment.split())
    return f"{option.letter}: {option.text}" + (f"; {comment}" if comment else "")


def _edit_block_reason(source: dict, state: str, main_branch: str, vcs: str = "git") -> str | None:
    if not source["editable"]:
        return "This source is read-only."
    if state in LOCKED_STATES:
        return f"Tasks in {state} are being worked on and can't be edited here."
    return main_branch_block(source["root_path"], main_branch, vcs)


def create_app(config: dict, updater=None, activity: Activity | None = None, commits: bool = False) -> Flask:
    """
    The board app for a validated config (see config.validate_board_config). updater: the update.Updater of the
    periodic update (`tb serve`), shown in the header and at /api/update; None when the board doesn't update.
    activity: notes the time of every accepted request (the idle exit of `tb serve --idle-exit`).
    commits: the board commits its own answers and notes (commit.py; coordinator checkouts only); off without
    the version control on PATH.
    """
    vcs = backend(config)
    commits_text = ""
    if commits:
        if vcs.available():
            commits_text = "Board commits on"
        else:
            commits, commits_text = False, f"Board commits off: {vcs.name} is not on PATH"
    app = Flask(__name__, template_folder=os.path.join(HERE, "templates"),
                static_folder=os.path.join(HERE, "static"))
    sources = {s["name"]: s for s in config["sources"]}
    token = secrets.token_urlsafe(32)
    refresh_ms = int(config["refresh_seconds"] * 1000)

    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    app.jinja_env.globals["board_title"] = config["title"]
    people = list(config.get("people", []))
    default_owner = config.get("default_owner")
    plans_path = os.path.normpath(os.path.join(config["repo_root_path"], config["plans_dir"]))

    def same_path(a: str, b: str) -> bool:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))

    def plan_url(path: str) -> str | None:
        """The plan view's URL of an existing plan file at an absolute path, else None."""
        name = os.path.basename(path)
        if not same_path(os.path.dirname(path), plans_path) or not PLAN_FILE.fullmatch(name) \
                or not os.path.isfile(os.path.join(plans_path, name)):
            return None
        return url_for("plan", file_name=name)

    def repo_plan_url(path: str) -> str | None:
        """For a repo-relative path as tasks write it (`plan: AI/Plans/....md`)."""
        return plan_url(os.path.join(config["repo_root_path"], path))

    def plan_link_url(href: str) -> str | None:
        """For a relative link in a plan: another plan, or a task file in a source (by id, wherever it is now)."""
        target = os.path.normpath(os.path.join(plans_path, href.split("#")[0].split("?")[0]))
        url = plan_url(target)
        if url:
            return url
        match = FILE_PATTERN.match(os.path.basename(target))
        for source in config["sources"]:
            if match and same_path(os.path.dirname(os.path.dirname(target)), source["root_path"]):
                return url_for("locate", source_name=source["name"], task_id=match.group(1))
        return None

    @app.template_filter("plan_links")
    def plan_links(text: str) -> Markup:
        """Plain text (a question) with its plan paths as links."""
        return Markup(link_paths(str(escape(text)), repo_plan_url))

    def current_person() -> str:
        """Who every write of this board is made as: this checkout's `tb me`, else nobody ("")."""
        return config.get("me") or ""

    @app.context_processor
    def person_context():
        return {"people": people, "me": current_person() if people else ""}

    def update_info() -> dict:
        state = dict(updater.state) if updater is not None else {"status": "off"}
        text, level = banner(state)
        return {**state, "text": text, "level": level}

    @app.context_processor
    def update_context():
        info = update_info()
        return {"update_text": info["text"], "update_level": info["level"], "updating": updater is not None,
                "commits_text": commits_text}

    @app.before_request
    def check_host():
        name, port = split_host(request.host)
        if not is_loopback(name) or (port or 80) != config["port"]:
            abort(403, "Requests must address the board by a loopback host name and its port.")
        if activity is not None:
            activity.touch()

    def render_task(source_name: str, state: str, file_name: str, message: str | None = None,
                    error: str | None = None, keep: dict | None = None, status: int = 200):
        source = sources.get(source_name)
        if source is None:
            abort(404)
        try:
            path = task_file_path(source["root_path"], state, file_name)
        except EditError:
            abort(404)
        with open(path, "rb") as f:
            data = f.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            text = None
            error = error or f"The file is not UTF-8: {exc}"
        rel = f"{state}/{file_name}"
        task = parse_task(text, state, file_name, rel, read_error=error)
        _, source_problems = _load_sources(config)[source_name]
        body = ""
        if text is not None:
            normalized = text.replace("\r\n", "\n")
            end = normalized.find("\n---", 3)
            body = normalized[end + 4:] if normalized.startswith("---") and end >= 0 else normalized
        return render_template(
            "task.html", source=source, state=state, file=file_name, task=task, header=task["header"],
            body=render_markdown(body, repo_plan_url), questions=_open_questions(task) if text is not None else [],
            problems=[p for p in source_problems if p.startswith(rel + ":")],
            block_reason=(_edit_block_reason(source, state, config["main_branch"], vcs.name) if text is not None
                          else "The file can't be read."),
            file_hash=content_hash(data), token=token, message=message, error=error, keep=keep or {},
            max_length=MAX_INPUT_LENGTH, refresh_ms=refresh_ms, fingerprint=fingerprint(config["sources"]),
            task_id=FILE_PATTERN.match(file_name).group(1), owner=_owner_label(task["header"]),
            plan_url=repo_plan_url(task["header"]["plan"]) if isinstance(task["header"].get("plan"), str) else None,
            mode="task"), status

    def _owner_label(header: dict) -> str:
        """`owner Robin`, `owner Alex (default)` for null with a default owner, else ""."""
        if "owner" not in header:
            return ""
        if isinstance(header["owner"], str) and header["owner"]:
            return f"owner {header['owner']}"
        return f"owner {default_owner} (default)" if default_owner else ""

    def waits_on_me(task: dict, me: str) -> bool:
        """"Waiting on me": with this checkout's person, an open user question for them (addressed `@<me>:`, or
        unaddressed on a task they own); without one (or without people) any open user question (waiting_on)."""
        if me:
            return waits_on_person(task, me, default_owner)
        return task["header"].get("waiting_on") == "user"

    @app.get("/")
    def board():
        selected_source = request.args.get("source", "")
        selected_priority = request.args.get("priority", "")
        selected_owner = request.args.get("owner", "")
        if selected_owner not in people:
            selected_owner = ""
        # "me" (`user` is the old spelling of the checkbox) or "everyone"; anything else is no filter.
        selected_waiting = {"me": "me", "user": "me", "everyone": "everyone"}.get(request.args.get("waiting", ""), "")
        me = current_person() if people else ""
        columns = {state: [] for state in STATES}
        problems: list[str] = []
        everything = _load_sources(config)
        for source in config["sources"]:
            loaded, source_problems = everything[source["name"]]
            problems += [f"{source['name']}: {p}" for p in source_problems]
            if selected_source and source["name"] != selected_source:
                continue
            for task in loaded["tasks"]:
                card = _card(source, task)
                card["waiting_me"] = waits_on_me(task, me)
                if selected_priority and card["priority"] != selected_priority:
                    continue
                if selected_owner and effective_owner(task["header"], default_owner) != selected_owner:
                    continue
                if selected_waiting == "me" and not card["waiting_me"]:
                    continue
                if selected_waiting == "everyone" and task["header"].get("waiting_on") != "user":
                    continue
                columns[task["state"]].append(card)
        for cards in columns.values():
            cards.sort(key=lambda c: (PRIORITY_ORDER.get(str(c["priority"]), 3), c["id"]))
        return render_template(
            "board.html", columns=columns, problems=problems, sources=config["sources"],
            selected_source=selected_source, selected_priority=selected_priority, selected_owner=selected_owner,
            selected_waiting=selected_waiting, refresh_ms=refresh_ms, fingerprint=fingerprint(config["sources"]),
            mode="board")

    @app.get("/task/<source_name>/<task_id>")
    def locate(source_name: str, task_id: str):
        """Redirect to a task's page wherever its file is now (it moves between state folders)."""
        source = sources.get(source_name)
        if source is None:
            abort(404)
        found = find_task_files(source["root_path"], task_id)
        if not found:
            abort(404, f"No task file for {task_id}.")
        if len(found) > 1:
            abort(409, f"More than one task file for {task_id}: {', '.join(f'{s}/{n}' for s, n in found)}.")
        state, file_name = found[0]
        return redirect(url_for("task", source_name=source_name, state=state, file_name=file_name))

    @app.get("/task/<source_name>/<state>/<file_name>")
    def task(source_name: str, state: str, file_name: str):
        saved = request.args.get("saved")
        message = {"answer": "Answer saved.", "note": "Note saved."}.get(saved or "")
        rev = request.args.get("rev", "")
        if message and rev == "committed":
            message = message[:-1] + " and committed."
        elif message and re.fullmatch(vcs.commit_revision_pattern(), rev):
            message = message[:-1] + f" and {vcs.commit_note(rev)}."
        return render_task(source_name, state, file_name, message=message)

    def post_edit(source_name: str, state: str, file_name: str, kind: str):
        source = sources.get(source_name)
        if source is None:
            abort(404)
        # An answer by option button sends `option` (the letter) and an optional `comment`; one in free text
        # sends `answer`.
        option = request.form.get("option", "") if kind == "answer" else ""
        field = "comment" if option else ("answer" if kind == "answer" else "note")
        keep = {"kind": kind, "question": request.form.get("question", ""), "text": request.form.get(field, "")}
        if not hmac.compare_digest(request.form.get("token", ""), token):
            return render_task(source_name, state, file_name, keep=keep, status=403,
                               error="The form is outdated (the board was restarted). Submit again.")
        by = current_person() if people else "user"
        if not by:
            return render_task(source_name, state, file_name, keep=keep, status=400,
                               error="This checkout has no person set, so the board can't write. Run "
                                     "`python .claude/board/tb.py me <name>` and reload.")
        actor = f"{by} (board)"
        today = date.today().isoformat()
        if kind == "answer":
            def edit(text: str) -> str:
                answer_text = keep["text"]
                if option:
                    answer_text = option_answer(parse_task(text, state, file_name, f"{state}/{file_name}"),
                                                keep["question"], option, keep["text"])
                return answer_question(text, keep["question"], answer_text, today, by=by, actor=actor)
        else:
            def edit(text: str) -> str:
                return add_note(text, keep["text"], today, actor=actor)
        result = None
        try:
            if state in LOCKED_STATES:
                raise ForbiddenError(f"Tasks in {state} can't be edited here.")
            if commits:
                result = write_and_commit(config, source, state, file_name, request.form.get("hash", ""), edit, by,
                                          schema_of(config))
            else:
                apply_edit(source, state, file_name, request.form.get("hash", ""), edit, config["main_branch"],
                           schema_of(config), config["repo_root_path"], vcs=vcs.name)
        except EditError as exc:
            if exc.status == 404:
                abort(404)
            return render_task(source_name, state, file_name, error=str(exc), keep=keep, status=exc.status)
        what = "Answer" if kind == "answer" else "Note"
        if result is not None and result.status == "refused":
            # The form may be gone (the question was answered meanwhile): the message carries the text too.
            error = f"{what} not saved: {result.message}. Your text: {keep['text']}"
            if not os.path.isfile(os.path.join(source["root_path"], state, file_name)):
                abort(409, error)
            return render_task(source_name, state, file_name, keep=keep, status=409, error=error)
        if result is not None and result.status in ("problems", "failed"):
            return render_task(source_name, state, file_name,
                               error=f"{what} saved, not committed: {result.message}. The coordinator commits it "
                                     f"({vcs.commit_by_hand}).")
        rev = None
        if result is not None and result.status == "committed":
            rev = str(result.revision) if result.revision is not None else "committed"
        return redirect(url_for("task", source_name=source_name, state=state, file_name=file_name, saved=kind,
                                rev=rev))

    @app.post("/task/<source_name>/<state>/<file_name>/answer")
    def answer(source_name: str, state: str, file_name: str):
        return post_edit(source_name, state, file_name, "answer")

    @app.post("/task/<source_name>/<state>/<file_name>/note")
    def note(source_name: str, state: str, file_name: str):
        return post_edit(source_name, state, file_name, "note")

    @app.get("/plan/<file_name>")
    def plan(file_name: str):
        """A plan from plans_dir, rendered read-only."""
        if not PLAN_FILE.fullmatch(file_name):
            abort(404)
        try:
            with open(os.path.join(plans_path, file_name), encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            abort(404)
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), file_name)
        return render_template("plan.html", file=file_name, path=f"{config['plans_dir']}/{file_name}",
                               title=title, body=render_document(text, plan_link_url), refresh_ms=refresh_ms,
                               fingerprint="", mode="plan")

    @app.get("/api/update")
    def api_update():
        return jsonify(update_info())

    @app.get("/api/fingerprint")
    def api_fingerprint():
        return jsonify({"fingerprint": fingerprint(config["sources"]), "board": board_id(config)})

    return app
