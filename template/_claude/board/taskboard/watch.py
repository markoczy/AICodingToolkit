"""
Board watcher for the supervisor session, and with `--for <dev>` for a coordinator session (below).

Makes sure the web board is running, keeps the checkout current (the backend's periodic update every
`watch.update_seconds`, see update.py and taskboard/vcs), then sleeps until there is input for the supervisor:

- someone writes something through this checkout's board (a new `<name> (board):` Log line, `user (board):` in
  older files or without people in board.json, in any task file of any source);
- an update brings another dev's input (INCOMING): a board line written on their board, a new task, a new open
  question for the supervisor, a task that now waits on the supervisor. Only differences across a move of the
  local revision count (git: HEAD on the main branch; svn: the working-copy revision of the update paths), so
  the supervisor's own CLI edits never do. With git, a move made only of the checkout's own commits never counts,
  and only tasks whose files changed between the two commits count (so uncommitted edits next to a pull don't);
  svn can't tell those apart, so every new key counts. Known limitation: an own commit that arrives in the same
  poll as another dev's commit, or a task both changed, is reported once.

Not everything that arrives wakes it. A batch starts (and is extended) only by what the supervisor must act on:
a board line written on this checkout's board, a new open question for the supervisor (`question`), a task now
waiting on the supervisor (`waiting`). A new task (`task`) and another dev's board line (`board`) are reported
with the next wake but never cause one (session start shows them anyway). The coordinator's watcher wakes for
`question` keys and `board` lines on the dev's tasks; a task newly theirs (`owned`) is reported, never a wake.

It then waits until nothing more arrived for `quiet_seconds` (people often answer several questions in a row)
AND at least `hold_seconds` passed since the batch's first input (the supervisor wakes less often: a slow
conversation on the board becomes one wake). Then it prints the input, with the state and the answered
questions of each task that has board lines or now waits on the supervisor (so the usual `tb show` is not
needed), and the tasks waiting on the supervisor, and exits 0. The marker `log/wake-now.json` (the board's
"Wake now" button, `/wake`) ends the wait at once: hold and quiet are ignored, the report is preceded by
`WAKE NOW: requested by <name> at <time>`, and with nothing pending the WAITING ON section alone is printed
(exit 0). A marker that exists when the watcher starts is deleted without a wake (it was meant for a watcher
that was not there).

A conflict, a diverged branch (git) or 3 failed updates in a row print `UPDATE STOPPED` / `UPDATE FAILING` and
exit 3, once per conflict or failure (a restarted watcher does not exit again for the same one, see
`log/watch-update.json`); a single failure only prints a line. Those are never held.

Exiting is what wakes the supervisor: start it with the Bash tool's `run_in_background`, handle the input,
`tb done "<message>"` to commit the task files, start it again. While it sleeps it costs no model usage. CLI
edits never write `(board)` actors, so they never trigger it.

Lifecycle: the watcher stops (exit 4, `SESSION GONE`) once a process of the session that started it ended
(session.py), so an orphaned watcher never restarts its board. The board it starts is detached (it survives the
watcher's exit and restart) but stops itself after `watch.board_idle_minutes` without any request (idle.py); the
watcher's look at the board every poll keeps it alive. The watcher only uses its own checkout's board: another
board on the port (another checkout or project, or an older version) is `BOARD FAILED` (exit 2). The supervisor's
watcher starts its board with `--no-commit`: that board never commits (commit.py).

Coordinator mode (`tb watch --for <dev>`): same loop, exits and lifecycle, but INCOMING is only what concerns
the dev (person_state): a new open question for them (addressed `@<dev>:`, or unaddressed on a task they own), a
task newly owned by them, another person's board line on a task they own. Every line written on this checkout's
board (BOARD INPUT) wakes it too. The heading is `WAITING ON <DEV>:`, and the board it starts commits answers and
notes when local.json has `board_commits` on.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from .config import BOARD_DIR, board_id, log_dir
from .model import FILE_PATTERN, effective_owner, load_tasks, waits_on_person
from .update import FAILURES_TO_WAKE, Updater
from .vcs import backend
from .web.options import question_texts

# A Log line the web board wrote: `- <date> <name> (board): ...` (`user (board)` without people in board.json).
# The CLI refuses a --by containing "(board)", so only the board writes it.
BOARD_LINE = re.compile(r"^- \d{4}-\d{2}-\d{2} ([\w-]+) \(board\):")
# A board line that answered a question: its Qn gets a question/answer block in the report.
ANSWERED = re.compile(r"\banswered (Q\d+)\b")
EXIT_UPDATE = 3
EXIT_SESSION_GONE = 4
# Steps in a row with the checkout locked by an update before the watcher prints the backend's lock note (an
# update that long is more likely a crashed command's stale lock). A step runs every watch.poll_seconds (default
# 5 s), so 20 is about 100 s; a step with a due update can observe twice and count twice.
LOCK_NOTE_POLLS = 20
KIND_ORDER = {"task": 0, "question": 1, "owned": 2, "waiting": 2, "board": 3}
# The INCOMING kinds that start or extend a batch (module docstring): the supervisor's and the coordinator's.
WAKE_KINDS_SUPERVISOR = frozenset({"question", "waiting"})
WAKE_KINDS_COORDINATOR = frozenset({"question", "board"})
LOG_MAX_BYTES = 1024 * 1024
WAKE_FILE = "wake-now.json"
# The report's question/answer block: the question's first line and the answer are cut here.
QUESTION_CHARS = 160
ANSWER_CHARS = 300
BLOCK_INDENT = " " * 8


def wake_kinds(person: str | None) -> frozenset:
    """The INCOMING kinds that wake the supervisor's watcher (None) or a coordinator's."""
    return WAKE_KINDS_SUPERVISOR if person is None else WAKE_KINDS_COORDINATOR


def wake_path(config: dict) -> str:
    """The "wake now" marker `log/wake-now.json` next to the config (written by the board's /wake)."""
    return os.path.join(log_dir(config), WAKE_FILE)


def request_wake(config: dict, by: str, at: str | None = None) -> None:
    """Write the marker `{"by": <name>, "at": <ISO local time>}`: the running watcher exits at its next poll."""
    path = wake_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"by": by, "at": at or datetime.now().isoformat(timespec="seconds")}, f)
    os.replace(temp, path)


def take_wake(config: dict) -> dict | None:
    """The marker's content (deleted on the way), or None when there is none. An unreadable marker is deleted
    too and comes back with what could be read."""
    path = wake_path(config)
    if not os.path.exists(path):
        return None
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    try:
        os.remove(path)
    except OSError:
        pass
    return {"by": str(data.get("by") or "someone"), "at": str(data.get("at") or "an unknown time")}


def board_url(config: dict) -> str:
    return f"http://{config['host']}:{config['port']}/api/fingerprint"


def board_state(config: dict) -> str:
    """
    "ours" when this checkout's board answers on the port, "foreign" when something else does (another
    checkout's or project's board, a board without the id of older versions, another program), "down" when
    nothing answers. Every call is a request, so it also keeps a watcher-started board alive (idle exit).
    """
    try:
        with urllib.request.urlopen(board_url(config), timeout=3) as response:
            data = json.load(response)
    except urllib.error.HTTPError:
        return "foreign"
    except (urllib.error.URLError, OSError):
        return "down"
    except ValueError:  # not JSON: not a task board
        return "foreign"
    if isinstance(data, dict) and data.get("board") == board_id(config):
        return "ours"
    return "foreign"


def foreign_message(config: dict) -> str:
    from .cli import pid_hint
    port = config["port"]
    return (f"port {port} is held by another task board (another checkout or project, or an older board "
            f"version) or another program: stop it ({pid_hint(port)}) or use --port N")


def board_command(config: dict, no_update: bool, commits: bool = False) -> list[str]:
    """
    The `tb serve` command the watcher starts (with --no-update while the watcher itself updates, and with the
    idle exit of watch.board_idle_minutes unless it is 0). Without commits (the supervisor's watcher) it passes
    --no-commit, so that board never commits whatever local.json says; with commits (`tb watch --for`), the board
    follows local.json board_commits.
    """
    command = [sys.executable, os.path.join(BOARD_DIR, "tb.py"), "--config", config["config_path"], "serve"]
    if config.get("port_override"):
        command += ["--port", str(config["port"])]
    if not commits:
        command.append("--no-commit")
    idle = config.get("watch", {}).get("board_idle_minutes", 15)
    if idle:
        command += ["--idle-exit", f"{idle:g}"]
    return command + (["--no-update"] if no_update else [])


def _log_tail(log_path: str) -> str:
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 2000))
            lines = [x.strip() for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    except OSError:
        return "no output"
    return lines[-1] if lines else "no output"


def rotate_log(log_path: str, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Move a log larger than max_bytes to <log>.1 (replacing the old backup). Only at start: while the board
    runs, Windows keeps the file open."""
    try:
        if os.path.getsize(log_path) > max_bytes:
            os.replace(log_path, log_path + ".1")
    except OSError:
        pass  # missing, or still held open by a running board: keep appending


def start_board(config: dict, no_update: bool = False, commits: bool = False) -> None:
    log_dir = os.path.join(BOARD_DIR, "log")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "server.log")
    rotate_log(log_path)
    command = board_command(config, no_update, commits)
    kwargs: dict = {"stdin": subprocess.DEVNULL, "cwd": config["repo_root_path"]}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, **kwargs)
    for _ in range(20):
        found = board_state(config)
        if found == "ours":
            return
        if found == "foreign":
            raise RuntimeError(foreign_message(config))
        if process.poll() is not None:  # `tb serve` exited: its last message says why (e.g. the port is busy)
            found = board_state(config)
            if found == "ours":  # a slow but running board of this checkout is not a failure
                return
            if found == "foreign":
                raise RuntimeError(foreign_message(config))
            raise RuntimeError(f"The board could not start: {_log_tail(log_path)} (see {log_path}).")
        time.sleep(1)
    raise RuntimeError(f"The board did not start (see {log_path}).")


def _tasks(config: dict):
    for source in config["sources"]:
        try:
            board = load_tasks(source["root_path"])
        except OSError:
            continue
        for task in board["tasks"]:
            yield source, task


def snapshot(config: dict) -> set[tuple[str, str, str]]:
    """(source, task id, log line) of every Log line the web board wrote (BOARD_LINE)."""
    lines: set[tuple[str, str, str]] = set()
    for source, task in _tasks(config):
        task_id = str(task["header"].get("id") or task["file"])
        for line in task["sections"].get("Log", "").split("\n"):
            if BOARD_LINE.match(line.strip()):
                lines.add((source["name"], task_id, line.strip()))
    return lines


def state(config: dict, person: str | None = None) -> dict[tuple, str]:
    """
    Everything that is input for the supervisor when it arrives with an update, as {key: report line}: task
    files, open questions for the supervisor, tasks waiting on the supervisor and board Log lines. Tasks are
    keyed by the id in their file name, so a file whose header can't be read (e.g. a conflict) keeps its key.
    With person (`tb watch --for`), what is input for that person's coordinator instead (person_state).
    """
    if person is not None:
        return person_state(config, person)
    out: dict[tuple, str] = {}
    many = len(config["sources"]) > 1
    for source, task in _tasks(config):
        m = FILE_PATTERN.match(task["file"])
        if not m:
            continue
        h = task["header"]
        task_id = m.group(1)
        name = f"{source['name']}/{task_id}" if many else task_id
        title = h.get("title") or f"({task['file']})"
        out[("task", source["name"], task_id)] = f"{name} new task: {title} ({task['state']})"
        for q in task["questions"]:
            if q["status"] == "open" and q["for"] == "supervisor":
                out[("question", source["name"], task_id, q["id"])] = f"{name} {q['id']} open, for supervisor"
        if h.get("waiting_on") == "supervisor":
            out[("waiting", source["name"], task_id)] = f"{name} now waiting on supervisor ({task['state']})"
        for line in task["sections"].get("Log", "").split("\n"):
            line = line.strip()
            if BOARD_LINE.match(line):
                out[("board", source["name"], task_id, line)] = f"{name} {line[2:].split(' ', 1)[1]}"
    return out


def person_state(config: dict, person: str) -> dict[tuple, str]:
    """
    The coordinator's state for person: open user questions for them (addressed `@<person>:`, or unaddressed on a
    task they own, as the board's "waiting on me"), the tasks they own (null owners count for default_owner) and
    board Log lines of other people on those tasks. Every other board Log line has the value None: it is not
    input, but the watcher must know it arrived with an update (only lines written on this checkout's board are
    BOARD INPUT).
    """
    out: dict[tuple, str | None] = {}
    many = len(config["sources"]) > 1
    default_owner = config.get("default_owner")
    for source, task in _tasks(config):
        m = FILE_PATTERN.match(task["file"])
        if not m:
            continue
        task_id = m.group(1)
        name = f"{source['name']}/{task_id}" if many else task_id
        owned = effective_owner(task["header"], default_owner) == person
        if owned:
            out[("owned", source["name"], task_id)] = f"{name} now owned by {person} ({task['state']})"
        for q in task["questions"]:
            if q["status"] == "open" and waits_on_person({"header": task["header"], "questions": [q]}, person,
                                                         default_owner):
                out[("question", source["name"], task_id, q["id"])] = f"{name} {q['id']} open, for {person}"
        for line in task["sections"].get("Log", "").split("\n"):
            line = line.strip()
            match = BOARD_LINE.match(line)
            if not match:
                continue
            # One key per line whatever its value, so a task that becomes the person's doesn't report old lines.
            text = f"{name} {line[2:].split(' ', 1)[1]}" if owned and match.group(1) != person else None
            out[("board", source["name"], task_id, line)] = text
    return out


def waiting_on_supervisor(config: dict) -> list[str]:
    return [f"  {task['header'].get('id')} {task['header'].get('title')} ({task['state']})"
            for _, task in _tasks(config) if task["header"].get("waiting_on") == "supervisor"]


def waiting_on(config: dict, person: str | None = None) -> tuple[str, list[str]]:
    """(the WAITING ON heading, its lines): tasks waiting on the supervisor, or on person (waits_on_person)."""
    if person is None:
        return "WAITING ON SUPERVISOR:", waiting_on_supervisor(config)
    default_owner = config.get("default_owner")
    return f"WAITING ON {person.upper()}:", [
        f"  {task['header'].get('id')} {task['header'].get('title')} ({task['state']})"
        for _, task in _tasks(config) if waits_on_person(task, person, default_owner)]


def print_waiting(config: dict, person: str | None = None) -> None:
    heading, lines = waiting_on(config, person)
    print(heading + ("" if lines else " none"))
    for line in lines:
        print(line)


def _cut(text: str, limit: int) -> str:
    text = text.replace("\r\n", "\n").replace("\n", " / ")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _question_lines(task: dict, qids: list[str]) -> list[str]:
    """
    `Qn: <first line of the question>` and `answer (<who>): <answer>` for each question id, from the task's
    question fields (the answered line `... → <answer>`; a multi-line question carries the answer on its last
    line, so the full text is split at the last arrow). A question the file does not have is skipped; an
    open one gets its question line only.
    """
    full = question_texts(task["sections"].get("Questions", ""))
    by_id = {q["id"]: q for q in task["questions"]}
    lines = []
    for qid in qids:
        q = by_id.get(qid)
        if q is None:
            continue
        first, answer = q["text"], None
        if q["status"] == "answered":
            whole = full.get(qid, q["text"])
            if " → " in whole:
                _, _, answer = whole.rpartition(" → ")
            else:
                answer = ""
            if answer and " → " in first and first.endswith(answer):
                first = first[:-len(answer) - 3]
        lines.append(f"{qid}: {_cut(first.strip(), QUESTION_CHARS)}")
        if answer is not None:
            lines.append(f"answer ({q['answered_by']}): {_cut(answer.strip(), ANSWER_CHARS)}")
    return lines


def task_block(config: dict, task: dict, board_lines: list[str]) -> list[str]:
    """
    The lines of a task's report block (indented by the caller): its state, waiting_on and owner, then one
    question/answer pair per question its board lines mention ("answered Qn"), in number order.
    """
    h = task["header"]
    owner = effective_owner(h, config.get("default_owner")) or "-"
    lines = [f"state {task['state']}, waiting on {h.get('waiting_on') or '-'}, owner {owner}"]
    mentioned = sorted({m.group(1) for line in board_lines for m in ANSWERED.finditer(line)},
                       key=lambda q: int(q[1:]))
    return lines + _question_lines(task, mentioned)


def report(config: dict, new_lines: set[tuple[str, str, str]], incoming: dict | None = None,
           revisions: tuple | None = None, person: str | None = None) -> None:
    """
    BOARD INPUT (this checkout's board lines), INCOMING (what an update brought) and the WAITING ON list. A
    task with board lines, or with a `waiting` key in INCOMING, gets a block after its lines (task_block) with
    its state and the questions those lines answered, so the supervisor needs no `tb show` for the usual case.
    """
    # snapshot() keys a task by its header id (else the file name), state() by the id in the file name.
    tasks: dict[tuple[str, str], dict] = {}
    for source, task in _tasks(config):
        tasks[(source["name"], str(task["header"].get("id") or task["file"]))] = task
        m = FILE_PATTERN.match(task["file"])
        if m:
            tasks.setdefault((source["name"], m.group(1)), task)
    incoming = incoming or {}
    board_lines: dict[tuple[str, str], list[str]] = {}
    for source, task_id, line in new_lines:
        board_lines.setdefault((source, task_id), []).append(line)
    for key in incoming:
        if key[0] == "board":
            board_lines.setdefault((key[1], key[2]), []).append(key[3])

    def block(source: str, task_id: str) -> None:
        task = tasks.get((source, task_id))
        if task is not None:
            for line in task_block(config, task, board_lines.get((source, task_id), [])):
                print(f"{BLOCK_INDENT}{line}")

    if new_lines:
        print("BOARD INPUT:")
        ordered = sorted(new_lines)
        for i, (source, task_id, line) in enumerate(ordered):
            prefix = f"{source}/" if len(config["sources"]) > 1 else ""
            print(f"  {prefix}{task_id}: {line}")
            if i + 1 == len(ordered) or ordered[i + 1][:2] != (source, task_id):
                block(source, task_id)
    if incoming:
        print(f"INCOMING ({backend(config).incoming_header(revisions)}):")
        for key in sorted(incoming, key=lambda k: (KIND_ORDER[k[0]], k[1:])):
            print(f"  {incoming[key]}")
            if key[0] == "waiting":
                block(key[1], key[2])
    print_waiting(config, person)


def _reported_path(config: dict) -> str:
    return os.path.join(log_dir(config), "watch-update.json")


def _load_reported(config: dict) -> str:
    try:
        with open(_reported_path(config), encoding="utf-8") as f:
            return str(json.load(f).get("reported") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _save_reported(config: dict, key: str) -> None:
    path = _reported_path(config)
    try:
        if not key:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"reported": key}, f)
    except OSError:
        pass


class _Cycle:
    """
    The update part of the watch loop: run due updates, collect INCOMING, decide on exit 3.

    INCOMING is what differs between the state before and after the local revision moved (git: HEAD on the main
    branch; svn: the working-copy revision of the update paths; both local), whoever moved it: this watcher, the
    supervisor's own pull or update, a board that updates itself. The state is refreshed on every step, so the
    supervisor's own `tb` edits (which don't move the revision) never count; with git, off the main branch HEAD
    is not tracked (the last main-branch state is kept). Off together with the update (`--no-update`): then the
    watcher is as before.
    """

    def __init__(self, config: dict, updater: Updater, revision_of=None, running_of=None, own_of=None,
                 changed_of=None, person: str | None = None):
        self.config = config
        self.updater = updater
        self.backend = backend(config)
        self.person = person
        self.track = updater.enabled
        self.revision_of = revision_of or local_revision
        self.running_of = running_of or update_running
        self.own_of = own_of or own_commits_only
        self.changed_of = changed_of or changed_tasks
        self.incoming: dict[tuple, str] = {}
        # Board lines that arrived with an update but are no input for person (state() value None): never
        # BOARD INPUT either. Always empty for the supervisor.
        self.arrived: set[tuple] = set()
        self.revisions: list = [None, None]
        self.reported = _load_reported(config)
        self.stop_message = ""
        self.last_state: dict | None = None
        self.last_rev = None
        self.vcs_seen = False  # revision_of has answered once: a later None is a failed look at the revision
        self.locked_polls = 0
        self.lock_noted = False

    def observe(self) -> None:
        """
        Absorb the current state, but only when it belongs to one revision: an update or pull by anyone moves
        the revision before its files arrive (or writes files before it is done), so while one runs (the
        backend's lock), or when the revision moved while the state was read, the last state is kept and the next
        poll looks again.
        """
        rev = self.revision_of(self.config)
        if rev is None and self.vcs_seen:
            return  # no revision (git: off the main branch; the look failed): keep the last state as the baseline
        if rev is not None:
            self.vcs_seen = True
        now = state(self.config, self.person)
        if rev is not None:
            running = self.running_of(self.config)
            self.note_lock(running)
            if running or self.revision_of(self.config) != rev:
                return
        self.absorb(rev, now)

    def note_lock(self, locked: bool) -> None:
        """Print the backend's lock note once when the lock was seen on LOCK_NOTE_POLLS polls in a row. Never
        wakes anyone."""
        if not locked:
            self.locked_polls = 0
            self.lock_noted = False
            return
        self.locked_polls += 1
        if self.locked_polls >= LOCK_NOTE_POLLS and not self.lock_noted:
            self.lock_noted = True
            print(self.backend.lock_note, flush=True)

    def absorb(self, rev, now: dict | None = None) -> None:
        """
        Take the state; when the revision moved since the last look, its new keys are INCOMING, unless the move
        was only this checkout's own commits (own_commits_only), and only for tasks whose files changed between
        the two revisions when the backend can tell (changed_tasks): the state is read from the working tree, so
        an uncommitted edit of the supervisor (e.g. `tb move` right after its own pull) is not what the pull
        brought.
        """
        now = state(self.config, self.person) if now is None else now
        moved = rev is not None and self.last_rev is not None and rev != self.last_rev
        if moved and self.last_state is not None and not self.own_of(self.config, self.last_rev, rev):
            new = {k: v for k, v in now.items() if k not in self.last_state}
            touched = self.changed_of(self.config, self.last_rev, rev) if new else None
            if touched is not None:
                new = {k: v for k, v in new.items() if k[1:3] in touched}
            self.arrived.update(k[1:] for k, v in new.items() if v is None)
            new = {k: v for k, v in new.items() if v is not None}
            if new:
                self.incoming.update(new)
                if self.revisions[0] is None:
                    self.revisions[0] = self.last_rev
                self.revisions[1] = rev
        if rev is not None:
            self.last_rev = rev
        self.last_state = now

    def step(self) -> int | None:
        """Notice updates by anyone, run a due update; EXIT_UPDATE when the supervisor must be woken for it."""
        if not self.track:
            return None
        self.observe()
        if not self.updater.due():
            return None
        result, notices = self.updater.run()
        for line in notices:
            print(line, flush=True)
        rev = result.head if result.head is not None else result.revision
        if rev is not None and result.status != "off-branch":
            self.absorb(rev)  # this watcher's own update has finished
        else:
            self.observe()
        if result.status in ("ok", "changed") and self.reported:
            self.reported = ""
            _save_reported(self.config, "")
        key = message = ""
        if result.status == "conflict":
            key = "conflict: " + ", ".join(sorted(result.conflicts))
            message = f"UPDATE STOPPED: {result.message}"
        elif result.status == "diverged":
            key = f"diverged: {result.revision}"
            message = f"UPDATE STOPPED: {result.message}"
        elif result.status == "error" and self.updater.failures >= FAILURES_TO_WAKE:
            key = f"failure: {result.message}"
            message = (f"UPDATE FAILING: {self.updater.failures} {self.backend.name} updates in a row failed "
                       f"({result.message}); the board shows the local files only")
        if key and key != self.reported:
            self.reported = key
            _save_reported(self.config, key)
            self.stop_message = message
            return EXIT_UPDATE
        return None


def local_revision(config: dict):
    """The backend's local revision the watcher tracks (git: the full HEAD commit while on the main branch; svn:
    the working-copy revision of the first update path); None when it can't be read or is no input."""
    return backend(config).local_revision(config)


def own_commits_only(config: dict, old, new) -> bool:
    """True when the revision moved only through this checkout's own commits (git: the reflog; svn: never)."""
    return backend(config).own_commits_only(config, old, new)


def changed_tasks(config: dict, old, new) -> set[tuple[str, str]] | None:
    """(source name, task id) of the task files that differ between two revisions (git: `git diff`), or None
    when the backend can't tell (svn): every key counts then."""
    return backend(config).changed_tasks(config, old, new)


def update_running(config: dict) -> bool:
    """True while an update or merge by anyone holds the checkout's lock (git: `.git/index.lock`; svn: the
    lock column of `svn status` in the update paths)."""
    return backend(config).update_running(config)


def board_updates_itself(config: dict) -> bool:
    """True when the running board reports its own periodic update (/api/update status other than off)."""
    url = f"http://{config['host']}:{config['port']}/api/update"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.load(response).get("status") not in (None, "off")
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def ensure_board(config: dict, updating: bool, commits: bool = False) -> bool:
    """
    Start this checkout's board when nothing runs on the port (True when it was started); RuntimeError when
    another board holds the port.
    """
    found = board_state(config)
    if found == "foreign":
        raise RuntimeError(foreign_message(config))
    if found == "down":
        start_board(config, no_update=updating, commits=commits)
        return True
    return False


def watch(config: dict, start_server: bool = True, quiet_seconds: float | None = None, once: bool = False,
          update: bool = True, update_seconds: float | None = None, updater: Updater | None = None,
          revision_of=None, running_of=None, own_of=None, changed_of=None, guard=None,
          person: str | None = None, hold_seconds: float | None = None, clock=time.monotonic) -> int:
    """
    guard: a session.SessionGuard; when one of its processes ended, the watcher prints SESSION GONE and exits
    EXIT_SESSION_GONE without touching the board. None: no session check.
    person: the coordinator's watcher (`tb watch --for`): INCOMING is what concerns person (person_state), and
    the board it starts commits per local.json. None: the supervisor's watcher, whose board never commits.
    hold_seconds overrides `watch.hold_seconds`; clock gives the monotonic seconds of the quiet and hold
    periods (tests inject it). revision_of, running_of, own_of, changed_of replace the backend's functions
    (tests).
    """
    quiet = quiet_seconds if quiet_seconds is not None else config["watch"]["quiet_seconds"]
    hold = hold_seconds if hold_seconds is not None else config["watch"].get("hold_seconds", 0)
    poll = config["watch"]["poll_seconds"]
    if once:
        print_waiting(config, person)
        return 0
    interval = update_seconds if update_seconds is not None else config["watch"]["update_seconds"]
    if updater is None:
        updater = Updater(config, interval if update else 0)
    updating = updater.enabled
    cycle = _Cycle(config, updater, revision_of, running_of, own_of, changed_of, person)
    commits = person is not None
    kinds = wake_kinds(person)

    def stop() -> int:
        print(cycle.stop_message)
        report(config, set(), cycle.incoming, tuple(cycle.revisions), person)
        return EXIT_UPDATE

    def session_gone() -> bool:
        ended = guard.gone() if guard is not None else None
        if ended is not None:
            from .session import gone_message
            print(gone_message(ended), flush=True)
        return ended is not None

    try:
        if session_gone():
            return EXIT_SESSION_GONE
        take_wake(config)  # a marker from before this watcher: session start shows what waits
        if person is None and config.get("board_commits"):
            print("note: local.json has board_commits on, but this is the supervisor's watcher: a board it starts "
                  "never commits (--no-commit), but a board already running here that was started otherwise "
                  "(tb serve by hand, tb watch --for) does. Board commits are for coordinator checkouts: run "
                  "tb me --board-commits off", flush=True)
        elif person is not None and config.get("me") and config["me"] != person:
            print(f"note: this checkout's person is {config['me']} (tb me), the board writes as them, not as "
                  f"{person}", flush=True)
        if start_server and not ensure_board(config, updating, commits) and updating and board_updates_itself(config):
            print("note: the running board also updates the checkout itself; harmless (the write lock serializes the "
                  "updates), but it is a second updater: restart it with --no-update, or stop it and let the "
                  "watcher start its own", flush=True)
        print(f"watching {', '.join(s['name'] for s in config['sources'])} "
              + (f"for {person} " if person else "")
              + f"(board http://{config['host']}:{config['port']}/"
              + (f"; {cycle.backend.update_description(config, updater.interval)}" if updating else "")
              + (f"; hold {hold:g} s" if hold else "") + ")", flush=True)
        # Input that arrived while no watcher ran is in the files already (session start shows it): the base
        # is taken after a first update.
        if cycle.step() == EXIT_UPDATE:
            return stop()
        cycle.incoming.clear()
        cycle.revisions = [None, None]
        base = snapshot(config)
        # The batch: the time its first waking input was seen, the time the waking input last changed, and
        # that input (board lines and waking INCOMING keys); None/empty between batches.
        since = last_change = None
        last_sig: tuple = ()
        while True:
            time.sleep(poll)
            if session_gone():
                return EXIT_SESSION_GONE
            if start_server:
                ensure_board(config, updating, commits)
            if cycle.step() == EXIT_UPDATE:
                return stop()
            current = snapshot(config)
            arrived = {key[1:] for key in cycle.incoming if key[0] == "board"} | cycle.arrived
            new_lines = (current - base) - arrived
            wake = take_wake(config)
            if wake is not None:
                print(f"WAKE NOW: requested by {wake['by']} at {wake['at']}")
                report(config, new_lines, cycle.incoming, tuple(cycle.revisions), person)
                return 0
            wakers = frozenset(k for k in cycle.incoming if k[0] in kinds)
            if not new_lines and not wakers:
                if current != base:
                    base = current  # lines that are no input (another dev's, arrived with an update)
                since = last_change = None
                last_sig = ()
                continue
            now = clock()
            sig = (frozenset(new_lines), wakers)
            if sig != last_sig:
                last_sig, last_change = sig, now
                if since is None:
                    since = now
            if now - last_change < quiet or now - since < hold:
                continue
            report(config, new_lines, cycle.incoming, tuple(cycle.revisions), person)
            return 0
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"BOARD FAILED: {exc}", file=sys.stderr)
        return 2
