"""
The SVN backend: trunk guard and `svn mv` (store), `svn update` of the task folders (update), the watcher's
working-copy revision and lock column (watch), and board commits with the out-of-date redo (commit).

The periodic update: `svn update --non-interactive --accept postpone <update_paths>` (board.json
`watch.update_paths`, default `["AI"]`, never the code), every `watch.update_seconds`. The rules:

- `--accept postpone`: no prompt can hang the process and a conflict never picks a side; the tools never
  resolve anything.
- While a conflict exists in the update paths (`svn status`, local) nothing is updated; updating resumes by
  itself once the conflict is gone (`svn revert` / `svn resolve`).
- While the update paths hold local modifications (uncommitted task edits) nothing is updated unless
  `watch.update_when_dirty` is true. Unversioned files (`?`) don't count, added ones do.
- The update runs under the cross-process write lock (store.write_lock), so no task-file write interleaves.

Board commits: `svn commit` of the one file. When the commit is out of date and the file had no local edits
before the board's write, commit.py reverts the file, updates it, redoes the edit on the incoming version and
commits once more (`supports_redo`).
"""

import os
import re
import shutil
import subprocess

from ..store import LockBusy, write_lock
from ..winproc import no_window_flags
from . import REVIEW_FIELDS, Backend, UpdateResult, norm_path

TIMEOUT_SECONDS = 120
LOCAL_TIMEOUT_SECONDS = 60  # svn status / svn info (no server contact)
MV_TIMEOUT_SECONDS = 60
COMMIT_TIMEOUT_SECONDS = 120
LOCK_NOTE = "the update paths are locked; run svn cleanup if no svn command is running"
# `svn update` item lines: action, property action, lock break, tree conflict, space, path.
UPDATE_LINE = re.compile(r"^([ADUCGER ])([UCG ])([B ])([C ]) (\S.*)$")
# `svn status` item lines: seven status columns, a space, the path.
STATUS_LINE = re.compile(r"^(.)(.)(.)(.)(.)(.)(.) (\S.*)$")
SVN_ERROR = re.compile(r"\b(?:svn: )?(E\d{6}):?\s*(.*)")
DIRTY_ITEM = set("MADR~!")
# svn's "out of date" codes for a commit whose base is older than the repository's version of the file.
OUT_OF_DATE = ("E155011", "E160024", "E160028", "E170004")
COMMITTED = re.compile(r"Committed revision (\d+)\.")


def svn_env() -> dict:
    """The environment of every svn call: untranslated messages (the item and status columns are parsed);
    `--non-interactive` on every command keeps svn from prompting, credentials are never read or passed."""
    return {**os.environ, "LC_MESSAGES": "C"}


def _env() -> dict:
    return svn_env()


def _run(config: dict, args: list[str], timeout: float = TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """An svn command in the repo root (the update's runner; tests replace it)."""
    return subprocess.run(["svn", "--non-interactive", *args], cwd=config["repo_root_path"], capture_output=True,
                          text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=_env(),
                          encoding="utf-8", errors="replace", creationflags=no_window_flags())


def first_error(stderr: str) -> str:
    """The svn error code and text (`E170013 Unable to connect ...`), else the first non-empty line."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        m = SVN_ERROR.search(line)
        if m:
            return f"{m.group(1)} {m.group(2)}".strip()
    return lines[0] if lines else "svn failed without a message"


def svn_available() -> bool:
    return shutil.which("svn") is not None


# ---- the guard ----

def svn_location(path: str) -> str | None:
    """
    The repository-relative URL of the SVN working copy item at path (e.g. `^/trunk/AI/Tasks`), or None when path
    is not in a working copy or svn is not installed. Runs `svn info`; never contacts the server.
    """
    try:
        result = subprocess.run(["svn", "info", "--show-item", "relative-url", "--non-interactive", path],
                                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                env=svn_env(), encoding="utf-8", errors="replace", creationflags=no_window_flags())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _line_root(main_branch: str) -> str:
    """`trunk`, `^/trunk` or `/trunk` -> `^/trunk`."""
    return "^/" + main_branch.strip().lstrip("^").strip("/")


def main_branch_block(root_path: str, main_branch: str) -> str | None:
    """
    Why task files under root_path can't be written now, or None (in a working copy of the main line, e.g.
    `^/trunk`, or not in an SVN working copy at all).
    """
    location = svn_location(root_path)
    line = _line_root(main_branch)
    if location is not None and location != line and not location.startswith(line + "/"):
        return (f"The task folder is in an SVN working copy of '{location}', not of '{line}'; task files change "
                f"in the {line} working copy only. Nothing was written.")
    return None


def _versioned(path: str) -> bool:
    """True when SVN tracks path (committed or scheduled for addition)."""
    try:
        result = subprocess.run(["svn", "info", "--show-item", "kind", "--non-interactive", path],
                                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                env=svn_env(), encoding="utf-8", errors="replace", creationflags=no_window_flags())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def svn_mv(repo_root: str, src: str, dst: str) -> str:
    """`svn mv` (history follows the file); RuntimeError with the reason when it fails."""
    try:
        result = subprocess.run(["svn", "mv", "--non-interactive", "--force", src, dst], cwd=repo_root,
                                capture_output=True, text=True, timeout=MV_TIMEOUT_SECONDS,
                                stdin=subprocess.DEVNULL, env=svn_env(), encoding="utf-8", errors="replace",
                                creationflags=no_window_flags())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"svn mv timed out after {MV_TIMEOUT_SECONDS} s") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot run svn: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"svn mv failed: {result.stderr.strip()}")
    return "svn mv"


# ---- the periodic update ----

def update_paths(config: dict) -> list[str]:
    return list(config["watch"].get("update_paths") or ["AI"])


def status_items(config: dict) -> list[tuple[str, str]]:
    """(the seven status columns, path) of every versioned item with a status in the update paths (local only)."""
    result = _run(config, ["status", "-q", *update_paths(config)], timeout=LOCAL_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(first_error(result.stderr))
    items = []
    for line in result.stdout.splitlines():
        m = STATUS_LINE.match(line)
        if m and m.group(7) != ">":
            items.append(("".join(m.group(i) for i in range(1, 8)), norm_path(m.group(8))))
    return items


def conflicted(config: dict, items: list | None = None) -> list[str]:
    """Paths in the update paths with a text, property or tree conflict (`svn status`, no server contact)."""
    items = status_items(config) if items is None else items
    return [path for cols, path in items if "C" in (cols[0], cols[1], cols[6])]


def modified(config: dict, items: list | None = None) -> list[str]:
    """Paths with local changes (modified, added, deleted, replaced, missing, property changes); not `?`."""
    items = status_items(config) if items is None else items
    return [path for cols, path in items if cols[0] in DIRTY_ITEM or cols[1] == "M"]


def revision(config: dict) -> int | None:
    """The working-copy revision of the first update path (svn info, local), or None."""
    try:
        result = _run(config, ["info", "--show-item", "revision", update_paths(config)[0]],
                      timeout=LOCAL_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = result.stdout.strip()
    return int(text) if result.returncode == 0 and text.isdigit() else None


def svn_update(config: dict, lock_wait: float = 5.0) -> UpdateResult:
    """One update of the update paths, following the rules in the module docstring. Never raises for svn trouble."""
    if not svn_available():
        return UpdateResult("no-vcs", message="svn is not on PATH; the board is not updated automatically")
    paths = update_paths(config)
    missing = [p for p in paths if not os.path.isdir(os.path.join(config["repo_root_path"], p))]
    if missing:
        return UpdateResult("error", message=f"update path not found: {', '.join(missing)}")
    try:
        with write_lock(wait=lock_wait):
            try:
                items = status_items(config)
            except RuntimeError as exc:
                return UpdateResult("error", message=str(exc))
            conflicts = conflicted(config, items)
            if conflicts:
                return UpdateResult("conflict", conflicts=conflicts, revision=revision(config),
                                    message=conflict_message(conflicts))
            dirty = modified(config, items)
            if dirty and not config["watch"].get("update_when_dirty", False):
                return UpdateResult("dirty-skip", dirty=dirty, revision=revision(config),
                                    message=f"waiting for local task edits to be committed ({len(dirty)} "
                                            f"file{'s' if len(dirty) != 1 else ''})")
            before = revision(config)
            result = _run(config, ["update", "--accept", "postpone", *paths])
    except LockBusy:
        return UpdateResult("busy", message="the task files are being written; retrying at the next interval")
    except subprocess.TimeoutExpired:
        return UpdateResult("error", message=f"svn update timed out after {TIMEOUT_SECONDS} s")
    except OSError as exc:
        return UpdateResult("error", message=f"cannot run svn: {exc}")
    if result.returncode != 0:
        message = first_error(result.stderr)
        if "E155004" in message:
            return UpdateResult("busy", message=message)
        return UpdateResult("error", message=message)
    changed, conflicts = [], []
    for line in result.stdout.splitlines():
        m = UPDATE_LINE.match(line)
        if not m or not line[:4].strip():
            continue
        path = norm_path(m.group(5))
        changed.append((line[:4].strip(), path))
        if "C" in (m.group(1), m.group(2), m.group(4)):
            conflicts.append(path)
    after = revision(config)
    try:
        conflicts = sorted(set(conflicts) | set(conflicted(config)))
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        pass
    if conflicts:
        return UpdateResult("conflict", revision=after, revision_before=before, changed=changed,
                            conflicts=conflicts, message=conflict_message(conflicts))
    return UpdateResult("changed" if changed else "ok", revision=after, revision_before=before, changed=changed,
                        message=f"updated to r{after}" if after is not None else "updated")


def conflict_message(paths: list[str]) -> str:
    return (f"conflict in {', '.join(paths)}; updating stopped. Recover: svn revert <file> (takes the incoming "
            "version), redo your change with tb, commit; updating resumes by itself (AI/Methodology.md)")


# ---- the watcher ----

def local_revision(config: dict) -> int | None:
    """The working-copy revision of the first update path (svn info, no server contact); None outside SVN."""
    return revision(config) if svn_available() else None


def update_running(config: dict) -> bool:
    """True while an svn operation (an update by anyone) holds working-copy locks in the update paths."""
    if not svn_available():
        return False
    try:
        return any(cols[2] == "L" for cols, _ in status_items(config))
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return False


# ---- board commits ----

def local_status(config: dict, path: str) -> str:
    """The text status column of one file (`svn status`, local): " " when unmodified."""
    result = _run(config, ["status", path], timeout=LOCAL_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(first_error(result.stderr))
    for line in result.stdout.splitlines():
        m = STATUS_LINE.match(line)
        if m:
            return m.group(1)
    return " "


def commit_error(stderr: str) -> str:
    """
    The reason of a failed `svn commit`: its error lines without svn's wrapper `Commit failed (details follow):`
    (the cause, e.g. out of date, authentication or a hook, follows on later lines), joined; else first_error.
    """
    reasons = []
    for line in stderr.splitlines():
        m = SVN_ERROR.search(line.strip())
        if m and "details follow" not in m.group(2):
            text = f"{m.group(1)} {m.group(2)}".strip()
            if text not in reasons:
                reasons.append(text)
    return "; ".join(reasons) if reasons else first_error(stderr)


def out_of_date(stderr: str) -> bool:
    """True when any line of svn's output carries an out-of-date code (the client's wrapper or the server's)."""
    return any(code in stderr for code in OUT_OF_DATE)


def svn_commit(config: dict, path: str, message: str) -> tuple[str | None, str, bool]:
    """(revision, "", False) on success, else (None, the svn error, whether svn says the file is out of date)."""
    try:
        result = _run(config, ["commit", "-m", message, path], timeout=COMMIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"svn commit timed out after {COMMIT_TIMEOUT_SECONDS} s", False
    except OSError as exc:
        return None, f"cannot run svn: {exc}", False
    if result.returncode != 0:
        return None, commit_error(result.stderr), out_of_date(result.stderr)
    m = COMMITTED.search(result.stdout)
    return (m.group(1) if m else None), "", False


def _svn(config: dict, *args: str) -> str:
    """Run an svn command for the redo; "" on success, else the error."""
    try:
        result = _run(config, list(args))
    except subprocess.TimeoutExpired:
        return f"svn {args[0]} timed out"
    except OSError as exc:
        return f"cannot run svn: {exc}"
    return "" if result.returncode == 0 else first_error(result.stderr)


def svn_commit_paths(config: dict, paths: list[str], message: str) -> tuple:
    """
    `tb done`: `svn add --force --depth infinity <paths>` (new files; versioned ones are skipped), then
    `svn commit -m <message> <paths>`. (revision text, "", "") on success; (None, "", "") when nothing under
    the paths changed; (None, "", the error) when a step failed.
    """
    existing = [p for p in paths if os.path.exists(os.path.join(config["repo_root_path"], p))]
    if not existing:
        return None, "", ""
    try:
        add = _run(config, ["add", "--force", "--depth", "infinity", *existing], timeout=LOCAL_TIMEOUT_SECONDS)
        if add.returncode != 0:
            return None, "", f"svn add failed: {first_error(add.stderr)}"
        status = _run(config, ["status", "-q", *existing], timeout=LOCAL_TIMEOUT_SECONDS)
        if status.returncode != 0:
            return None, "", f"svn status failed: {first_error(status.stderr)}"
        if not any(STATUS_LINE.match(line) for line in status.stdout.splitlines()):
            return None, "", ""
        result = _run(config, ["commit", "-m", message, *existing], timeout=COMMIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, "", f"svn commit timed out after {COMMIT_TIMEOUT_SECONDS} s"
    except OSError as exc:
        return None, "", f"cannot run svn: {exc}"
    if result.returncode != 0:
        return None, "", f"svn commit failed: {commit_error(result.stderr)}"
    m = COMMITTED.search(result.stdout)
    return (m.group(1) if m else "HEAD"), "", ""


class SvnBackend(Backend):
    name = "svn"
    default_main_branch = "trunk"
    review = REVIEW_FIELDS["svn"]
    lock_note = LOCK_NOTE
    update_verb = "svn update"
    commit_by_hand = "svn commit AI"
    supports_redo = True

    def available(self) -> bool:
        return svn_available()

    def env(self) -> dict:
        return svn_env()

    def main_branch_block(self, root_path: str, main_branch: str) -> str | None:
        return main_branch_block(root_path, main_branch)

    def is_versioned(self, path: str) -> bool:
        return _versioned(path)

    def move_file(self, repo_root: str, src: str, dst: str) -> str:
        return svn_mv(repo_root, src, dst)

    def update(self, config: dict, lock_wait: float = 5.0) -> UpdateResult:
        return svn_update(config, lock_wait)

    def revision(self, config: dict):
        return revision(config)

    def first_error(self, stderr: str) -> str:
        return first_error(stderr)

    def conflicted(self, config: dict) -> list[str]:
        return conflicted(config)

    def update_running(self, config: dict) -> bool:
        return update_running(config)

    def local_revision(self, config: dict):
        return local_revision(config)

    def format_revision(self, rev) -> str:
        return "" if rev is None else f"r{rev}"

    def conflict_message(self, paths: list[str]) -> str:
        return conflict_message(paths)

    def conflict_banner(self, conflicts: list[str]) -> str:
        return (f"Update stopped: conflict in {', '.join(conflicts)}, see AI/Methodology.md (revert the file, "
                "redo the change with tb, commit)")

    def update_description(self, config: dict, interval: float) -> str:
        return f"svn update of {', '.join(config['watch']['update_paths'])} every {interval:g} s"

    def commit_file(self, config: dict, path: str, message: str) -> tuple:
        return svn_commit(config, path, message)

    def file_is_clean(self, config: dict, path: str) -> bool:
        return local_status(config, path) == " "

    def revert_file(self, config: dict, path: str) -> str:
        return _svn(config, "revert", path)

    def update_file(self, config: dict, path: str) -> str:
        return _svn(config, "update", "--accept", "postpone", path)

    def commit_note(self, revision: str) -> str:
        return f"committed (r{revision})"

    def commit_revision_pattern(self) -> str:
        return r"\d+"

    def commit_paths(self, config: dict, paths: list[str], message: str, push: bool = True) -> tuple:
        return svn_commit_paths(config, paths, message)
