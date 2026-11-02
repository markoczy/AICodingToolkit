"""
Reading and writing task files on disk, with the guards every writer shares (CLI and web board):

- writes happen only in a checkout of the main line (`main_branch`: the git branch, e.g. `master`, or the SVN
  line, e.g. `trunk`; the check is the version control backend's, taskboard/vcs), so task state is always
  current there; outside any checkout writes are allowed;
- a write replaces the file atomically and only when it is unchanged since it was read (content hash);
- a write that would add a consistency problem is refused and leaves the file untouched;
- writes and the periodic update (update.py) of one checkout never interleave: within a process they share
  `_WRITE_LOCK`, across processes (CLI, `tb serve`, `tb watch`) an OS lock on `log/write.lock`.

The version control commands themselves (the guard, the versioned move) are the backend's; this module only
chooses it by the `vcs` name (`backend_named`).
"""

import contextlib
import hashlib
import os
import tempfile
import threading
import time
from typing import Callable

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .config import BOARD_DIR
from .model import FILE_PATTERN, ID_PATTERN, STATES, check_tasks, load_tasks, parse_task
from .textedit import ConflictError, EditError, ForbiddenError, NotFoundError, WriteError

_WRITE_LOCK = threading.RLock()

# The cross-process lock of this checkout's task files (the log folder is ignored by version control).
# TB_WRITE_LOCK overrides the path (tests point it into a temporary folder, also for child processes).
LOCK_PATH = os.environ.get("TB_WRITE_LOCK") or os.path.join(BOARD_DIR, "log", "write.lock")
LOCK_WAIT_SECONDS = 5.0
# Windows refuses to replace a file another process has open (a reader, a virus scanner): retry this long.
REPLACE_RETRIES = 40
REPLACE_RETRY_SECONDS = 0.025
_lock_depth = threading.local()


class LockBusy(Exception):
    """The write lock was not free within the wait time."""


def _try_os_lock(fd: int) -> bool:
    """Take the OS lock on the open lock file without waiting; False when another handle holds it."""
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def write_lock(wait: float = LOCK_WAIT_SECONDS, path: str | None = None):
    """
    Hold the checkout's write lock for a write or an update: an OS lock (msvcrt.locking on Windows, flock
    elsewhere) on an open handle of the lock file, which stays on disk. The OS releases it when the holder's
    process ends, so a crashed holder never blocks anyone and there is nothing stale to take over. Each acquire
    opens its own handle, so threads of one process exclude each other too; re-entrant within a thread (the
    nesting depth is per thread, not per path: a nested acquire with another `path` in the same thread is not
    locked). Raises LockBusy when it can't be taken within wait seconds.
    """
    if getattr(_lock_depth, "n", 0) > 0:
        _lock_depth.n += 1
        try:
            yield
        finally:
            _lock_depth.n -= 1
        return
    path = path or LOCK_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + wait
        while not _try_os_lock(fd):
            if time.monotonic() >= deadline:
                raise LockBusy(f"{path} is held by another writer")
            time.sleep(0.05)
        _lock_depth.n = 1
        try:
            yield
        finally:
            _lock_depth.n = 0
            _os_unlock(fd)
    finally:
        os.close(fd)


def replace_file(src: str, dst: str) -> None:
    """os.replace, retried for about a second while Windows reports the target as in use by another process."""
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_RETRY_SECONDS)


@contextlib.contextmanager
def process_write_lock(wait: float | None = None):
    """_WRITE_LOCK with a time limit: a web write doesn't wait for a whole update, it fails like a busy lock."""
    if not _WRITE_LOCK.acquire(timeout=LOCK_WAIT_SECONDS if wait is None else wait):
        raise WriteError("Could not write the file: the task files are being updated (periodic update). Try again.")
    try:
        yield
    finally:
        _WRITE_LOCK.release()


@contextlib.contextmanager
def task_write_lock():
    """write_lock for a task-file write: a busy lock is the normal "could not write" error."""
    try:
        with write_lock():
            yield
    except LockBusy as exc:
        raise WriteError("Could not write the file: the task files are locked by another writer "
                         f"(an update or another tb command). Try again. ({exc})") from exc


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def backend_named(vcs: str):
    """The version control backend of a `vcs` name (taskboard/vcs); imported here to avoid an import cycle."""
    from . import vcs as vcs_package
    return vcs_package.backend_named(vcs)


def main_branch_block(root_path: str, main_branch: str, vcs: str = "git") -> str | None:
    """
    Why task files under root_path can't be written now, or None: the backend's main-line guard (git: the
    checkout is on main_branch, e.g. `master`, or git tracks nothing in the folder; svn: the folder is in a
    working copy of the main line, e.g. `^/trunk`). Outside any checkout writes are allowed.
    """
    return backend_named(vcs).main_branch_block(root_path, main_branch)


def task_file_path(root: str, state: str, file_name: str) -> str:
    """The path of `<root>/<state>/<file_name>`, refused unless it is an existing task file inside root."""
    if state not in STATES or not FILE_PATTERN.match(file_name):
        raise NotFoundError("Not a task file.")
    root_real = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root_real, state, file_name))
    if os.path.dirname(path) != os.path.join(root_real, state) or not os.path.isfile(path):
        raise NotFoundError("Task file not found.")
    return path


def find_task_files(root: str, task_id: str) -> list[tuple[str, str]]:
    """(state, file name) of every task file in root whose file name carries task_id (`T098-...md`)."""
    found = []
    for state in STATES:
        try:
            names = sorted(os.listdir(os.path.join(root, state)))
        except OSError:
            continue
        for name in names:
            m = FILE_PATTERN.match(name)
            if m and m.group(1) == task_id and os.path.isfile(os.path.join(root, state, name)):
                found.append((state, name))
    return found


def next_id(root: str, prefix: str) -> str:
    """One higher than the highest number of any task file of this prefix in any state folder; never reused."""
    highest = 0
    for state in STATES:
        try:
            names = os.listdir(os.path.join(root, state))
        except OSError:
            continue
        for name in names:
            m = FILE_PATTERN.match(name)
            if m:
                pm = ID_PATTERN.match(m.group(1))
                if pm.group(1) == prefix:
                    highest = max(highest, int(pm.group(2)))
    return f"{prefix}{highest + 1:03d}"


def new_problems(old_text: str | None, new_text: str, state: str, file_name: str, schema: dict | None,
                 prefix: str | None, old_state: str | None = None) -> list[str]:
    """
    Problems of new_text that old_text did not have (cross-file checks such as unknown blockers aside).

    Problems are compared by their stable keys (task file, kind, item), not by their text: answering one of two
    open questions in a finished task changes the message but not the problem, and is not a new one. A problem
    is new when it names a key old_text did not report. old_state is the state old_text is checked in (default
    state): a move passes the source state, so problems only the destination state has count as new.
    """
    def problems(text: str, in_state: str) -> list[str]:
        task = parse_task(text, in_state, file_name, f"{in_state}/{file_name}")
        board = {"states": list(STATES), "tasks": [task], "errors": []}
        own = {task["header"].get("id")}
        return [p for p in check_tasks(board, schema=schema, prefix=prefix, known_ids=own)
                if "names unknown task" not in p]
    before = set().union(*(p.keys for p in problems(old_text, old_state or state))) if old_text is not None \
        else set()
    return [p for p in problems(new_text, state) if not p.keys <= before]


def atomic_write(root: str, path: str, data: bytes, expected_hash: str | None) -> None:
    """Replace path with data via a temporary file; ConflictError if its content changed meanwhile."""
    fd, temp_path = tempfile.mkstemp(prefix=".task_board_", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with task_write_lock():
            if expected_hash is not None:
                with open(path, "rb") as f:
                    if content_hash(f.read()) != expected_hash:
                        raise ConflictError("The file changed while saving. "
                                            "Review the current version and submit again.")
            elif os.path.exists(path):
                raise ConflictError(f"{path} already exists.")
            replace_file(temp_path, path)
    except EditError:
        raise
    except OSError as exc:
        raise WriteError(f"Could not write the file: {exc}") from exc
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def apply_edit(source: dict, state: str, file_name: str, expected_hash: str | None,
               edit: Callable[[str], str], main_branch: str, schema: dict | None = None,
               repo_root: str | None = None, vcs: str = "git") -> list[str]:
    """
    Apply edit (text -> text) to one task file and write it atomically.

    expected_hash None means "whatever is on disk now" (the CLI reads and writes in one step). Raises an
    EditError subclass and leaves the file untouched when the write is not allowed, the file changed since it was
    displayed (ConflictError), or the edit would add a consistency problem. Returns the check_tasks problems of
    the written file within its source (normally none). vcs names the backend of the main-line guard.
    """
    if not source.get("editable", False):
        raise ForbiddenError(f"The source '{source['name']}' is read-only.")
    block = main_branch_block(source["root_path"], main_branch, vcs)
    if block:
        raise ForbiddenError(block)
    root = source["root_path"]
    path = task_file_path(root, state, file_name)
    # One lock for read, check and replace: two requests for the same file can't both pass the hash check, and
    # no periodic update (update.py) or other process changes the file in between.
    with process_write_lock(), task_write_lock():
        with open(path, "rb") as f:
            data = f.read()
        current_hash = content_hash(data)
        if expected_hash is not None and current_hash != expected_hash:
            raise ConflictError("The file changed since you opened it. "
                                "Review the current version and submit again.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EditError(f"The file is not UTF-8: {exc}") from exc
        new_text = edit(text)
        if new_text == text:
            return []
        added = new_problems(text, new_text, state, file_name, schema, source.get("prefix"))
        if added:
            raise EditError("The edit would make the file inconsistent: " + "; ".join(added))
        atomic_write(root, path, new_text.encode("utf-8"), current_hash)
    rel = f"{state}/{file_name}"
    return [p for p in check_tasks(load_tasks(root), repo_root, schema, source.get("prefix"))
            if p.startswith(rel + ":") and "names unknown task" not in p]


def is_versioned(path: str, vcs: str = "git") -> bool:
    """True when the version control tracks path (committed, staged or scheduled for addition)."""
    return backend_named(vcs).is_versioned(path)


def move_file(repo_root: str, src: str, dst: str, vcs: str = "git") -> str:
    """Move a task file: the backend's versioned move (`git mv`: the rename is staged; `svn mv`: history follows
    the file) when the file is versioned, else a plain rename. Returns how it was moved."""
    with task_write_lock():
        if os.path.exists(dst):
            raise ConflictError(f"{dst} already exists.")
        if is_versioned(src, vcs):
            try:
                return backend_named(vcs).move_file(repo_root, src, dst)
            except RuntimeError as exc:
                raise WriteError(str(exc)) from exc
        replace_file(src, dst)
        return "rename"
