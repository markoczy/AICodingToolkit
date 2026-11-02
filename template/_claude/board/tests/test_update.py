"""
Tests for the periodic update (taskboard/update.py with the git and svn backends, taskboard/vcs), the
cross-process write lock (store.write_lock), the watcher's INCOMING report and exit 3 (watch.py), and the board's
update thread and /api/update (cli serve, web).

Everything that runs git uses throw-away repositories in tmp_path: a bare `origin` and two clones of `master`,
dev_a (the board's) and dev_b (another dev), each with its own user.name/email (`repos`). Everything that runs
svn uses `svnadmin create` in tmp_path, a file:// URL and two trunk checkouts (`svn_repos`). The fixtures assert
that the config and the checkout point there, so no test ever contacts a real repository. Failures (offline,
authentication, timeout) are simulated by replacing the backend's runner.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from taskboard import cli, store, update, watch
from taskboard.config import BoardConfigError, load_board_config, validate_board_config
from taskboard.model import STATES
from taskboard.update import UpdateResult, Updater, run_update
from taskboard.vcs import git as gitvcs
from taskboard.vcs import svn as svnvcs
from taskboard.vcs.git import git_update
from taskboard.vcs.svn import svn_update
from taskboard.web.app import create_app

BOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SV_PY = os.path.join(os.path.dirname(BOARD_DIR), "svn", "sv.py")
HAVE_GIT = shutil.which("git") is not None
HAVE_SVN = shutil.which("svn") is not None and shutil.which("svnadmin") is not None
needs_git = pytest.mark.skipif(not HAVE_GIT, reason="git not installed")
needs_svn = pytest.mark.skipif(not HAVE_SVN, reason="svn not installed")
BASE = "http://127.0.0.1:5070"
T001 = "AI/Tasks/00-Conception/T001-task.md"


def git(cwd, *args, check=True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=gitvcs.git_env(),
                            stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def local_identity(wc, name):
    """Repository-local settings only: never the user's global config."""
    for key, value in (("user.name", name), ("user.email", f"{name}@example.invalid"), ("core.autocrlf", "false"),
                       ("commit.gpgsign", "false"), ("pull.rebase", "false")):
        git(wc, "config", key, value)


def board_json(wc):
    return wc / ".claude" / "board" / "board.json"


def tb(wc, *argv) -> int:
    return cli.main(["--config", str(board_json(wc)), *argv])


def task_path(wc, task_id="T001"):
    found = list((wc / "AI" / "Tasks").glob(f"*/{task_id}-*.md"))
    assert len(found) == 1, found
    return found[0]


def commit(wc, message="change", push=True):
    git(wc, "add", "-A", "AI")
    git(wc, "commit", "-q", "-m", message)
    if push:
        git(wc, "push", "-q")


def head(wc) -> str:
    return git(wc, "rev-parse", "HEAD").strip()


def config_of(wc, **watch_keys):
    if watch_keys:
        data = json.loads(board_json(wc).read_text(encoding="utf-8"))
        data["watch"] = {**data.get("watch", {}), **watch_keys}
        board_json(wc).write_text(json.dumps(data), encoding="utf-8")
        if (wc / ".git").exists():
            git(wc, "update-index", "--assume-unchanged", ".claude/board/board.json")
    return load_board_config(str(board_json(wc)))


@pytest.fixture
def repos(tmp_path):
    """A bare origin and two master clones laid out like this repo (AI/Tasks, code in src)."""
    if not HAVE_GIT:
        pytest.skip("git not installed")
    server = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "master", str(server))
    a, b = tmp_path / "dev_a", tmp_path / "dev_b"
    git(tmp_path, "init", "-q", "-b", "master", str(a))
    local_identity(a, "dev-a")
    (a / ".claude" / "board").mkdir(parents=True)
    (a / "AI" / "Plans").mkdir(parents=True)
    (a / "AI" / "Plans" / ".gitkeep").write_text("", encoding="utf-8")
    for state in STATES:
        (a / "AI" / "Tasks" / state).mkdir(parents=True)
        (a / "AI" / "Tasks" / state / ".gitkeep").write_text("", encoding="utf-8")  # git keeps no empty folder
    (a / "src").mkdir()
    (a / "src" / "code.txt").write_text("v1\n", encoding="utf-8")
    (a / "src" / "other.txt").write_text("o1\n", encoding="utf-8")
    config = {"main_branch": "master", "people": ["Alex", "Robin"],
              "watch": {"quiet_seconds": 1, "poll_seconds": 1, "update_seconds": 10},
              "sources": [{"name": "tasks", "root": "AI/Tasks", "prefix": "T", "editable": True}]}
    board_json(a).write_text(json.dumps(config), encoding="utf-8")
    git(a, "add", "-A")
    git(a, "commit", "-q", "-m", "init")
    git(a, "remote", "add", "origin", str(server))
    git(a, "push", "-q", "-u", "origin", "master")
    git(tmp_path, "clone", "-q", str(server), str(b))
    local_identity(b, "dev-b")
    config_a = load_board_config(str(board_json(a)))
    # Never touch a real repository: the board's checkout and its origin are inside tmp_path.
    assert config_a["repo_root_path"] == str(a)
    assert git(a, "remote", "get-url", "origin").strip() == str(server)
    assert os.path.realpath(git(a, "rev-parse", "--show-toplevel").strip()) == os.path.realpath(a)
    return SimpleNamespace(a=a, b=b, server=server, config=config_a, tmp=tmp_path)


def new_task(wc, title="A task", by="supervisor"):
    assert tb(wc, "new", "--title", title, "--type", "bug", "--priority", "low", "--by", by) == 0


# ---- svn: a throw-away repository with two trunk checkouts ----

def svn(cwd, *args) -> str:
    return subprocess.run(["svn", "--non-interactive", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def svn_commit_all(wc, message="change"):
    svn(wc, "add", "--force", "--depth", "infinity", "AI")
    svn(wc, "commit", "-m", message)


def svn_revision(wc) -> int:
    return int(svn(wc, "info", "--show-item", "revision", "AI").strip())


@pytest.fixture
def svn_repos(tmp_path):
    """A throw-away repository with two trunk checkouts laid out like `repos` (AI/Tasks, code in src)."""
    if not HAVE_SVN:
        pytest.skip("svn not installed")
    server = tmp_path / "server"
    subprocess.run(["svnadmin", "create", str(server)], check=True, capture_output=True)
    url = server.as_uri()
    svn(tmp_path, "mkdir", "-m", "layout", f"{url}/trunk", f"{url}/branches")
    a, b = tmp_path / "dev_a", tmp_path / "dev_b"
    svn(tmp_path, "checkout", f"{url}/trunk", str(a))
    (a / ".claude" / "board").mkdir(parents=True)
    (a / "AI" / "Plans").mkdir(parents=True)
    for state in STATES:
        (a / "AI" / "Tasks" / state).mkdir(parents=True)
    (a / "src").mkdir()
    (a / "src" / "code.txt").write_text("v1\n", encoding="utf-8")
    config = {"vcs": "svn", "people": ["Alex", "Robin"],
              "watch": {"quiet_seconds": 1, "poll_seconds": 1, "update_seconds": 10},
              "sources": [{"name": "tasks", "root": "AI/Tasks", "prefix": "T", "editable": True}]}
    board_json(a).write_text(json.dumps(config), encoding="utf-8")
    svn(a, "add", "--force", ".")
    svn(a, "commit", "-m", "init")
    svn(tmp_path, "checkout", f"{url}/trunk", str(b))
    config_a = load_board_config(str(board_json(a)))
    # Never touch a real repository: the board's checkout and its repository are inside tmp_path.
    assert config_a["repo_root_path"] == str(a) and config_a["main_branch"] == "trunk"
    assert svn(a, "info", "--show-item", "repos-root-url").strip() == url
    assert url.startswith(tmp_path.as_uri())
    return SimpleNamespace(a=a, b=b, url=url, config=config_a, tmp=tmp_path)


# ---- git_update against throw-away repositories ----

@needs_git
class TestGitUpdate:
    def test_new_task_arrives_and_a_second_update_is_ok(self, repos):
        new_task(repos.b, "From b")
        commit(repos.b)
        before = head(repos.a)
        result = git_update(repos.config)
        assert result.status == "changed"
        assert ("A", "AI/Tasks/00-Conception/T001-from-b.md") in result.changed
        assert result.revision_before == before[:7] and result.revision == head(repos.a)[:7] != before[:7]
        assert result.head == head(repos.a) and result.message == f"updated {before[:7]} -> {result.revision}"
        assert task_path(repos.a).exists()
        again = git_update(repos.config)
        assert again.status == "ok" and again.changed == [] and again.revision == result.revision

    def test_the_whole_checkout_fast_forwards_and_keeps_uncommitted_code_edits(self, repos):
        (repos.a / "src" / "code.txt").write_text("local edit\n", encoding="utf-8")  # not in the update paths
        (repos.b / "src" / "other.txt").write_text("o2\n", encoding="utf-8")
        new_task(repos.b)
        git(repos.b, "add", "-A")
        commit(repos.b, "code and a task")
        result = git_update(repos.config)
        assert result.status == "changed" and ("M", "src/other.txt") in result.changed
        assert (repos.a / "src" / "other.txt").read_text(encoding="utf-8") == "o2\n"
        assert (repos.a / "src" / "code.txt").read_text(encoding="utf-8") == "local edit\n"
        assert task_path(repos.a).exists()

    def test_a_colliding_code_edit_is_an_error_and_nothing_is_forced(self, repos):
        (repos.a / "src" / "code.txt").write_text("local edit\n", encoding="utf-8")
        (repos.b / "src" / "code.txt").write_text("v2\n", encoding="utf-8")
        git(repos.b, "commit", "-q", "-am", "code")
        git(repos.b, "push", "-q")
        before = head(repos.a)
        result = git_update(repos.config)
        assert result.status == "error" and result.message.startswith("fast-forward refused: ")
        assert "overwritten" in result.message
        assert head(repos.a) == before
        assert (repos.a / "src" / "code.txt").read_text(encoding="utf-8") == "local edit\n"
        assert git(repos.a, "stash", "list") == ""
        (repos.a / "src" / "code.txt").write_text("v1\n", encoding="utf-8")
        assert git_update(repos.config).status == "changed"

    def shared_task(self, repos):
        new_task(repos.a)
        commit(repos.a)
        git(repos.b, "pull", "-q", "--ff-only")

    def test_local_edits_pause_the_update(self, repos):
        self.shared_task(repos)
        assert tb(repos.b, "log", "T001", "from b") == 0
        commit(repos.b)
        assert tb(repos.a, "log", "T001", "from a") == 0
        before = task_path(repos.a).read_bytes()
        result = git_update(repos.config)
        assert result.status == "dirty-skip" and result.dirty == [T001]
        assert result.message == "waiting for local task edits to be committed and pushed (1 file)"
        assert task_path(repos.a).read_bytes() == before
        # Staged counts as well.
        git(repos.a, "add", T001)
        assert git_update(repos.config).status == "dirty-skip"
        # Dropping the edit (here; normally: commit, pull, push) ends the pause.
        git(repos.a, "checkout", "HEAD", "--", T001)
        assert git_update(repos.config).status == "changed"
        assert "from b" in task_path(repos.a).read_text(encoding="utf-8")

    def test_untracked_files_are_not_dirty_but_added_ones_are(self, repos):
        (repos.a / "AI" / "Tasks" / "00-Conception" / "notes.txt").write_text("x", encoding="utf-8")
        new_task(repos.b, "From b")
        commit(repos.b)
        assert git_update(repos.config).status == "changed"
        git(repos.a, "add", "AI/Tasks/00-Conception/notes.txt")
        result = git_update(repos.config)
        assert result.status == "dirty-skip" and result.dirty == ["AI/Tasks/00-Conception/notes.txt"]

    def test_unpushed_commits_pause_until_pushed(self, repos):
        new_task(repos.a, "Mine")
        commit(repos.a, push=False)
        result = git_update(repos.config)
        assert result.status == "dirty-skip" and result.head == head(repos.a)
        assert result.message == "waiting for local task edits to be committed and pushed (1 unpushed commit)"
        git(repos.a, "push", "-q")
        assert git_update(repos.config).status == "ok"

    def test_diverged_stops_until_the_user_rebases_and_pushes(self, repos):
        self.shared_task(repos)
        new_task(repos.a, "Local")
        commit(repos.a, "local", push=False)
        new_task(repos.b, "Remote")
        commit(repos.b, "remote")
        before = head(repos.a)
        result = git_update(repos.config)
        assert result.status == "diverged" and result.message == gitvcs.DIVERGED
        assert head(repos.a) == before and git_update(repos.config).status == "diverged"
        assert Updater.describe(result) == "update stopped: local and origin diverged; pull --rebase or push manually"
        # The tools never resolve anything; here the two tasks got the same id (T002), so the user sorts it out.
        git(repos.a, "reset", "-q", "--hard", "@{u}")
        assert git_update(repos.config).status == "ok"

    def test_off_the_main_branch_nothing_is_updated(self, repos):
        git(repos.a, "checkout", "-q", "-b", "t001-feature")
        new_task(repos.b, "From b")
        commit(repos.b)
        result = git_update(repos.config)
        assert result.status == "off-branch" and result.head is None
        assert "'t001-feature', not 'master'" in result.message
        assert not list((repos.a / "AI" / "Tasks").glob("*/T001-*.md"))
        assert Updater.describe(result).startswith("update off: the checkout is on branch 't001-feature'")
        git(repos.a, "checkout", "-q", "master")
        assert git_update(repos.config).status == "changed"

    def test_unmerged_paths_stop_it_until_the_merge_is_aborted(self, repos):
        self.shared_task(repos)
        git(repos.a, "checkout", "-q", "-b", "side")
        assert tb(repos.a, "log", "T001", "on side", "--by", "x") != 0  # tb refuses off master
        text = task_path(repos.a).read_text(encoding="utf-8")
        task_path(repos.a).write_bytes((text + "- 2026-10-20 dev: side line.\n").encode())
        commit(repos.a, "side", push=False)
        git(repos.a, "checkout", "-q", "master")
        task_path(repos.a).write_bytes((text + "- 2026-10-20 dev: master line.\n").encode())
        commit(repos.a, "master", push=False)
        git(repos.a, "merge", "side", check=False)
        result = git_update(repos.config)
        assert result.status == "conflict" and result.conflicts == [T001]
        assert "unmerged paths" in result.message and "never resolve" in result.message
        git(repos.a, "merge", "--abort")
        assert git_update(repos.config).status == "dirty-skip"  # the local commit is not pushed yet

    def test_update_when_dirty_fast_forwards_past_local_task_edits(self, repos):
        self.shared_task(repos)
        config = config_of(repos.a, update_when_dirty=True)
        new_task(repos.b, "Second")
        commit(repos.b)
        assert tb(repos.a, "log", "T001", "local line") == 0
        result = git_update(config)
        assert result.status == "changed" and task_path(repos.a, "T002").exists()
        assert "local line" in task_path(repos.a).read_text(encoding="utf-8")
        # A clash with the local edit: git refuses, nothing is forced.
        assert tb(repos.b, "log", "T001", "from b") == 0
        commit(repos.b)
        local = task_path(repos.a).read_bytes()
        result = git_update(config)
        assert result.status == "error" and "fast-forward refused" in result.message
        assert task_path(repos.a).read_bytes() == local

    def test_no_upstream_is_an_error(self, repos):
        git(repos.a, "branch", "--unset-upstream")
        result = git_update(repos.config)
        assert result.status == "error" and "master has no upstream branch" in result.message

    def test_not_a_git_checkout_disables_updating(self, tmp_path):
        (tmp_path / ".claude" / "board").mkdir(parents=True)
        (tmp_path / "AI" / "Tasks").mkdir(parents=True)
        board_json(tmp_path).write_text(json.dumps({"sources": [{"name": "t", "root": "AI/Tasks"}]}),
                                        encoding="utf-8")
        config = load_board_config(str(board_json(tmp_path)))
        if git(tmp_path, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true":
            pytest.skip("the temp folder is inside a git checkout")
        result = git_update(config)
        assert result.status == "no-vcs" and "not a git checkout" in result.message

    def test_a_git_index_lock_is_busy(self, repos):
        new_task(repos.b, "From b")
        commit(repos.b)
        lock = repos.a / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        try:
            assert git_update(repos.config).status == "busy"
            assert watch.update_running(repos.config) is True
        finally:
            lock.unlink()
        assert watch.update_running(repos.config) is False
        assert git_update(repos.config).status == "changed"

    def test_held_write_lock_skips_the_fast_forward(self, repos):
        new_task(repos.b, "From b")
        commit(repos.b)
        os.makedirs(os.path.dirname(store.LOCK_PATH), exist_ok=True)
        done = threading.Event()
        taken = threading.Event()

        def hold():
            with store.write_lock():
                taken.set()
                done.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        taken.wait(5)
        try:
            assert git_update(repos.config, lock_wait=0.2).status == "busy"
        finally:
            done.set()
            t.join()
        assert not list((repos.a / "AI" / "Tasks").glob("*/T001-*.md"))
        assert git_update(repos.config).status == "changed"


# ---- svn_update against a throw-away repository ----

@needs_svn
class TestSvnUpdate:
    def test_new_task_arrives_and_a_second_update_is_ok(self, svn_repos):
        repos = svn_repos
        new_task(repos.b, "From b")
        svn_commit_all(repos.b)
        result = svn_update(repos.config)
        assert result.status == "changed"
        assert ("A", "AI/Tasks/00-Conception/T001-from-b.md") in result.changed
        assert result.revision == result.revision_before + 1
        assert task_path(repos.a).exists()
        again = svn_update(repos.config)
        assert again.status == "ok" and again.changed == []
        assert run_update(repos.config).status == "ok"  # the dispatcher picks the svn backend

    def test_code_folders_are_never_updated(self, svn_repos):
        repos = svn_repos
        (repos.b / "src" / "code.txt").write_text("v2\n", encoding="utf-8")
        (repos.b / "src" / "new.txt").write_text("x\n", encoding="utf-8")
        svn(repos.b, "add", "src/new.txt")
        new_task(repos.b)
        svn(repos.b, "add", "--force", "AI")
        svn(repos.b, "commit", "-m", "code and a task")
        assert svn_update(repos.config).status == "changed"
        assert (repos.a / "src" / "code.txt").read_text(encoding="utf-8") == "v1\n"
        assert not (repos.a / "src" / "new.txt").exists()
        assert task_path(repos.a).exists()

    def shared_task(self, repos):
        new_task(repos.a)
        svn_commit_all(repos.a)
        svn(repos.b, "update")

    def test_local_edits_pause_the_update(self, svn_repos):
        repos = svn_repos
        self.shared_task(repos)
        assert tb(repos.b, "log", "T001", "from b") == 0
        svn_commit_all(repos.b)
        assert tb(repos.a, "log", "T001", "from a") == 0
        before = task_path(repos.a).read_bytes()
        result = svn_update(repos.config)
        assert result.status == "dirty-skip"
        assert result.message == "waiting for local task edits to be committed (1 file)"
        assert task_path(repos.a).read_bytes() == before
        assert update.conflicted(repos.config) == []
        # Committing the edit ends the pause: the next update runs (svn refuses the out-of-date commit first,
        # so the dev updates by hand; here the edit is reverted instead).
        svn(repos.a, "revert", str(task_path(repos.a)))
        assert svn_update(repos.config).status == "changed"
        assert "from b" in task_path(repos.a).read_text(encoding="utf-8")

    def test_dirty_update_merges_or_stops_on_a_conflict_until_reverted(self, svn_repos):
        repos = svn_repos
        self.shared_task(repos)
        config = config_of(repos.a, update_when_dirty=True)
        # A clean merge: b rewrites Problem (by hand, no Log line), a appends to the Log.
        b_text = task_path(repos.b).read_text(encoding="utf-8")
        task_path(repos.b).write_bytes(b_text.replace("## Problem\n", "## Problem\nRewritten by b.\n").encode())
        svn_commit_all(repos.b)
        text = task_path(repos.a).read_text(encoding="utf-8")
        task_path(repos.a).write_bytes((text + "- 2026-10-20 supervisor: local line.\n").encode())
        assert svn_update(config).status == "changed"
        merged = task_path(repos.a).read_text(encoding="utf-8")
        assert "Rewritten by b." in merged and "local line" in merged
        # A clash: both append to the Log tail.
        svn(repos.a, "revert", str(task_path(repos.a)))
        assert tb(repos.b, "log", "T001", "from b") == 0
        svn_commit_all(repos.b)
        assert tb(repos.a, "log", "T001", "from a") == 0
        result = svn_update(config)
        assert result.status == "conflict"
        assert result.conflicts == [T001]
        assert "updating stopped" in result.message and "svn revert" in result.message
        # Nothing is updated while the conflict exists.
        new_task(repos.b, "Second")
        svn_commit_all(repos.b)
        again = svn_update(config)
        assert again.status == "conflict" and again.changed == []
        assert not list((repos.a / "AI" / "Tasks").glob("*/T002-*.md"))
        # Reverting the file (the incoming version) resumes updating by itself.
        svn(repos.a, "revert", str(task_path(repos.a)))
        resumed = svn_update(config)
        assert resumed.status == "changed"
        assert task_path(repos.a, "T002").exists()

    def test_tree_conflict_is_detected(self, svn_repos):
        repos = svn_repos
        self.shared_task(repos)
        config = config_of(repos.a, update_when_dirty=True)
        svn(repos.b, "mv", T001, "AI/Tasks/01-Refinement/T001-task.md")
        svn(repos.b, "commit", "-m", "b moves")
        svn(repos.a, "mv", T001, "AI/Tasks/08-Dropped/T001-task.md")
        result = svn_update(config)
        assert result.status == "conflict"
        assert T001 in result.conflicts
        assert svn_update(config).status == "conflict"

    def test_unversioned_files_are_not_dirty_but_added_ones_are(self, svn_repos):
        repos = svn_repos
        (repos.a / "AI" / "Tasks" / "00-Conception" / "notes.txt").write_text("x", encoding="utf-8")
        new_task(repos.b, "From b")
        svn_commit_all(repos.b)
        assert svn_update(repos.config).status == "changed"
        svn(repos.a, "add", "AI/Tasks/00-Conception/notes.txt")
        result = svn_update(repos.config)
        assert result.status == "dirty-skip" and result.dirty == ["AI/Tasks/00-Conception/notes.txt"]

    def test_svn_working_copy_lock_is_busy(self, svn_repos, monkeypatch):
        def fake(config, args, timeout=0):
            if args[0] == "update":
                return subprocess.CompletedProcess(args, 1, "", "svn: E155004: Working copy is locked\n")
            return real(config, args, timeout)
        real = svnvcs._run
        monkeypatch.setattr(svnvcs, "_run", fake)
        assert svn_update(svn_repos.config).status == "busy"

    def test_held_write_lock_skips_the_cycle(self, svn_repos):
        os.makedirs(os.path.dirname(store.LOCK_PATH), exist_ok=True)
        done = threading.Event()
        taken = threading.Event()

        def hold():
            with store.write_lock():
                taken.set()
                done.wait(5)
        t = threading.Thread(target=hold)
        t.start()
        taken.wait(5)
        try:
            assert svn_update(svn_repos.config, lock_wait=0.2).status == "busy"
        finally:
            done.set()
            t.join()
        assert svn_update(svn_repos.config).status == "ok"

    def test_the_svn_tool_lands_a_branch_next_to_the_task_only_update(self, svn_repos, monkeypatch):
        """The periodic update leaves a mixed-revision working copy (AI newer than src); sv sync and sv land
        (the svn workflow tool next to the board) equalize it."""
        if not os.path.isfile(SV_PY):
            pytest.skip("the svn workflow tool is not next to the board")
        repos = svn_repos
        sys.path.insert(0, os.path.dirname(SV_PY))
        try:
            import sv
        finally:
            sys.path.remove(os.path.dirname(SV_PY))
        monkeypatch.setattr(sv, "REPO_ROOT", str(repos.a))
        assert sv.repo_url() == repos.url
        new_task(repos.b, "From b")
        svn_commit_all(repos.b)
        (repos.b / "src" / "code.txt").write_text("v2\n", encoding="utf-8")
        svn(repos.b, "commit", "-m", "code")
        assert svn_update(repos.config).status == "changed"  # AI is now newer than src: mixed revision
        sv.branch_create("t009-x", None)
        wt = sv.wt_add("agent-x", branch="t009-x")
        with open(os.path.join(wt, "src", "feature.txt"), "w", encoding="utf-8") as f:
            f.write("f\n")
        svn(wt, "add", "src/feature.txt")
        sv.commit(wt, "T009: feature")
        sv.sync(wt)
        sv.commit(wt, "T009: sync")
        assert sv.land("t009-x", "T009: feature").startswith("r")
        assert (repos.a / "src" / "feature.txt").exists()
        assert (repos.a / "src" / "code.txt").read_text(encoding="utf-8") == "v2\n"
        assert svn_update(repos.config).status == "ok"


# ---- failures, reporting and back-off (git runner replaced) ----

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def failing_runner(monkeypatch, stderr=None, hang=False, branch="master"):
    calls = []

    def fake(config, args, timeout=0):
        calls.append(args[0])
        if args[0] == "rev-parse":
            if "@{u}" in args:
                return subprocess.CompletedProcess(args, 0, "origin/master\n", "")
            if "--abbrev-ref" in args:
                return subprocess.CompletedProcess(args, 0, branch + "\n", "")
            return subprocess.CompletedProcess(args, 0, SHA + "\n", "")
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, "", "")
        if hang:
            raise subprocess.TimeoutExpired(["git", *args], 120)
        return subprocess.CompletedProcess(args, 128, "", stderr)
    monkeypatch.setattr(gitvcs, "_run", fake)
    monkeypatch.setattr(gitvcs, "git_available", lambda: True)
    return calls


def plain_board(tmp_path, **keys):
    (tmp_path / ".claude" / "board").mkdir(parents=True)
    for state in STATES:
        (tmp_path / "AI" / "Tasks" / state).mkdir(parents=True)
    config = {"sources": [{"name": "tasks", "root": "AI/Tasks", "prefix": "T", "editable": True}], **keys}
    board_json(tmp_path).write_text(json.dumps(config), encoding="utf-8")
    return load_board_config(str(board_json(tmp_path)))


@pytest.fixture
def plain_config(tmp_path):
    """A git board on plain task folders (no checkout)."""
    return plain_board(tmp_path, main_branch="master")


@pytest.fixture
def svn_plain_config(tmp_path):
    """An svn board on plain task folders (no working copy)."""
    return plain_board(tmp_path, vcs="svn")


class TestFailures:
    @pytest.mark.parametrize("stderr,message", [
        ("fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com\n",
         "git fetch failed: unable to access 'https://github.com/x/y.git/': Could not resolve host"),
        ("fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
         "git fetch failed: could not read Username for 'https://github.com': terminal prompts disabled"),
        ("git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.\n",
         "git fetch failed: Could not read from remote repository."),
    ])
    def test_offline_and_auth_errors(self, plain_config, monkeypatch, stderr, message):
        calls = failing_runner(monkeypatch, stderr)
        result = git_update(plain_config)
        assert result.status == "error" and result.message.startswith(message)
        assert "merge" not in calls and result.revision == SHA[:7]

    def test_git_never_prompts_and_messages_are_untranslated(self, monkeypatch):
        monkeypatch.setenv("LANG", "de_CH.UTF-8")
        env = gitvcs._env()
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
        assert env["LC_MESSAGES"] == "C" and env["LANG"] == "de_CH.UTF-8"
        assert "LC_ALL" not in env or env["LC_ALL"] == os.environ.get("LC_ALL")

    def test_every_git_call_has_no_stdin_and_the_no_prompt_env(self, plain_config, monkeypatch):
        seen = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw))
                            or subprocess.CompletedProcess(cmd, 0, "", ""))
        gitvcs._run(plain_config, ["fetch", "--quiet"])
        gitvcs.git_branch(str(plain_config["repo_root_path"]))
        assert seen and all(kw["env"]["GIT_TERMINAL_PROMPT"] == "0" and kw["stdin"] == subprocess.DEVNULL
                            for _, kw in seen)

    def test_fetch_does_not_write_fetch_head(self, plain_config, monkeypatch):
        fetches = []
        monkeypatch.setattr(gitvcs, "_FETCH_HEAD_OPTION", [True])
        monkeypatch.setattr(gitvcs, "_run", lambda config, args, timeout=0: fetches.append(args)
                            or subprocess.CompletedProcess(args, 0, "", ""))
        assert gitvcs.git_fetch(plain_config).returncode == 0
        assert fetches == [["fetch", "--no-write-fetch-head", "--quiet"]]

    def test_git_update_fetches_without_fetch_head(self, plain_config, monkeypatch):
        calls = []
        monkeypatch.setattr(gitvcs, "_FETCH_HEAD_OPTION", [True])
        failing_runner(monkeypatch, "fatal: unable to access\n")
        fake = gitvcs._run
        monkeypatch.setattr(gitvcs, "_run", lambda config, args, timeout=0: calls.append(args) or fake(config, args))
        assert git_update(plain_config).status == "error"
        assert ["fetch", "--no-write-fetch-head", "--quiet"] in calls

    def test_fetch_falls_back_silently_on_git_before_2_29(self, plain_config, monkeypatch):
        fetches = []

        def old_git(config, args, timeout=0):
            fetches.append(args)
            if "--no-write-fetch-head" in args:
                return subprocess.CompletedProcess(args, 129, "", "error: unknown option `no-write-fetch-head'\n")
            return subprocess.CompletedProcess(args, 0, "", "")
        monkeypatch.setattr(gitvcs, "_FETCH_HEAD_OPTION", [True])
        monkeypatch.setattr(gitvcs, "_run", old_git)
        assert gitvcs.git_fetch(plain_config).returncode == 0
        assert gitvcs.git_fetch(plain_config).returncode == 0  # remembered: no second try of the new option
        assert fetches == [["fetch", "--no-write-fetch-head", "--quiet"], ["fetch", "--quiet"], ["fetch", "--quiet"]]

    @needs_git
    def test_the_update_leaves_no_fetch_head(self, repos):
        new_task(repos.b, "Theirs", by="coordinator (Robin)")
        commit(repos.b)
        fetch_head = repos.a / ".git" / "FETCH_HEAD"
        assert not fetch_head.exists()
        assert git_update(repos.config).status == "changed"
        assert not fetch_head.exists()

    def test_timeout_is_an_error(self, plain_config, monkeypatch):
        failing_runner(monkeypatch, hang=True)
        result = git_update(plain_config)
        assert result.status == "error" and result.message == "git fetch timed out after 120 s"

    def test_missing_git_disables_updating(self, plain_config, monkeypatch):
        monkeypatch.setattr(gitvcs.shutil, "which", lambda name: None)
        updater = Updater(plain_config, 60, clock=lambda: 0.0)
        result, notices = updater.run()
        assert result.status == "no-vcs" and not updater.enabled and not updater.due()
        assert notices == ["update disabled: git is not on PATH; the board is not updated automatically"]

    def test_off_branch_is_not_a_failure(self, plain_config, monkeypatch):
        calls = failing_runner(monkeypatch, "x", branch="t001-x")
        updater = Updater(plain_config, 60, clock=lambda: 0.0)
        result, notices = updater.run()
        assert result.status == "off-branch" and updater.enabled and updater.failures == 0
        assert "fetch" not in calls
        assert notices == ["update off: the checkout is on branch 't001-x', not 'master'; updating resumes on "
                           "master"]

    def test_reported_once_silent_repeats_recovery_and_back_off(self, plain_config):
        now = [0.0]
        results = []
        updater = Updater(plain_config, 60, clock=lambda: now[0], update=lambda c: results.pop(0))
        offline = UpdateResult("error", message="git fetch failed: Could not resolve host")
        results += [offline, offline, offline, UpdateResult("error", message="git fetch failed: auth"),
                    UpdateResult("ok", revision="a1b2c3d")]
        assert updater.due()
        assert updater.run()[1] == ["update failed: git fetch failed: Could not resolve host; retrying"]
        assert updater.next_at == 60
        now[0] = 60
        assert updater.run()[1] == []
        now[0] = 120
        assert updater.run()[1] == []
        assert updater.failures == 3 and updater.next_at == 120 + update.BACKOFF_SECONDS
        now[0] = 200
        assert not updater.due()
        now[0] = 420
        assert updater.run()[1] == ["update failed: git fetch failed: auth; retrying"]
        now[0] = 720
        assert updater.run()[1] == ["update ok again (a1b2c3d)"]
        assert updater.failures == 0 and updater.next_at == 780
        assert updater.state["status"] == "ok" and updater.state["revision"] == "a1b2c3d"


# ---- failures, reporting and back-off (svn runner replaced) ----

def failing_svn_runner(monkeypatch, stderr=None, hang=False):
    calls = []

    def fake(config, args, timeout=0):
        calls.append(args[0])
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "info":
            return subprocess.CompletedProcess(args, 0, "7\n", "")
        if hang:
            raise subprocess.TimeoutExpired(args, 120)
        return subprocess.CompletedProcess(args, 1, "", stderr)
    monkeypatch.setattr(svnvcs, "_run", fake)
    monkeypatch.setattr(svnvcs, "svn_available", lambda: True)
    return calls


class TestSvnFailures:
    @pytest.mark.parametrize("stderr,code", [
        ("svn: E170013: Unable to connect to a repository at URL 'https://x/svn'\nsvn: E731001: No such host\n",
         "E170013 Unable to connect"),
        ("svn: E215004: No more credentials or we tried too many times.\n", "E215004 No more credentials"),
    ])
    def test_offline_and_auth_errors(self, svn_plain_config, monkeypatch, stderr, code):
        failing_svn_runner(monkeypatch, stderr)
        result = run_update(svn_plain_config)
        assert result.status == "error" and result.message.startswith(code)

    def test_svn_messages_are_untranslated_without_touching_other_locale_settings(self, monkeypatch):
        monkeypatch.setenv("LANG", "de_CH.UTF-8")
        env = svnvcs._env()
        assert env["LC_MESSAGES"] == "C" and env["LANG"] == "de_CH.UTF-8"
        assert "LC_ALL" not in env or env["LC_ALL"] == os.environ.get("LC_ALL")

    def test_every_svn_call_is_non_interactive_without_stdin(self, svn_plain_config, monkeypatch):
        seen = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw))
                            or subprocess.CompletedProcess(cmd, 0, "", ""))
        svnvcs._run(svn_plain_config, ["update"])
        svnvcs.svn_location(str(svn_plain_config["repo_root_path"]))
        assert seen and all("--non-interactive" in cmd and kw["stdin"] == subprocess.DEVNULL for cmd, kw in seen)

    def test_timeout_is_an_error(self, svn_plain_config, monkeypatch):
        failing_svn_runner(monkeypatch, hang=True)
        result = svn_update(svn_plain_config)
        assert result.status == "error" and "timed out" in result.message

    def test_missing_svn_disables_updating(self, svn_plain_config, monkeypatch):
        monkeypatch.setattr(svnvcs.shutil, "which", lambda name: None)
        updater = Updater(svn_plain_config, 60, clock=lambda: 0.0)
        result, notices = updater.run()
        assert result.status == "no-vcs" and not updater.enabled and not updater.due()
        assert notices == ["update disabled: svn is not on PATH; the board is not updated automatically"]

    def test_recovery_names_the_svn_revision(self, svn_plain_config):
        results = [UpdateResult("error", message="E170013 Unable to connect"), UpdateResult("ok", revision=9)]
        now = [0.0]
        updater = Updater(svn_plain_config, 60, clock=lambda: now[0], update=lambda c: results.pop(0))
        assert updater.run()[1] == ["update failed: E170013 Unable to connect; retrying"]
        now[0] = 60
        assert updater.run()[1] == ["update ok again (r9)"]
        assert updater.state["revision"] == 9 and updater.state["vcs"] == "svn"


# ---- config ----

class TestConfig:
    def config(self, tmp_path, **watch_keys):
        for state in STATES:
            (tmp_path / "AI" / "Tasks" / state).mkdir(parents=True, exist_ok=True)
        return validate_board_config({"repo_root": ".", "watch": watch_keys,
                                      "sources": [{"name": "t", "root": "AI/Tasks"}]}, str(tmp_path))

    def test_defaults(self, tmp_path):
        watch_config = self.config(tmp_path)["watch"]
        assert watch_config["update_seconds"] == 60 and watch_config["update_paths"] == ["AI"]
        assert watch_config["update_when_dirty"] is False and watch_config["board_idle_minutes"] == 15

    @pytest.mark.parametrize("value", [0, 10, 60.5])
    def test_valid_intervals(self, tmp_path, value):
        assert self.config(tmp_path, update_seconds=value)["watch"]["update_seconds"] == value

    @pytest.mark.parametrize("keys,message", [
        ({"update_seconds": -1}, "0 (off) or a number of at least 10"),
        ({"update_seconds": 1}, "at least 10"),
        ({"update_seconds": 9}, "at least 10"),
        ({"update_seconds": True}, "at least 10"),
        ({"update_paths": "AI"}, "non-empty list"),
        ({"update_paths": []}, "non-empty list"),
        ({"update_paths": ["../other"]}, "without '..'"),
        ({"update_paths": ["AI/../src"]}, "without '..'"),
        ({"update_paths": ["C:/x"]}, "relative path"),
        ({"update_paths": ["/abs"]}, "relative path"),
        ({"update_paths": ["."]}, "not the repo root"),
        ({"update_paths": ["Missing"]}, "folder not found"),
        ({"update_when_dirty": "no"}, "true or false"),
        ({"update": 1}, "watch"),
    ])
    def test_invalid(self, tmp_path, keys, message):
        with pytest.raises(BoardConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
            self.config(tmp_path, **keys)

    def test_cli_update_seconds_is_validated(self, plain_config, capsys):
        path = plain_config["config_path"]
        assert cli.main(["--config", path, "watch", "--no-server", "--update-seconds", "5"]) == 2
        assert "--update-seconds must be 0 (off) or a number of at least 10" in capsys.readouterr().err


# ---- the cross-process write lock ----

class TestWriteLock:
    def test_reentrant_and_the_lock_file_stays(self, tmp_path):
        path = str(tmp_path / "log" / "write.lock")
        with store.write_lock(wait=0.2, path=path):
            with store.write_lock(wait=0.2, path=path):  # re-entrant in the same thread
                pass
            other = {}

            def other_thread():
                try:
                    with store.write_lock(wait=0.2, path=path):
                        other["got"] = True
                except store.LockBusy:
                    other["busy"] = True
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
            assert other == {"busy": True}
        assert os.path.exists(path)  # nothing to clean up, nothing stale: the OS lock is gone with the handle
        with store.write_lock(wait=0.2, path=path):
            pass

    def test_concurrent_takers_exclude_each_other(self, tmp_path):
        path = str(tmp_path / "write.lock")
        inside, peak, errors = [0], [0], []
        guard = threading.Lock()

        def taker():
            try:
                for _ in range(15):
                    with store.write_lock(wait=20, path=path):
                        with guard:
                            inside[0] += 1
                            peak[0] = max(peak[0], inside[0])
                        time.sleep(0.002)
                        with guard:
                            inside[0] -= 1
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        threads = [threading.Thread(target=taker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == [] and peak[0] == 1

    def test_two_taker_processes_exclude_each_other(self, tmp_path):
        counter = tmp_path / "counter.txt"
        counter.write_text("0", encoding="utf-8")
        script = ("import sys, time; sys.path.insert(0, sys.argv[1]); from taskboard import store\n"
                  "for i in range(25):\n"
                  "    with store.write_lock(wait=30):\n"
                  "        n = int(open(sys.argv[2]).read()); time.sleep(0.002)\n"
                  "        open(sys.argv[2], 'w').write(str(n + 1))\n")
        procs = [subprocess.Popen([sys.executable, "-c", script, BOARD_DIR, str(counter)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, err
        assert counter.read_text(encoding="utf-8") == "50"  # no lost increment: never two holders at once

    def test_a_crashed_holder_never_blocks(self, tmp_path):
        script = ("import sys, time; sys.path.insert(0, sys.argv[1]); from taskboard import store\n"
                  "with store.write_lock():\n"
                  "    print('held', flush=True); time.sleep(60)\n")
        p = subprocess.Popen([sys.executable, "-c", script, BOARD_DIR], stdout=subprocess.PIPE, text=True)
        try:
            assert p.stdout.readline().strip() == "held"
            with pytest.raises(store.LockBusy):
                with store.write_lock(wait=0.3):
                    pass
        finally:
            p.kill()  # the holder dies without releasing (our own child, stopped by its handle)
            p.wait(30)
            p.stdout.close()
        start = time.monotonic()
        with store.write_lock(wait=5):
            pass
        assert time.monotonic() - start < 2

    def test_tests_never_use_the_checkouts_lock(self, private_write_lock):
        assert store.LOCK_PATH == private_write_lock == os.environ["TB_WRITE_LOCK"]
        assert not store.LOCK_PATH.startswith(BOARD_DIR)

    def test_a_busy_lock_is_the_normal_write_error(self, plain_config, monkeypatch, capsys):
        path = plain_config["config_path"]
        assert cli.main(["--config", path, "new", "--title", "x", "--type", "bug", "--priority", "low"]) == 0
        monkeypatch.setattr(store, "LOCK_WAIT_SECONDS", 0.2)
        taken, done = threading.Event(), threading.Event()

        def holder():
            with store.write_lock():
                taken.set()
                done.wait(10)
        t = threading.Thread(target=holder)
        t.start()
        taken.wait(5)
        try:
            capsys.readouterr()
            assert cli.main(["--config", path, "log", "T001", "x"]) == 1
            assert "locked by another writer" in capsys.readouterr().err
        finally:
            done.set()
            t.join()

    def test_replace_waits_while_another_process_has_the_target_open(self, tmp_path):
        target = tmp_path / "T001-x.md"
        target.write_bytes(b"old\n")
        opened, done = threading.Event(), threading.Event()

        def reader():  # Python's open() on Windows does not share delete: os.replace fails meanwhile
            with open(target, "rb"):
                opened.set()
                time.sleep(0.3)
            done.set()
        t = threading.Thread(target=reader)
        t.start()
        opened.wait(5)
        store.atomic_write(str(tmp_path), str(target), b"new\n", store.content_hash(b"old\n"))
        t.join()
        assert target.read_bytes() == b"new\n" and done.is_set()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["T001-x.md"]

    def test_replace_gives_up_after_its_retries(self, tmp_path, monkeypatch):
        calls = []

        def busy(src, dst):
            calls.append(1)
            raise PermissionError(5, "Access is denied")
        monkeypatch.setattr(store.os, "replace", busy)
        monkeypatch.setattr(store, "REPLACE_RETRY_SECONDS", 0)
        with pytest.raises(PermissionError):
            store.replace_file("a", "b")
        assert len(calls) == store.REPLACE_RETRIES

    @needs_git
    def test_two_writer_processes_and_updates_lose_nothing(self, repos):
        new_task(repos.a)
        commit(repos.a)
        config = config_of(repos.a, update_when_dirty=True)
        script = ("import sys; sys.path.insert(0, sys.argv[1]); from taskboard import cli\n"
                  "for i in range(8):\n"
                  "    assert cli.main(['--config', sys.argv[2], 'log', 'T001', f'{sys.argv[3]} {i}']) == 0\n")
        procs = [subprocess.Popen([sys.executable, "-c", script, BOARD_DIR, str(board_json(repos.a)), who],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for who in ("one", "two")]
        statuses = set()
        while any(p.poll() is None for p in procs):
            statuses.add(git_update(config).status)
        for p in procs:
            out, err = p.communicate()
            assert p.returncode == 0, err
        text = task_path(repos.a).read_text(encoding="utf-8")
        for who in ("one", "two"):
            for i in range(8):
                assert f"supervisor: {who} {i}\n" in text
        assert update.conflicted(config) == []
        assert statuses <= {"ok", "busy"}

    @needs_svn
    def test_two_writer_processes_and_svn_updates_lose_nothing(self, svn_repos):
        repos = svn_repos
        new_task(repos.a)
        svn_commit_all(repos.a)
        config = config_of(repos.a, update_when_dirty=True)
        script = ("import sys; sys.path.insert(0, sys.argv[1]); from taskboard import cli\n"
                  "for i in range(8):\n"
                  "    assert cli.main(['--config', sys.argv[2], 'log', 'T001', f'{sys.argv[3]} {i}']) == 0\n")
        procs = [subprocess.Popen([sys.executable, "-c", script, BOARD_DIR, str(board_json(repos.a)), who],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for who in ("one", "two")]
        statuses = set()
        while any(p.poll() is None for p in procs):
            statuses.add(svn_update(config).status)
        for p in procs:
            out, err = p.communicate()
            assert p.returncode == 0, err
        text = task_path(repos.a).read_text(encoding="utf-8")
        for who in ("one", "two"):
            for i in range(8):
                assert f"supervisor: {who} {i}\n" in text
        assert update.conflicted(config) == []
        assert statuses <= {"ok", "busy"}


# ---- the watcher ----

class FakeTime:
    """time.sleep for the watch loop: advances a clock, runs scripted actions, stops an endless loop."""

    def __init__(self, actions=None, limit=200):
        self.now = 0.0
        self.calls = 0
        self.actions = actions or {}
        self.limit = limit

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.calls += 1
        self.now += seconds
        if self.calls in self.actions:
            self.actions[self.calls]()
        if self.calls >= self.limit:
            raise KeyboardInterrupt


def run_watch(config, monkeypatch, fake_time, update_fn, interval=10, **kwargs):
    monkeypatch.setattr(watch.time, "sleep", fake_time.sleep)
    updater = Updater(config, interval, clock=fake_time.clock, update=update_fn)
    # No checkout here: the revision comes from the fake update results only.
    return watch.watch(config, start_server=False, updater=updater, revision_of=lambda c: None,
                       own_of=lambda c, old, new: False, changed_of=lambda c, old, new: None, **kwargs)


def write_task(config, task_id, title, state="00-Conception", waiting="null", questions="", log="", review="pr"):
    root = config["sources"][0]["root_path"]
    path = os.path.join(root, state, f"{task_id}-{title.lower().replace(' ', '-')}.md")
    text = (f"---\nid: {task_id}\ntitle: {title}\ntype: bug\npriority: low\nowner: null\ncreated: 2026-10-20\n"
            f"updated: 2026-10-20\nwaiting_on: {waiting}\nblocked_by: []\nplan: null\nbranch: null\n{review}: null\n"
            f"---\n\n## Problem\nP\n\n## Approach\n\n## Acceptance\n\n## Questions\n{questions}\n## Decisions\n\n"
            f"## Log\n- 2026-10-20 supervisor: created.\n{log}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def changed(*paths):
    return UpdateResult("changed", revision_before=5, revision=6, changed=[("A", p) for p in paths])


class TestWatch:
    def test_incoming_kinds_are_reported_and_own_edits_are_not(self, plain_config, monkeypatch, capsys):
        config = plain_config
        write_task(config, "T001", "Old one", log="- 2026-10-19 Robin (board): old note.\n")
        updates = []

        def fake_update(c):
            if not updates:
                return UpdateResult("ok", revision=5)
            return updates.pop(0)()

        def own_edits():
            assert tb_cfg(config, "log", "T001", "my own line") == 0
            assert tb_cfg(config, "ask", "T001", "--for", "supervisor", "Own question?") == 0

        def arrive():
            write_task(config, "T002", "New from b")
            write_task(config, "T003", "Asks", state="01-Refinement",
                       questions="- **Q4** (open, for supervisor): Which?\n")
            write_task(config, "T004", "Review me", state="04-CodeReview", waiting="supervisor")
            path = write_task(config, "T005", "Answered", state="01-Refinement")
            os.replace(path, path)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write("- 2026-10-20 Robin (board): answered Q2.\n")
            return UpdateResult("changed", revision_before=5, revision=9, changed=[("A", "AI/Tasks")])
        fake = FakeTime(actions={3: own_edits, 4: lambda: updates.append(arrive)})
        assert run_watch(config, monkeypatch, fake, fake_update) == 0
        out = capsys.readouterr().out
        assert "BOARD INPUT" not in out
        incoming = out.split("INCOMING (git pull, 5 -> 9):\n")[1].split("WAITING ON SUPERVISOR")[0]
        assert "git fetch + fast-forward of master every 10 s" in out
        assert incoming.splitlines() == [
            "  T002 new task: New from b (00-Conception)",
            "  T003 new task: Asks (01-Refinement)",
            "  T004 new task: Review me (04-CodeReview)",
            "  T005 new task: Answered (01-Refinement)",
            "  T003 Q4 open, for supervisor",
            "  T004 now waiting on supervisor (04-CodeReview)",
            "  T005 Robin (board): answered Q2.",
        ]
        assert "Own question" not in out and "T001 Q" not in incoming
        # A restarted watcher takes everything reported as its base: nothing again.
        fake2 = FakeTime(limit=60)
        assert run_watch(config, monkeypatch, fake2, lambda c: UpdateResult("ok", revision=9)) == 130
        assert capsys.readouterr().out.count("\n") == 1

    def test_svn_incoming_header_and_every_key_counts(self, svn_plain_config, monkeypatch, capsys):
        """svn can't tell own commits or changed files apart: the backend answers "unknown" and every new key of
        a moved revision is INCOMING, under `svn update, rA -> rB`."""
        config = svn_plain_config
        write_task(config, "T001", "Old one", review="rev")
        updates = []

        def fake_update(c):
            return updates.pop(0)() if updates else UpdateResult("ok", revision=5)

        def arrive():
            write_task(config, "T002", "New from b", review="rev")
            return UpdateResult("changed", revision_before=5, revision=9, changed=[("U", "AI/Tasks")])
        fake = FakeTime(actions={4: lambda: updates.append(arrive)})
        monkeypatch.setattr(watch.time, "sleep", fake.sleep)
        updater = Updater(config, 10, clock=fake.clock, update=fake_update)
        assert watch.watch(config, start_server=False, updater=updater, revision_of=lambda c: None) == 0
        out = capsys.readouterr().out
        assert "svn update of AI every 10 s" in out
        assert "INCOMING (svn update, r5 -> r9):\n  T002 new task: New from b (00-Conception)\n" in out
        assert "T001" not in out.split("INCOMING")[1]

    def test_changes_without_update_are_not_incoming(self, plain_config, monkeypatch, capsys):
        config = plain_config

        def edit_between_updates():
            write_task(config, "T001", "Local only", waiting="supervisor")
        fake = FakeTime(actions={2: edit_between_updates}, limit=80)
        results = iter([UpdateResult("ok", revision=1)] + [changed("AI/other")] * 100)
        assert run_watch(config, monkeypatch, fake, lambda c: next(results)) == 130
        assert capsys.readouterr().out.startswith("watching tasks")

    def test_a_burst_of_commits_gives_one_report(self, plain_config, monkeypatch, capsys):
        config = plain_config
        steps = {12: lambda: write_task(config, "T001", "First"), 16: lambda: write_task(config, "T002", "Second")}
        pending = []

        revs = [5]

        def fake_update(c):
            if pending:
                pending.pop()()
                revs.append(revs[-1] + 1)
                return UpdateResult("changed", revision_before=revs[-2], revision=revs[-1],
                                    changed=[("A", "AI/Tasks")])
            return UpdateResult("ok", revision=revs[-1])
        fake = FakeTime(actions={k: (lambda f=f: pending.append(f)) for k, f in steps.items()})
        assert run_watch(config, monkeypatch, fake, fake_update, quiet_seconds=60) == 0
        out = capsys.readouterr().out
        assert out.count("INCOMING") == 1 and "INCOMING (git pull, 5 -> 7)" in out
        assert "T001 new task: First" in out and "T002 new task: Second" in out

    def test_board_input_is_still_reported(self, plain_config, monkeypatch, capsys):
        config = plain_config
        path = write_task(config, "T001", "Mine")

        def answer():
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write("- 2026-10-20 Alex (board): a note.\n")
        fake = FakeTime(actions={3: answer})
        assert run_watch(config, monkeypatch, fake, lambda c: UpdateResult("ok", revision=5)) == 0
        out = capsys.readouterr().out
        assert "BOARD INPUT:\n  T001: - 2026-10-20 Alex (board): a note." in out and "INCOMING" not in out

    def test_conflict_exits_3_once(self, plain_config, monkeypatch, capsys):
        config = plain_config
        conflict = UpdateResult("conflict", conflicts=["AI/Tasks/01-Refinement/T001-x.md"],
                                message=gitvcs.conflict_message(["AI/Tasks/01-Refinement/T001-x.md"]))
        results = [UpdateResult("ok", revision=3), conflict] + [conflict] * 100
        assert run_watch(config, monkeypatch, FakeTime(), lambda c: results.pop(0)) == 3
        out = capsys.readouterr().out
        assert "UPDATE STOPPED: unmerged paths AI/Tasks/01-Refinement/T001-x.md; updating stopped." in out
        assert "WAITING ON SUPERVISOR" in out
        assert "git index is locked" == watch._Cycle(config, Updater(config, 0)).backend.lock_note[:19]
        # The restarted watcher does not wake the supervisor again for the same conflict.
        assert run_watch(config, monkeypatch, FakeTime(limit=50), lambda c: conflict) == 130
        assert "UPDATE STOPPED" not in capsys.readouterr().out
        # After it is resolved, a new conflict wakes it again.
        results = [UpdateResult("ok", revision=4), conflict]
        assert run_watch(config, monkeypatch, FakeTime(), lambda c: results.pop(0)) == 3

    def test_three_failures_in_a_row_exit_3_a_single_one_does_not(self, plain_config, monkeypatch, capsys):
        config = plain_config
        error = UpdateResult("error", message="E170013 Unable to connect")
        results = [error, UpdateResult("ok", revision=2), error, error, error]
        fake = FakeTime()
        assert run_watch(config, monkeypatch, fake, lambda c: results.pop(0)) == 3
        out = capsys.readouterr().out
        assert out.count("update failed: E170013 Unable to connect; retrying") == 2
        assert "update ok again (2)" in out
        assert "UPDATE FAILING: 3 git updates in a row failed (E170013 Unable to connect)" in out
        assert fake.now == 40  # updates at 0, 10, 20, 30, 40 (seconds)

    def test_svn_failures_name_the_svn_updates(self, svn_plain_config, monkeypatch, capsys):
        error = UpdateResult("error", message="E170013 Unable to connect")
        results = [error, UpdateResult("ok", revision=2), error, error, error]
        assert run_watch(svn_plain_config, monkeypatch, FakeTime(), lambda c: results.pop(0)) == 3
        out = capsys.readouterr().out
        assert "update ok again (r2)" in out
        assert "UPDATE FAILING: 3 svn updates in a row failed (E170013 Unable to connect)" in out

    @pytest.mark.parametrize("how", ["flag", "zero"])
    def test_no_update_is_todays_watcher(self, plain_config, monkeypatch, capsys, how):
        config = plain_config
        if how == "zero":
            config["watch"]["update_seconds"] = 0
        monkeypatch.setattr(watch.time, "sleep", FakeTime(limit=30).sleep)
        monkeypatch.setattr(update, "run_update", lambda c: pytest.fail("no update expected"))
        started = []
        monkeypatch.setattr(watch, "board_state", lambda c: "down")
        monkeypatch.setattr(watch, "start_board", lambda c, no_update=False, commits=False: started.append(no_update))
        kwargs = {"update": False} if how == "flag" else {}
        assert watch.watch(config, **kwargs) == 130
        assert started and set(started) == {False}
        out = capsys.readouterr().out
        assert out.count("\n") == 1 and "git fetch" not in out

    def test_watcher_starts_its_board_without_updates(self, plain_config, monkeypatch):
        started = []
        monkeypatch.setattr(watch, "board_state", lambda c: "ours" if started else "down")
        monkeypatch.setattr(watch, "start_board", lambda c, no_update=False, commits=False: started.append(no_update))
        monkeypatch.setattr(watch.time, "sleep", FakeTime(limit=5).sleep)
        updater = Updater(plain_config, 10, clock=lambda: 0.0, update=lambda c: UpdateResult("ok", revision=1))
        assert watch.watch(plain_config, updater=updater) == 130
        assert started == [True]
        command = watch.board_command(plain_config, no_update=True)
        assert command[-5:] == ["serve", "--no-commit", "--idle-exit", "15", "--no-update"]
        assert watch.board_command(plain_config, no_update=False)[-4:] == ["serve", "--no-commit", "--idle-exit",
                                                                           "15"]

    def test_idle_watcher_never_exits_and_prints_one_line(self, plain_config, monkeypatch, capsys):
        write_task(plain_config, "T001", "Idle", waiting="supervisor")
        fake = FakeTime(limit=100)  # 100 polls of 5 s
        calls = []

        def fake_update(c):
            calls.append(fake.now)
            return UpdateResult("ok", revision=1)
        assert run_watch(plain_config, monkeypatch, fake, fake_update) == 130
        assert capsys.readouterr().out.count("\n") == 1
        assert calls == [float(t) for t in range(0, 500, 10)]  # one at start, then one every 10 s

    @needs_git
    def test_an_update_by_someone_else_is_incoming_too(self, repos):
        """The supervisor's own `git pull` (or a self-updating board) brings another dev's task: still INCOMING."""
        config = repos.config
        cycle = watch._Cycle(config, Updater(config, 60, clock=lambda: 0.0))
        assert cycle.step() is None  # the watcher's first update, base taken
        cycle.incoming.clear()
        # The supervisor's own edits and commits between updates never count, even created and committed
        # within one poll (HEAD moves only through its own commits).
        new_task(repos.a, "Mine")
        commit(repos.a)
        assert tb(repos.a, "ask", "T001", "--for", "supervisor", "Own question?") == 0
        commit(repos.a, "ask")
        assert cycle.step() is None and cycle.incoming == {}
        git(repos.b, "pull", "-q", "--ff-only")
        new_task(repos.b, "Pulled by hand", by="coordinator (Robin)")
        commit(repos.b)
        git(repos.a, "pull", "-q", "--ff-only")  # not the watcher's update (its next one is not due)
        assert not cycle.updater.due()
        assert cycle.step() is None
        assert list(cycle.incoming.values()) == ["T002 new task: Pulled by hand (00-Conception)"]
        assert cycle.revisions[1] == head(repos.a)

    @needs_git
    def test_an_uncommitted_task_edit_during_a_pull_is_not_incoming(self, repos):
        """
        The landing chain `gh pr merge && git pull --ff-only && tb move ...` within one poll: the state is read from
        the working tree, so the uncommitted edit shows up next to the pull. Only tasks whose files the pulled
        commits changed count.
        """
        config = repos.config
        new_task(repos.a, "Mine")
        commit(repos.a)
        cycle = watch._Cycle(config, Updater(config, 60, clock=lambda: 0.0))
        assert cycle.step() is None
        cycle.incoming.clear()
        # A pull of commits that touch no task file, and the supervisor's uncommitted edits in the same poll.
        git(repos.b, "pull", "-q", "--ff-only")
        (repos.b / "src" / "code.txt").write_text("v2\n", encoding="utf-8")
        git(repos.b, "commit", "-q", "-am", "code")
        git(repos.b, "push", "-q")
        git(repos.a, "pull", "-q", "--ff-only")
        assert tb(repos.a, "ask", "T001", "--for", "supervisor", "Own question?") == 0
        assert tb(repos.a, "move", "T001", "refinement") == 0
        assert not cycle.updater.due()
        assert cycle.step() is None and cycle.incoming == {} and cycle.revisions == [None, None]
        # A pull that does change a task file reports exactly that task, not the uncommitted edit next to it.
        commit(repos.a, "mine")
        assert cycle.step() is None and cycle.incoming == {}  # own commits are absorbed
        git(repos.b, "pull", "-q", "--ff-only")
        new_task(repos.b, "Theirs", by="coordinator (Robin)")
        commit(repos.b)
        git(repos.a, "pull", "-q", "--ff-only")
        assert tb(repos.a, "ask", "T001", "--for", "supervisor", "Another own question?") == 0
        assert cycle.step() is None
        assert list(cycle.incoming.values()) == ["T002 new task: Theirs (00-Conception)"]

    @needs_git
    def test_changed_tasks_lists_the_tasks_whose_files_differ(self, repos):
        new_task(repos.a, "Mine")
        commit(repos.a)
        start = head(repos.a)
        assert tb(repos.a, "move", "T001", "refinement") == 0
        (repos.a / "src" / "code.txt").write_text("v2\n", encoding="utf-8")
        git(repos.a, "add", "-A")
        git(repos.a, "commit", "-q", "-m", "move and code")
        assert watch.changed_tasks(repos.config, start, head(repos.a)) == {("tasks", "T001")}
        assert watch.changed_tasks(repos.config, head(repos.a), head(repos.a)) == set()
        assert watch.changed_tasks(repos.config, "0" * 40, head(repos.a)) is None

    @needs_git
    def test_changed_tasks_skips_only_a_source_outside_the_repository(self, repos, monkeypatch):
        """A source outside the checkout (or on another drive: relpath raises ValueError) can't be changed by a
        pull: it is skipped, and the other sources keep the filter."""
        new_task(repos.a, "Mine")
        commit(repos.a)
        start = head(repos.a)
        assert tb(repos.a, "move", "T001", "refinement") == 0
        git(repos.a, "add", "-A")
        git(repos.a, "commit", "-q", "-m", "move")
        outside = repos.tmp / "elsewhere"
        drive = repos.tmp / "other-drive"
        sources = repos.config["sources"] + [
            {"name": "outside", "root_path": str(outside)}, {"name": "drive", "root_path": str(drive)}]
        config = {**repos.config, "sources": sources}
        relpath = os.path.relpath

        def fake_relpath(path, start=os.curdir):
            if str(path) == str(drive):
                raise ValueError("path is on mount 'D:', start on mount 'C:'")
            return relpath(path, start)
        monkeypatch.setattr(watch.os.path, "relpath", fake_relpath)
        assert watch.changed_tasks(config, start, head(repos.a)) == {("tasks", "T001")}

    @needs_git
    def test_a_branch_switch_is_not_incoming(self, repos):
        config = repos.config
        new_task(repos.a, "Old")
        commit(repos.a)
        cycle = watch._Cycle(config, Updater(config, 60, clock=lambda: 0.0))
        assert cycle.step() is None
        git(repos.a, "checkout", "-q", "-b", "t009-x")
        (repos.a / "AI" / "Tasks" / "00-Conception" / "notes.txt").write_text("x", encoding="utf-8")
        assert cycle.step() is None
        git(repos.a, "checkout", "-q", "master")
        assert cycle.step() is None and cycle.incoming == {}

    @needs_git
    def test_own_commits_only_reads_the_reflog(self, repos):
        start = head(repos.a)
        new_task(repos.a, "Mine")
        commit(repos.a)
        mine = head(repos.a)
        assert watch.own_commits_only(repos.config, start, mine) is True
        git(repos.b, "pull", "-q", "--ff-only")
        new_task(repos.b, "Theirs")
        commit(repos.b)
        git(repos.a, "pull", "-q", "--ff-only")
        assert watch.own_commits_only(repos.config, mine, head(repos.a)) is False
        assert watch.own_commits_only(repos.config, "0" * 40, head(repos.a)) is False

    def test_a_poll_during_someone_elses_update_does_not_lose_its_input(self, plain_config):
        """A pull moves HEAD before all files are written: a look in between must not become the new base."""
        wc = {"rev": 5, "running": False}
        cycle = watch._Cycle(plain_config, Updater(plain_config, 60, clock=lambda: 0.0,
                                                   update=lambda c: UpdateResult("ok", revision=wc["rev"])),
                             revision_of=lambda c: wc["rev"], running_of=lambda c: wc["running"])
        assert cycle.step() is None
        cycle.incoming.clear()
        wc.update(rev=6, running=True)  # an external update started: revision moved, files not there yet
        assert cycle.step() is None and cycle.incoming == {} and cycle.last_rev == 5
        write_task(plain_config, "T001", "Arrives")  # the update writes the file, then finishes
        wc["running"] = False
        assert cycle.step() is None
        assert list(cycle.incoming.values()) == ["T001 new task: Arrives (00-Conception)"]
        assert cycle.revisions == [5, 6]

    def test_a_revision_change_while_reading_is_looked_at_again(self, plain_config):
        revs = iter([5, 5, 5, 6, 6, 6, 6, 6])  # observe() reads the revision before and after the state
        cycle = watch._Cycle(plain_config, Updater(plain_config, 60, clock=lambda: 0.0,
                                                   update=lambda c: UpdateResult("ok", revision=5)),
                             revision_of=lambda c: next(revs), running_of=lambda c: False)
        assert cycle.step() is None  # observe (5, 5), own update -> absorb(5)
        cycle.incoming.clear()
        write_task(plain_config, "T001", "Arrives")
        cycle.observe()  # reads 5, state with T001, then 6: inconsistent, skipped
        assert cycle.last_rev == 5 and cycle.incoming == {}
        cycle.observe()  # 6 and 6: T001 is INCOMING
        assert list(cycle.incoming.values()) == ["T001 new task: Arrives (00-Conception)"]

    def test_a_failed_rev_parse_keeps_the_last_state(self, plain_config):
        """HEAD 5, None (rev-parse failed or off master), 6: the incoming task is still reported."""
        revs = iter([5, 5, 5, None, 6, 6, 6, 6, 6])
        cycle = watch._Cycle(plain_config, Updater(plain_config, 60, clock=lambda: 0.0,
                                                   update=lambda c: UpdateResult("ok", revision=5)),
                             revision_of=lambda c: next(revs), running_of=lambda c: False)
        assert cycle.step() is None  # observe (5, 5), own update -> absorb(5)
        cycle.incoming.clear()
        write_task(plain_config, "T001", "Arrives")  # half-arrived files, rev-parse fails
        cycle.observe()
        assert cycle.last_rev == 5 and cycle.incoming == {} and "T001" not in str(cycle.last_state)
        cycle.observe()  # 6 and 6
        assert list(cycle.incoming.values()) == ["T001 new task: Arrives (00-Conception)"]

    def test_a_stale_lock_prints_one_note_and_does_not_wake(self, plain_config, capsys):
        cycle = watch._Cycle(plain_config, Updater(plain_config, 60, clock=lambda: 0.0,
                                                   update=lambda c: UpdateResult("ok", revision=5)),
                             revision_of=lambda c: 5, running_of=lambda c: True)
        for _ in range(watch.LOCK_NOTE_POLLS * 3):
            assert cycle.step() is None
        assert capsys.readouterr().out.count(cycle.backend.lock_note) == 1
        cycle.running_of = lambda c: False
        cycle.step()
        cycle.running_of = lambda c: True  # a new lock period notes again, after the polls
        for _ in range(watch.LOCK_NOTE_POLLS):
            cycle.step()
        assert capsys.readouterr().out.count(cycle.backend.lock_note) == 1

    def test_update_running_is_the_git_index_lock(self, plain_config, monkeypatch, tmp_path):
        monkeypatch.setattr(gitvcs, "git_available", lambda: True)
        monkeypatch.setattr(gitvcs, "git_dir", lambda c: str(tmp_path))
        assert watch.update_running(plain_config) is False
        (tmp_path / "index.lock").write_text("", encoding="utf-8")
        assert watch.update_running(plain_config) is True
        monkeypatch.setattr(gitvcs, "git_dir", lambda c: None)
        assert watch.update_running(plain_config) is False

    @pytest.mark.parametrize("stdout,running", [
        ("! L     AI\n! L     AI\\Plans\n", True),  # what `svn status -q AI` shows during an update
        ("M       AI\\Tasks\\01-Refinement\\T001-x.md\n", False),
        ("", False),
    ])
    def test_svn_update_running_reads_the_lock_column(self, svn_plain_config, monkeypatch, stdout, running):
        monkeypatch.setattr(svnvcs, "svn_available", lambda: True)
        monkeypatch.setattr(svnvcs, "_run", lambda config, args, timeout=0:
                            subprocess.CompletedProcess(args, 0, stdout, ""))
        assert watch.update_running(svn_plain_config) is running

    def test_svn_backend_cannot_tell_own_commits_or_changed_tasks(self, svn_plain_config):
        assert watch.own_commits_only(svn_plain_config, 5, 6) is False
        assert watch.changed_tasks(svn_plain_config, 5, 6) is None

    @needs_svn
    def test_an_svn_update_by_someone_else_is_incoming_too(self, svn_repos):
        """The supervisor's own `svn update` (or a self-updating board) pulls another dev's task: still INCOMING."""
        repos = svn_repos
        config = repos.config
        cycle = watch._Cycle(config, Updater(config, 60, clock=lambda: 0.0))
        assert cycle.step() is None  # the watcher's first update, base taken
        cycle.incoming.clear()
        # The supervisor's own edits between updates never count, even with an update after them.
        new_task(repos.a, "Mine")
        svn_commit_all(repos.a)
        assert tb(repos.a, "ask", "T001", "--for", "supervisor", "Own question?") == 0
        assert cycle.step() is None and cycle.incoming == {}
        svn(repos.b, "update")
        new_task(repos.b, "Pulled by hand", by="coordinator (Robin)")
        svn_commit_all(repos.b)
        svn(repos.a, "update", "AI")  # not the watcher's update (its next one is not due)
        assert not cycle.updater.due()
        assert cycle.step() is None
        assert list(cycle.incoming.values()) == ["T002 new task: Pulled by hand (00-Conception)"]
        assert cycle.revisions[1] == svn_revision(repos.a)
        assert cycle.backend.lock_note == svnvcs.LOCK_NOTE

    def test_an_unreadable_task_file_keeps_its_id(self, plain_config):
        root = plain_config["sources"][0]["root_path"]
        with open(os.path.join(root, "01-Refinement", "T007-broken.md"), "w", encoding="utf-8") as f:
            f.write("<<<<<<< .mine\nno header\n=======\n>>>>>>> .r9\n")
        current = watch.state(plain_config)
        assert current[("task", "tasks", "T007")] == "T007 new task: (T007-broken.md) (01-Refinement)"
        assert not any("None" in line for line in current.values())

    def test_diverged_exits_3_once(self, plain_config, monkeypatch, capsys):
        diverged = UpdateResult("diverged", revision="a1b2c3d", head="a1b2c3d" + "0" * 33, message=gitvcs.DIVERGED)
        results = [UpdateResult("ok", revision=3), diverged] + [diverged] * 100
        assert run_watch(plain_config, monkeypatch, FakeTime(), lambda c: results.pop(0)) == 3
        assert ("UPDATE STOPPED: local and origin diverged; pull --rebase or push manually"
                in capsys.readouterr().out)
        assert run_watch(plain_config, monkeypatch, FakeTime(limit=50), lambda c: diverged) == 130
        assert "UPDATE STOPPED" not in capsys.readouterr().out

    def test_dirty_skip_and_off_branch_never_wake(self, plain_config, monkeypatch, capsys):
        results = [UpdateResult("dirty-skip", revision="a1b2c3d", message=gitvcs.WAITING_FOR_PUSH + " (1 file)"),
                   UpdateResult("off-branch", revision="b1b2c3d", message="the checkout is on branch 'x'")] * 50
        assert run_watch(plain_config, monkeypatch, FakeTime(limit=60), lambda c: results.pop(0)) == 130
        out = capsys.readouterr().out
        assert "update paused: waiting for local task edits to be committed and pushed (1 file)" in out
        assert "update off: the checkout is on branch 'x'" in out and "UPDATE" not in out

    def test_once_does_not_update(self, plain_config, monkeypatch, capsys):
        monkeypatch.setattr(update, "run_update", lambda c: pytest.fail("no update expected"))
        assert watch.watch(plain_config, once=True) == 0


def tb_cfg(config, *argv) -> int:
    return cli.main(["--config", config["config_path"], *argv])


# ---- tb serve: the updater thread and the web board ----

class TestServe:
    def test_update_thread_waits_for_the_write_lock(self, plain_config):
        calls = []
        updater = Updater(plain_config, 10, update=lambda c: calls.append(1) or UpdateResult("ok", revision=1))
        stop = threading.Event()
        with store._WRITE_LOCK:
            t = threading.Thread(target=update.serve_updates, args=(updater, store._WRITE_LOCK, stop, 0.01,
                                                                     lambda *a, **k: None))
            t.start()
            time.sleep(0.3)
            assert calls == []
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.01)
        stop.set()
        t.join(5)
        assert calls == [1]

    def test_serve_logs_state_changes(self, plain_config):
        results = [UpdateResult("changed", revision_before=1, revision=2, changed=[("A", "x")]),
                   UpdateResult("error", message="E170013 offline")]
        ticks = iter(range(0, 10 ** 6, 100))  # every look at the clock is 100 s later: every tick is due
        updater = Updater(plain_config, 10, clock=lambda: next(ticks), update=lambda c: results.pop(0))
        lines = []
        stop = threading.Event()
        lock = threading.Lock()

        def out(line, flush=False):
            lines.append(line)
            if not results:
                stop.set()
        update.serve_updates(updater, lock, stop, 0.0, out)
        assert [line[20:] for line in lines] == ["updated 1 -> 2 (1 file(s))",
                                                 "update failed: E170013 offline; retrying"]

    @pytest.mark.parametrize("no_update", [True, False])
    def test_serve_starts_the_thread_unless_no_update(self, plain_config, monkeypatch, no_update):
        import flask
        threads = []

        class FakeThread:
            def __init__(self, target, args, name, daemon):
                threads.append((target, name, daemon))

            def start(self):
                pass
        monkeypatch.setattr(cli.threading, "Thread", FakeThread)
        monkeypatch.setattr(cli, "run_board", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "port_problem", lambda host, port: None)
        argv = ["--config", plain_config["config_path"], "serve"] + (["--no-update"] if no_update else [])
        assert cli.main(argv) == 0
        assert threads == ([] if no_update else [(update.serve_updates, "periodic-update", True)])

    def test_no_update_thread_when_the_port_is_taken(self, plain_config, monkeypatch, capsys):
        import socket

        import flask
        threads = []
        monkeypatch.setattr(cli.threading, "Thread", lambda **kw: threads.append(kw))
        monkeypatch.setattr(cli, "run_board", lambda *a, **kw: pytest.fail("must not run"))
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            data = json.loads(open(plain_config["config_path"], encoding="utf-8").read())
            data["port"] = port
            with open(plain_config["config_path"], "w", encoding="utf-8") as f:
                json.dump(data, f)
            assert cli.port_problem("127.0.0.1", port) is not None
            assert cli.main(["--config", plain_config["config_path"], "serve"]) == 1
        assert threads == []
        assert "Cannot start the server" in capsys.readouterr().err

    def test_serve_busy_port_message_and_port_option(self, plain_config, monkeypatch, capsys):
        import socket

        import flask
        ran = []
        monkeypatch.setattr(cli, "run_board", lambda app, host, port, *a: ran.append({"port": port}))
        cfg = plain_config["config_path"]
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            assert cli.main(["--config", cfg, "serve", "--no-update", "--port", str(port)]) == 1
            err = capsys.readouterr().err
            assert str(port) in err and "--port" in err and "board.json" in err and not ran
        assert cli.main(["--config", cfg, "serve", "--no-update", "--port", str(port)]) == 0
        assert ran[0]["port"] == port
        assert cli.main(["--config", cfg, "serve", "--no-update", "--port", "0"]) != 0

    def test_watch_passes_the_port_and_reports_start_failure(self, plain_config, monkeypatch, capsys, tmp_path):
        plain_config["port"], plain_config["port_override"] = 5999, True
        command = watch.board_command(plain_config, no_update=True)
        assert command[-7:] == ["serve", "--port", "5999", "--no-commit", "--idle-exit", "15", "--no-update"]
        plain_config["port_override"] = False
        assert "--port" not in watch.board_command(plain_config, no_update=True)

        class Dead:
            def poll(self):
                return 1
        monkeypatch.setattr(watch, "BOARD_DIR", str(tmp_path))
        monkeypatch.setattr(watch, "board_state", lambda c: "down")
        monkeypatch.setattr(watch.subprocess, "Popen", lambda cmd, stdout, **kw: (
            stdout.write(b"Cannot start the server: 127.0.0.1:5999 is not free. Use --port N\n"), Dead())[1])
        assert watch.watch(plain_config, update=False) == 2
        assert "BOARD FAILED" in capsys.readouterr().err

    def test_start_board_exited_child_but_board_up_is_not_a_failure(self, plain_config, monkeypatch, tmp_path):
        class Dead:
            def poll(self):
                return 1
        ups = iter(["down", "ours"])  # not up at the first look, up when re-checked after the child exited
        monkeypatch.setattr(watch, "BOARD_DIR", str(tmp_path))
        monkeypatch.setattr(watch, "board_state", lambda c: next(ups))
        monkeypatch.setattr(watch.subprocess, "Popen", lambda cmd, stdout, **kw: Dead())
        watch.start_board(plain_config)

    def test_a_web_write_does_not_wait_for_a_whole_update(self, plain_config, monkeypatch):
        path = write_task(plain_config, "T001", "Asks", state="01-Refinement")
        monkeypatch.setattr(store, "LOCK_WAIT_SECONDS", 0.2)
        source = plain_config["sources"][0]
        taken, done = threading.Event(), threading.Event()

        def updating():  # what serve_updates does during an update
            with store._WRITE_LOCK:
                taken.set()
                done.wait(5)
        t = threading.Thread(target=updating)
        t.start()
        taken.wait(5)
        start = time.monotonic()
        try:
            with pytest.raises(store.WriteError, match="being updated"):
                store.apply_edit(source, "01-Refinement", os.path.basename(path), None, lambda s: s + "x\n",
                                 "master")
        finally:
            done.set()
            t.join()
        assert time.monotonic() - start < 2

    def app_with(self, config, results):
        updater = Updater(config, 10, clock=lambda: 0.0, update=lambda c: results.pop(0))
        return updater, create_app(config, updater=updater).test_client()

    def test_api_update_and_banner(self, plain_config):
        conflict_path = "AI/Tasks/01-Refinement/T001-x.md"
        updater, client = self.app_with(plain_config, [
            UpdateResult("ok", revision="a1b2c3d"),
            UpdateResult("error", message="git fetch failed: Could not resolve host"),
            UpdateResult("conflict", conflicts=[conflict_path], message="conflict"),
            UpdateResult("dirty-skip", message="waiting for local task edits to be committed and pushed (2 files)"),
            UpdateResult("diverged", message=gitvcs.DIVERGED),
            UpdateResult("off-branch", message="the checkout is on branch 't001-x', not 'master'"),
            UpdateResult("no-vcs", message="git is not on PATH; the board is not updated automatically")])
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["status"] == "pending" and info["text"] == "Update pending"
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["status"] == "ok" and info["revision"] == "a1b2c3d" and info["text"].startswith("Updated a1b2c3d, ")
        assert 'id="update-banner"' in client.get("/", base_url=BASE).get_data(as_text=True)
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["text"] == "Update failed: git fetch failed: Could not resolve host, retrying"
        assert info["level"] == "error"
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["status"] == "conflict" and conflict_path in info["text"] and "merge/rebase" in info["text"]
        assert conflict_path in client.get("/", base_url=BASE).get_data(as_text=True)
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["text"] == "Update paused: waiting for local task edits to be committed and pushed (2 files)"
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["text"] == "Update stopped: local and origin diverged; pull --rebase or push manually"
        assert info["level"] == "error"
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["text"] == "Update off: the checkout is on branch 't001-x', not 'master'" and info["level"] == "warn"
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["status"] == "no-vcs" and info["text"].startswith("Update off: git is not on PATH")

    def test_api_update_and_banner_for_svn(self, svn_plain_config):
        conflict_path = "AI/Tasks/01-Refinement/T001-x.md"
        updater, client = self.app_with(svn_plain_config, [
            UpdateResult("ok", revision=12),
            UpdateResult("error", message="E170013 Unable to connect"),
            UpdateResult("conflict", conflicts=[conflict_path], message="conflict"),
            UpdateResult("dirty-skip", message="waiting for local task edits to be committed (2 files)"),
            UpdateResult("no-vcs", message="svn is not on PATH; the board is not updated automatically")])
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["status"] == "ok" and info["revision"] == 12 and info["text"].startswith("Updated r12, ")
        assert info["vcs"] == "svn"
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["text"] == "Update failed: E170013 Unable to connect, retrying" and info["level"] == "error"
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["status"] == "conflict" and conflict_path in info["text"] and "Methodology" in info["text"]
        assert "merge/rebase" not in info["text"]
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["text"] == "Update paused: waiting for local task edits to be committed (2 files)"
        updater.run()
        info = client.get("/api/update", base_url=BASE).get_json()
        assert info["text"].startswith("Update off: svn is not on PATH")

    def test_serve_logs_svn_revisions(self, svn_plain_config):
        results = [UpdateResult("changed", revision_before=1, revision=2, changed=[("A", "x")])]
        updater = Updater(svn_plain_config, 10, clock=lambda: 0.0, update=lambda c: results.pop(0))
        lines = []
        stop = threading.Event()

        def out(line, flush=False):
            lines.append(line)
            stop.set()
        update.serve_updates(updater, threading.Lock(), stop, 0.0, out)
        assert [line[20:] for line in lines] == ["updated r1 -> r2 (1 file(s))"]

    def test_board_without_updater_has_no_banner(self, plain_config):
        client = create_app(plain_config).test_client()
        assert client.get("/api/update", base_url=BASE).get_json()["status"] == "off"
        assert "update-banner" not in client.get("/", base_url=BASE).get_data(as_text=True)

    def test_a_write_after_an_update_changed_the_file_is_refused_and_keeps_the_text(self, plain_config):
        path = write_task(plain_config, "T001", "Asks", state="01-Refinement", waiting="user",
                          questions="- **Q1** (open, for user): Which?\n")
        client = create_app(plain_config).test_client()
        url = "/task/tasks/01-Refinement/" + os.path.basename(path)
        html = client.get(url, base_url=BASE).get_data(as_text=True)
        token = html.split('name="token" value="')[1].split('"')[0]
        file_hash = html.split('name="hash" value="')[1].split('"')[0]
        with open(path, "a", encoding="utf-8", newline="\n") as f:  # what a git pull does
            f.write("- 2026-10-20 supervisor: arrived with git pull.\n")
        before = open(path, "rb").read()
        response = client.post(url + "/answer", base_url=BASE,
                               data={"token": token, "hash": file_hash, "question": "Q1", "answer": "my answer",
                                     "by": ""})
        body = response.get_data(as_text=True)
        assert response.status_code == 409
        assert "The file changed since you opened it" in body and "my answer" in body
        assert open(path, "rb").read() == before
