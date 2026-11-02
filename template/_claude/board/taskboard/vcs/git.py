"""
The git backend: main-branch guard and `git mv` (store), `git fetch` and a fast-forward of the main branch
(update), the watcher's refinements through the reflog and `git diff` (watch), and local board commits (commit).

The periodic update: `git fetch` and a fast-forward of the main branch from its upstream, every
`watch.update_seconds`. git cannot update one folder, so a fast-forward moves the whole checkout;
`watch.update_paths` (default `["AI"]`) are the folders whose local edits pause it. The rules:

- Only on the main branch (`main_branch`, e.g. `master`): on any other branch nothing happens ("off-branch").
- While the update paths hold uncommitted changes (staged or modified; untracked files don't count) nothing is
  fetched or merged unless `watch.update_when_dirty` is true. Unmerged paths (a conflict of a merge or rebase
  the user runs) stop it: the tools never resolve anything.
- `git fetch --no-write-fetch-head --quiet` (no FETCH_HEAD, so a `git pull` of the supervisor at the same time
  can't fail with "Cannot fast-forward to multiple branches"; git older than 2.29 doesn't know the option and
  gets a plain `git fetch --quiet`), then compare the branch with its upstream (`@{u}`): ahead only (unpushed
  commits) pauses like local edits; ahead and behind (diverged) stops it until the user pulls with rebase or
  pushes; behind only runs `git merge --ff-only @{u}`. git refuses a fast-forward that would overwrite
  uncommitted changes (in code or task files); that is reported as a failure, never stashed, reset or forced.
- Nothing can prompt: GIT_TERMINAL_PROMPT=0, no stdin, a timeout. Credentials are never read or stored.
- Only the fast-forward runs under the cross-process write lock (store.write_lock), after the local checks are
  repeated under it, so no task-file write interleaves; the fetch and the checks before it take no lock.

Board commits are local: `git add -- <file>` and `git commit -m <msg> -- <file>`, no push. The update then pauses
("unpushed commits") until the coordinator pushes, as for any own commit.
"""

import os
import shutil
import subprocess

from ..model import FILE_PATTERN
from ..store import LockBusy, write_lock
from ..winproc import no_window_flags
from . import SHORT, REVIEW_FIELDS, Backend, UpdateResult

FETCH_ARGS = ["fetch", "--no-write-fetch-head", "--quiet"]
FETCH_ARGS_OLD_GIT = ["fetch", "--quiet"]  # git before 2.29 has no --no-write-fetch-head
TIMEOUT_SECONDS = 120
LOCAL_TIMEOUT_SECONDS = 60  # git status / rev-parse / rev-list (no server contact)
MV_TIMEOUT_SECONDS = 60
COMMIT_TIMEOUT_SECONDS = 120
WAITING_FOR_PUSH = "waiting for local task edits to be committed and pushed"
DIVERGED = "local and origin diverged; pull --rebase or push manually"
UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
LOCK_NOTE = "git index is locked; remove .git/index.lock if no git command is running"
_FETCH_HEAD_OPTION = [True]  # False once git refused --no-write-fetch-head (too old): stay on the plain fetch
_GIT_DIRS: dict[str, str | None] = {}


class GitError(RuntimeError):
    pass


def git_env() -> dict:
    """The environment of every git call: nothing may prompt (no terminal or credential-manager dialogs) and the
    messages are untranslated; credentials are never read or passed."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_MESSAGES": "C",
            "LANGUAGE": "C"}


def _env() -> dict:
    return git_env()


def _git(cwd: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess | None:
    """A local git command in cwd; None when git is not installed, cwd is missing or it timed out."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=git_env(), encoding="utf-8", errors="replace",
                              creationflags=no_window_flags())
    except (OSError, subprocess.TimeoutExpired):
        return None


def _run(config: dict, args: list[str], timeout: float = TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """A git command in the repo root (the update's runner; tests replace it)."""
    return subprocess.run(["git", *args], cwd=config["repo_root_path"], capture_output=True,
                          text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=_env(),
                          encoding="utf-8", errors="replace", creationflags=no_window_flags())


def _local(config: dict, args: list[str]) -> str:
    """stdout of a local git command; GitError with its first error line when it fails."""
    result = _run(config, args, timeout=LOCAL_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise GitError(first_error(result.stderr))
    return result.stdout


def first_error(stderr: str) -> str:
    """The first `fatal:` / `error:` line of git's stderr (without the prefix), else the first non-empty line."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        for prefix in ("fatal:", "error:"):
            if line.lower().startswith(prefix):
                return line[len(prefix):].strip()
    return lines[0] if lines else "git failed without a message"


def git_available() -> bool:
    return shutil.which("git") is not None


# ---- the guard ----

def git_branch(path: str) -> str | None:
    """
    The branch checked out in the git checkout that holds the folder path (`HEAD` when detached), or None when
    path is in no git checkout or git is not installed. Local only (`git rev-parse`), never contacts a remote.
    """
    result = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if result is None:
        return None
    if result.returncode == 0:
        return result.stdout.strip() or None
    # No commit yet (an unborn branch): rev-parse HEAD fails, the symbolic ref still names the branch.
    unborn = _git(path, "symbolic-ref", "--short", "-q", "HEAD")
    if unborn is not None and unborn.returncode == 0 and unborn.stdout.strip():
        return unborn.stdout.strip()
    return None


def has_tracked_files(path: str) -> bool:
    """True when git tracks at least one file under the folder path (`git ls-files`, local only)."""
    result = _git(path, "ls-files", "--error-unmatch", "--", ".")
    return result is not None and result.returncode == 0


def main_branch_block(root_path: str, main_branch: str) -> str | None:
    """
    Why task files under root_path can't be written now, or None: allowed when the folder's checkout is on
    main_branch (e.g. `master`), when the folder is in no git checkout at all, and when git tracks nothing in it
    (a private, untracked task folder does not change with the branch).
    """
    branch = git_branch(root_path)
    line = main_branch.strip()
    if branch is None or branch == line or not has_tracked_files(root_path):
        return None
    where = "a detached HEAD" if branch == "HEAD" else f"branch '{branch}'"
    return (f"The task folder is in a git checkout of {where}, not of '{line}'; task files change "
            f"on {line} only. Nothing was written.")


def _versioned(path: str) -> bool:
    """True when git tracks path (committed or staged)."""
    result = _git(os.path.dirname(path) or ".", "ls-files", "--error-unmatch", "--", os.path.basename(path))
    return result is not None and result.returncode == 0


def git_mv(src: str, dst: str) -> str:
    """`git mv` (the rename is staged); WriteError text as RuntimeError when it fails."""
    try:
        result = subprocess.run(["git", "mv", "--", src, dst], cwd=os.path.dirname(src),
                                capture_output=True, text=True, timeout=MV_TIMEOUT_SECONDS,
                                stdin=subprocess.DEVNULL, env=git_env(), encoding="utf-8",
                                errors="replace", creationflags=no_window_flags())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git mv timed out after {MV_TIMEOUT_SECONDS} s") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot run git: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"git mv failed: {result.stderr.strip()}")
    return "git mv"


# ---- the periodic update ----

def update_paths(config: dict) -> list[str]:
    return list(config["watch"].get("update_paths") or ["AI"])


def status_items(config: dict) -> list[tuple[str, str]]:
    """(XY status, path) of every tracked file with a change in the update paths (untracked files left out)."""
    out = _local(config, ["status", "--porcelain=v1", "-z", "--untracked-files=no", "--", *update_paths(config)])
    items = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in "RC":
            i += 1  # the rename's source path follows
        items.append((xy, path.replace("\\", "/")))
    return items


def conflicted(config: dict, items: list | None = None) -> list[str]:
    """Unmerged paths in the update paths (a merge or rebase the user runs left conflicts)."""
    items = status_items(config) if items is None else items
    return [path for xy, path in items if xy in UNMERGED]


def modified(config: dict, items: list | None = None) -> list[str]:
    """Paths with uncommitted changes, staged or not (untracked files are not listed by status_items)."""
    items = status_items(config) if items is None else items
    return [path for xy, path in items if xy not in UNMERGED]


def head(config: dict) -> str | None:
    """The full HEAD commit (local), or None (no git, no commit yet, not a checkout)."""
    try:
        result = _run(config, ["rev-parse", "--verify", "-q", "HEAD"], timeout=LOCAL_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = result.stdout.strip()
    return text if result.returncode == 0 and text else None


def revision(config: dict) -> str | None:
    """The short HEAD commit (`a1b2c3d`), or None."""
    full = head(config)
    return full[:SHORT] if full else None


def current_branch(config: dict) -> str:
    """The checked-out branch (`HEAD` when detached); GitError outside a git checkout."""
    return _local(config, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()


def _busy(message: str) -> bool:
    return "index.lock" in message


def git_update(config: dict, lock_wait: float = 5.0) -> UpdateResult:
    """One update following the rules in the module docstring. Never raises for git trouble."""
    if not git_available():
        return UpdateResult("no-vcs", message="git is not on PATH; the board is not updated automatically")
    paths = update_paths(config)
    missing = [p for p in paths if not os.path.isdir(os.path.join(config["repo_root_path"], p))]
    if missing:
        return UpdateResult("error", message=f"update path not found: {', '.join(missing)}")
    main = config["main_branch"]
    try:
        # Everything up to the fast-forward only reads (git fetch writes remote-tracking refs, never files), so
        # it runs without the write lock; the lock is taken only to merge, after looking again under it.
        result, at = _look(config, main)
        if result:
            return result
        try:
            _local(config, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        except GitError:
            return UpdateResult("error", **at, message=f"{main} has no upstream branch (git branch -u origin/{main})")
        fetch = git_fetch(config)
        if fetch.returncode != 0:
            message = first_error(fetch.stderr)
            return UpdateResult("busy" if _busy(message) else "error", **at, message=f"git fetch failed: {message}")
        result = _compare(config, at)
        if result:
            return result
        with write_lock(wait=lock_wait):
            result, at = _look(config, main)  # a tb write may have come in meanwhile
            if result:
                return result
            result = _compare(config, at)
            if result:
                return result
            merge = _run(config, ["merge", "--ff-only", "--quiet", "@{u}"])
            if merge.returncode != 0:
                message = first_error(merge.stderr or merge.stdout)
                return UpdateResult("busy" if _busy(message) else "error", **at,
                                    message=f"fast-forward refused: {message}")
            full_after = head(config)
    except LockBusy:
        return UpdateResult("busy", message="the task files are being written; retrying at the next interval")
    except subprocess.TimeoutExpired as exc:
        return UpdateResult("error", message=f"git {exc.cmd[1] if len(exc.cmd) > 1 else ''} timed out after "
                                             f"{exc.timeout:g} s")
    except GitError as exc:
        message = str(exc)
        return UpdateResult("busy" if _busy(message) else "error", message=message)
    except OSError as exc:
        return UpdateResult("error", message=f"cannot run git: {exc}")
    before, after = at["revision"], full_after[:SHORT] if full_after else None
    changed = []
    if at["head"]:
        try:
            diff = _run(config, ["diff", "--name-status", "--no-renames", at["head"], "HEAD"],
                        timeout=LOCAL_TIMEOUT_SECONDS)
            for line in diff.stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    changed.append((parts[0], parts[1].replace("\\", "/")))
        except (OSError, subprocess.TimeoutExpired):
            pass
    return UpdateResult("changed", revision=after, revision_before=before, head=full_after, changed=changed,
                        message=f"updated {before} -> {after}")


def git_fetch(config: dict) -> subprocess.CompletedProcess:
    """`git fetch` without writing FETCH_HEAD; silently the plain fetch when this git doesn't know the option."""
    if _FETCH_HEAD_OPTION[0]:
        fetch = _run(config, FETCH_ARGS)
        if fetch.returncode == 0 or "unknown option" not in fetch.stderr.lower():
            return fetch
        _FETCH_HEAD_OPTION[0] = False
    return _run(config, FETCH_ARGS_OLD_GIT)


def _look(config: dict, main: str) -> tuple[UpdateResult | None, dict]:
    """The local checks (branch, unmerged and uncommitted paths): (a result that ends the attempt or None, HEAD)."""
    try:
        branch = current_branch(config)
    except GitError as exc:
        return UpdateResult("no-vcs", message=f"not a git checkout ({exc}); the board is not updated "
                                              "automatically"), {}
    if branch != main:
        where = "a detached HEAD" if branch == "HEAD" else f"branch '{branch}'"
        return UpdateResult("off-branch", revision=revision(config),
                            message=f"the checkout is on {where}, not '{main}'; updating resumes on {main}"), {}
    full = head(config)
    at = {"revision": full[:SHORT] if full else None, "head": full}
    items = status_items(config)
    conflicts = conflicted(config, items)
    if conflicts:
        return UpdateResult("conflict", conflicts=conflicts, message=conflict_message(conflicts), **at), at
    dirty = modified(config, items)
    if dirty and not config["watch"].get("update_when_dirty", False):
        return UpdateResult("dirty-skip", dirty=dirty, **at,
                            message=f"{WAITING_FOR_PUSH} ({len(dirty)} file{'s' if len(dirty) != 1 else ''})"), at
    return None, at


def _compare(config: dict, at: dict) -> UpdateResult | None:
    """The branch against its fetched upstream: a result unless it is only behind (then None: fast-forward)."""
    ahead, behind = (int(n) for n in _local(config, ["rev-list", "--left-right", "--count", "HEAD...@{u}"]).split())
    if ahead and behind:
        return UpdateResult("diverged", message=DIVERGED, **at)
    if ahead:
        return UpdateResult("dirty-skip", **at,
                            message=f"{WAITING_FOR_PUSH} ({ahead} unpushed commit{'s' if ahead != 1 else ''})")
    if not behind:
        return UpdateResult("ok", revision_before=at["revision"], message=f"up to date at {at['revision']}", **at)
    return None


def conflict_message(paths: list[str]) -> str:
    return (f"unmerged paths {', '.join(paths)}; updating stopped. Finish or abort the merge/rebase with git "
            "(the tools never resolve anything); updating resumes by itself (AI/Methodology.md)")


# ---- the watcher ----

def local_revision(config: dict) -> str | None:
    """
    The full HEAD commit while the checkout is on the main branch (git rev-parse, no server contact); None off
    the main branch (a branch switch is no incoming input), outside git or without git.
    """
    if not git_available() or git_branch(config["repo_root_path"]) != config["main_branch"]:
        return None
    return head(config)


def own_commits_only(config: dict, old: str, new: str) -> bool:
    """
    True when HEAD went from old to new only through commits made in this checkout (the supervisor's own task
    commits): every reflog entry since old is a `commit` (git reflog, local). A fast-forward, pull, rebase or
    reset brings other people's commits, and anything unclear (old not in the reflog, git failing) is False.
    """
    try:
        result = subprocess.run(["git", "reflog", "show", "-n", "100", "--format=%H%x09%gs", "HEAD"],
                                cwd=config["repo_root_path"], capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL, env=git_env(), encoding="utf-8", errors="replace",
                                creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    moves = []
    for line in result.stdout.splitlines():
        sha, _, subject = line.partition("\t")
        if sha == old:
            return bool(moves) and moves[0][0] == new and all(
                s.startswith("commit") and not s.startswith("commit (merge)") for _, s in moves)
        moves.append((sha, subject))
    return False


def changed_tasks(config: dict, old: str, new: str) -> set[tuple[str, str]] | None:
    """
    (source name, task id) of every task file that differs between the commits old and new (`diff --name-only
    --no-renames`, local, restricted to the sources' folders; a moved task lists both paths). None when that
    can't be told (the command failing): the caller then keeps every key. A source outside the repository (or on
    another drive) is skipped: a HEAD move can't bring its files.
    """
    found: set[tuple[str, str]] = set()
    for source in config["sources"]:
        try:
            folder = os.path.relpath(source["root_path"], config["repo_root_path"]).replace("\\", "/")
        except ValueError:  # another drive (Windows)
            continue
        if folder == ".." or folder.startswith("../") or os.path.isabs(folder):
            continue
        try:
            result = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", str(old), str(new), "--",
                                     folder], cwd=config["repo_root_path"], capture_output=True, text=True,
                                    timeout=30, stdin=subprocess.DEVNULL, env=git_env(), encoding="utf-8",
                                    errors="replace", creationflags=no_window_flags())
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        for path in result.stdout.split("\0"):
            m = FILE_PATTERN.match(path.rsplit("/", 1)[-1])
            if m:
                found.add((source["name"], m.group(1)))
    return found


def git_dir(config: dict) -> str | None:
    """The checkout's git directory (`.git`, or the worktree's), looked up once per repo root."""
    root = config["repo_root_path"]
    if root not in _GIT_DIRS:
        try:
            result = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=root, capture_output=True,
                                    text=True, timeout=30, stdin=subprocess.DEVNULL, env=git_env(),
                                    encoding="utf-8", errors="replace", creationflags=no_window_flags())
        except (OSError, subprocess.SubprocessError):
            return None
        _GIT_DIRS[root] = result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
    return _GIT_DIRS[root]


def update_running(config: dict) -> bool:
    """True while a git command (a merge or pull by anyone) holds the index lock (`.git/index.lock`)."""
    if not git_available():
        return False
    directory = git_dir(config)
    return bool(directory) and os.path.exists(os.path.join(directory, "index.lock"))


# ---- board commits ----

def local_status(config: dict, path: str) -> str:
    """The XY status of one file (`git status --porcelain`, local): "" when unmodified."""
    result = _run(config, ["status", "--porcelain=v1", "--untracked-files=all", "--", path],
                  timeout=LOCAL_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(first_error(result.stderr))
    lines = [line for line in result.stdout.splitlines() if len(line) > 3]
    return lines[0][:2] if lines else ""


def git_commit(config: dict, path: str, message: str) -> tuple[str | None, str, bool]:
    """`git add` and `git commit` of one file: (short commit id, "", False), else (None, the error, False)."""
    try:
        add = _run(config, ["add", "--", path], timeout=LOCAL_TIMEOUT_SECONDS)
        if add.returncode != 0:
            return None, f"git add failed: {first_error(add.stderr)}", False
        result = _run(config, ["commit", "--quiet", "-m", message, "--", path], timeout=COMMIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"git commit timed out after {COMMIT_TIMEOUT_SECONDS} s", False
    except OSError as exc:
        return None, f"cannot run git: {exc}", False
    if result.returncode != 0:
        return None, f"git commit failed: {first_error(result.stderr or result.stdout)}", False
    sha = head(config)
    return (sha[:SHORT] if sha else None), "", False


class GitBackend(Backend):
    name = "git"
    default_main_branch = "master"
    review = REVIEW_FIELDS["git"]
    lock_note = LOCK_NOTE
    update_verb = "git pull"
    commit_by_hand = "git commit -- AI, then git push"
    supports_redo = False

    def available(self) -> bool:
        return git_available()

    def env(self) -> dict:
        return git_env()

    def main_branch_block(self, root_path: str, main_branch: str) -> str | None:
        return main_branch_block(root_path, main_branch)

    def is_versioned(self, path: str) -> bool:
        return _versioned(path)

    def move_file(self, repo_root: str, src: str, dst: str) -> str:
        return git_mv(src, dst)

    def update(self, config: dict, lock_wait: float = 5.0) -> UpdateResult:
        return git_update(config, lock_wait)

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

    def own_commits_only(self, config: dict, old, new) -> bool:
        return own_commits_only(config, old, new)

    def changed_tasks(self, config: dict, old, new):
        return changed_tasks(config, old, new)

    def format_revision(self, rev) -> str:
        return "" if rev is None else str(rev)[:SHORT]

    def conflict_message(self, paths: list[str]) -> str:
        return conflict_message(paths)

    def conflict_banner(self, conflicts: list[str]) -> str:
        return (f"Update stopped: unmerged paths {', '.join(conflicts)}, finish or abort the merge/rebase "
                "with git")

    def update_description(self, config: dict, interval: float) -> str:
        paths = ", ".join(config["watch"]["update_paths"])
        return (f"git fetch + fast-forward of {config['main_branch']} every {interval:g} s, paused while {paths} "
                "has uncommitted or unpushed edits")

    def commit_file(self, config: dict, path: str, message: str) -> tuple:
        return git_commit(config, path, message)

    def file_is_clean(self, config: dict, path: str) -> bool:
        return local_status(config, path) == ""

    def revert_file(self, config: dict, path: str) -> str:
        result = _run(config, ["checkout", "HEAD", "--", path], timeout=LOCAL_TIMEOUT_SECONDS)
        return "" if result.returncode == 0 else first_error(result.stderr)

    def update_file(self, config: dict, path: str) -> str:
        return "git does not update single files; pull the checkout"

    def commit_note(self, revision: str) -> str:
        return f"committed locally ({revision}); push with git push"

    def commit_revision_pattern(self) -> str:
        return r"[0-9a-f]{7,40}"
