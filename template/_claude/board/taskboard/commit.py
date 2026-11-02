"""
Board commits: in a coordinator's checkout the web board commits its own answers and notes.

Opt-in per checkout (`local.json` `"board_commits": true`, set by `tb me <dev> --board-commits on`); `tb serve
--no-commit` overrides it, and the supervisor's watcher always starts its board so. With commits on, a board write
runs `write_and_commit()` instead of a plain `store.apply_edit()`:

1. the write itself (apply_edit: same guards; a refused write raises EditError as before);
2. the problems of the written file must be empty (problems of other tasks don't block);
3. the backend commits that one file (`tasks: <id> board input (<dev>)`): svn commits it to the repository, git
   commits it locally (no push; the update then pauses for the unpushed commit until the coordinator pushes);
4. svn only (`supports_redo`): out of date, for a file that had no local edits before this write: revert, update
   that file, redo the same text edit on the incoming version, check, commit once more. A redo the incoming
   version refuses (the question was answered meanwhile, the file moved) leaves the incoming version and reports
   why. A file that already had local edits is committed as a whole, without the redo (reverting would lose them);
5. any other failure leaves the write saved and uncommitted (the coordinator commits it by hand).

Everything runs under the board's write locks (the process lock and the checkout's cross-process lock), so no
periodic update or other write interleaves. Only that one file is ever committed; the board never moves files.
"""

import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from .model import FILE_PATTERN
from .store import apply_edit, process_write_lock, task_file_path, task_write_lock
from .textedit import EditError
from .vcs import backend


@dataclass
class CommitResult:
    """status: committed | unchanged | problems | failed | refused. revision: the backend's revision text of a
    commit (svn: the revision number, git: the short commit id)."""
    status: str
    message: str = ""
    revision: str | None = None


def commit_message(task_id: str, by: str) -> str:
    return f"tasks: {task_id} board input ({by})"


def _apply(config: dict, source: dict, state: str, file_name: str, expected_hash, edit, schema) -> list[str]:
    return apply_edit(source, state, file_name, expected_hash, edit, config["main_branch"], schema,
                      config["repo_root_path"], vcs=config.get("vcs") or "git")


def write_and_commit(config: dict, source: dict, state: str, file_name: str, expected_hash: str | None,
                     edit: Callable[[str], str], by: str, schema: dict | None) -> CommitResult:
    """
    The board's write with commits on (module docstring). Raises EditError like apply_edit when the write itself
    is refused (nothing written); otherwise the write is saved and the result says whether it was committed.
    """
    vcs = backend(config)
    task_id = FILE_PATTERN.match(file_name).group(1)
    message = commit_message(task_id, by)
    with process_write_lock(), task_write_lock():
        path = task_file_path(source["root_path"], state, file_name)
        try:
            clean_before = vcs.file_is_clean(config, path)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            _apply(config, source, state, file_name, expected_hash, edit, schema)
            return CommitResult("failed", f"{vcs.name} status failed ({exc})")
        problems = _apply(config, source, state, file_name, expected_hash, edit, schema)
        if problems:
            return CommitResult("problems", "the file has problems: " + "; ".join(problems))
        try:
            if vcs.file_is_clean(config, path):
                return CommitResult("unchanged")
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass  # the commit below reports it
        revision, error, stale = vcs.commit_file(config, path, message)
        if revision is not None or not error:
            return CommitResult("committed", revision=revision)
        if not stale or not clean_before or not vcs.supports_redo:
            return CommitResult("failed", error)
        # Out of date: redo the same text edit on the incoming version instead of merging (two Log lines appended
        # at the end of one file would conflict).
        failure = vcs.revert_file(config, path)
        if failure:  # nothing was reverted: the board's edit is still in the file
            return CommitResult("failed", f"{error}; then {vcs.name} revert failed: {failure}")
        failure = vcs.update_file(config, path)
        if failure:
            # The revert dropped the board's edit: put it back on the file as it is now, so it is never lost.
            reason = f"{error}; then {vcs.name} update failed: {failure}"
            if not os.path.isfile(path):
                return CommitResult("refused", f"{reason}; the task file is gone, nothing was written")
            try:
                _apply(config, source, state, file_name, None, edit, schema)
            except EditError as exc:
                return CommitResult("refused", f"{reason}; the input could not be written again: {exc}")
            return CommitResult("failed", reason)
        if not os.path.isfile(path):
            return CommitResult("refused", "the task file was moved or deleted meanwhile (it arrives with the next "
                                           "update); nothing was written")
        try:
            problems = _apply(config, source, state, file_name, None, edit, schema)
        except EditError as exc:
            return CommitResult("refused", f"the task changed meanwhile and the input no longer applies: {exc}")
        if problems:
            return CommitResult("problems", "the file has problems: " + "; ".join(problems))
        revision, error, _ = vcs.commit_file(config, path, message)
        if revision is not None or not error:
            return CommitResult("committed", revision=revision)
        return CommitResult("failed", error)
