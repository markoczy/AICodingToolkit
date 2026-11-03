"""
Tests for `tb done "<message>"` (cli.cmd_done, the backends' commit_paths): the check runs first (a problem
commits nothing), then the task files of the update paths (and the handover file outside them) are committed
through the backend, pushed with git unless --no-push, with the message prefixed `tasks: `. Refused off the
main line like every write. Throw-away repositories of test_update (`repos`: git with a bare origin and a
second clone; `svn_repos`: `svnadmin create` with two trunk checkouts).
"""
import json
import subprocess

import pytest

from taskboard import cli
from taskboard.config import load_board_config
from test_update import (HAVE_GIT, HAVE_SVN, board_json, git, head, new_task, repos, svn, svn_repos, task_path, tb)

assert repos and svn_repos  # the fixtures

needs_git = pytest.mark.skipif(not HAVE_GIT, reason="git not installed")
needs_svn = pytest.mark.skipif(not HAVE_SVN, reason="svn not installed")


def git_status(wc, *paths) -> str:
    return git(wc, "status", "--porcelain", "--", *paths).rstrip()


def origin_master(wc) -> str:
    return git(wc, "rev-parse", "origin/master").strip()


class TestMessage:
    @pytest.mark.parametrize("given, expected", [
        ("T001 created", "tasks: T001 created"),
        ("tasks: T001 created", "tasks: T001 created"),
        ("  tasks:  two spaces ", "tasks: two spaces"),
        ("tasks:no space", "tasks: tasks:no space"),
        ("line\nbreak", "tasks: line break"),
    ])
    def test_prefix(self, given, expected):
        assert cli.done_message(given) == expected

    def test_empty_is_refused(self):
        with pytest.raises(Exception, match="message is empty"):
            cli.done_message("   ")


@needs_git
class TestDoneGit:
    def test_commits_the_task_files_and_pushes(self, repos, capsys):
        new_task(repos.a, "Mine")
        (repos.a / "src" / "code.txt").write_text("local change\n", encoding="utf-8")  # never committed
        capsys.readouterr()
        assert tb(repos.a, "done", "T001 created") == 0
        out = capsys.readouterr().out
        sha = head(repos.a)
        assert out.strip() == f"committed {sha[:7]} (tasks: T001 created); pushed"
        assert origin_master(repos.a) == sha
        assert git(repos.a, "log", "-1", "--format=%s").strip() == "tasks: T001 created"
        assert git_status(repos.a, "AI") == "" and git_status(repos.a, "src") == " M src/code.txt"
        git(repos.b, "pull", "-q", "--ff-only")
        assert task_path(repos.b).exists()

    def test_a_check_problem_commits_nothing(self, repos, capsys):
        new_task(repos.a, "Mine")
        (repos.a / "AI" / "Tasks" / "02-Ready" / "T002-broken.md").write_text("no header\n", encoding="utf-8")
        before = head(repos.a)
        assert tb(repos.a, "done", "broken") == 1
        err = capsys.readouterr().err
        assert "1 problem(s), nothing committed:" in err and "T002-broken.md" in err
        assert head(repos.a) == before and git_status(repos.a, "AI").startswith("??")

    def test_no_push_keeps_the_commit_local(self, repos, capsys):
        new_task(repos.a, "Mine")
        capsys.readouterr()
        assert tb(repos.a, "done", "tasks: T001 created", "--no-push") == 0
        out = capsys.readouterr().out
        assert out.strip() == f"committed {head(repos.a)[:7]} (tasks: T001 created); not pushed (--no-push)"
        assert origin_master(repos.a) != head(repos.a)

    def test_nothing_to_commit(self, repos, capsys):
        assert tb(repos.a, "done", "nothing") == 0
        assert capsys.readouterr().out.strip() == "nothing to commit under AI"
        (repos.a / "src" / "code.txt").write_text("code only\n", encoding="utf-8")
        assert tb(repos.a, "done", "nothing") == 0
        assert "nothing to commit" in capsys.readouterr().out

    def test_refused_off_the_main_branch(self, repos, capsys):
        new_task(repos.a, "Mine")
        git(repos.a, "checkout", "-q", "-b", "t001-x")
        before = head(repos.a)
        assert tb(repos.a, "done", "x") == 1
        err = capsys.readouterr().err
        assert "branch 't001-x', not of 'master'" in err and "Nothing was written" in err
        assert head(repos.a) == before

    def test_the_handover_file_outside_the_update_paths_is_included(self, repos, capsys):
        data = json.loads(board_json(repos.a).read_text(encoding="utf-8"))
        data["handover"] = {"path": "notes/Handover.md"}
        board_json(repos.a).write_text(json.dumps(data), encoding="utf-8")
        git(repos.a, "update-index", "--assume-unchanged", ".claude/board/board.json")
        (repos.a / "notes").mkdir()
        (repos.a / "notes" / "Handover.md").write_text("# Handover\n", encoding="utf-8")
        (repos.a / "notes" / "other.md").write_text("not committed\n", encoding="utf-8")
        new_task(repos.a, "Mine")
        config = load_board_config(str(board_json(repos.a)))
        assert cli.done_paths(config) == ["AI", "notes/Handover.md"]
        assert tb(repos.a, "done", "with handover") == 0
        shown = git(repos.a, "show", "--stat", "--format=%s", "HEAD")
        assert "notes/Handover.md" in shown and "T001" in shown and "other.md" not in shown
        assert origin_master(repos.a) == head(repos.a)

    def test_the_handover_file_inside_the_update_paths_is_not_listed_twice(self, repos):
        data = json.loads(board_json(repos.a).read_text(encoding="utf-8"))
        data["handover"] = {"path": "AI/SupervisorHandover.md"}
        board_json(repos.a).write_text(json.dumps(data), encoding="utf-8")
        assert cli.done_paths(load_board_config(str(board_json(repos.a)))) == ["AI"]

    def test_a_failed_push_reports_the_commit_and_exits_1(self, repos, capsys, monkeypatch):
        from taskboard.vcs import git as gitvcs
        real_run = gitvcs._run

        def failing(config, args, timeout=0):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 1, "", "fatal: unable to access 'origin': offline\n")
            return real_run(config, args, timeout)
        monkeypatch.setattr(gitvcs, "_run", failing)
        new_task(repos.a, "Mine")
        assert tb(repos.a, "done", "T001") == 1
        err = capsys.readouterr().err
        assert err.startswith(f"committed {head(repos.a)[:7]} (tasks: T001); git push failed: unable to access")
        assert "push by hand (git push)" in err


def svn_head_log(wc) -> str:
    url = svn(wc, "info", "--show-item", "repos-root-url").strip() + "/trunk"
    return svn(wc, "log", "-v", "-l", "1", "-r", "HEAD:1", url)


@needs_svn
class TestDoneSvn:
    def test_commits_the_task_files(self, svn_repos, capsys):
        repos = svn_repos
        new_task(repos.a, "Mine")
        (repos.a / "src" / "code.txt").write_text("local change\n", encoding="utf-8")  # never committed
        capsys.readouterr()
        assert tb(repos.a, "done", "T001 created") == 0
        out = capsys.readouterr().out.strip()
        rev = svn(repos.a, "info", "--show-item", "revision", str(task_path(repos.a))).strip()  # the committed file
        assert out == f"committed r{rev} (tasks: T001 created)"
        log = svn_head_log(repos.a)
        assert "tasks: T001 created" in log and "T001-mine.md" in log and "code.txt" not in log
        assert svn(repos.a, "status", "-q", "AI").strip() == ""
        assert "M" in svn(repos.a, "status", "-q", "src")
        svn(repos.b, "update")
        assert task_path(repos.b).exists()

    def test_a_check_problem_commits_nothing(self, svn_repos, capsys):
        repos = svn_repos
        new_task(repos.a, "Mine")
        (repos.a / "AI" / "Tasks" / "02-Ready" / "T002-broken.md").write_text("no header\n", encoding="utf-8")
        assert tb(repos.a, "done", "broken") == 1
        assert "1 problem(s), nothing committed:" in capsys.readouterr().err
        assert "tasks:" not in svn_head_log(repos.a)
        assert svn(repos.a, "status", "AI").strip().startswith("?")

    def test_nothing_to_commit(self, svn_repos, capsys):
        assert tb(svn_repos.a, "done", "nothing") == 0
        assert capsys.readouterr().out.strip() == "nothing to commit under AI"

    def test_refused_outside_the_trunk_working_copy(self, svn_repos, capsys):
        repos = svn_repos
        svn(repos.tmp, "copy", f"{repos.url}/trunk", f"{repos.url}/branches/b1", "-m", "branch")
        branch = repos.tmp / "wc_b1"
        svn(repos.tmp, "checkout", f"{repos.url}/branches/b1", str(branch))
        assert tb(branch, "done", "x") == 1
        assert "working copy of '^/branches/b1/AI/Tasks', not of '^/trunk'" in capsys.readouterr().err
        assert "tasks:" not in svn_head_log(repos.a)

    def test_a_failed_commit_is_reported(self, svn_repos, capsys, monkeypatch):
        from taskboard.vcs import svn as svnvcs
        real_run = svnvcs._run

        def failing(config, args, timeout=0):
            if args[0] == "commit":
                return subprocess.CompletedProcess(args, 1, "", "svn: E170013: Unable to connect to a repository\n")
            return real_run(config, args, timeout)
        monkeypatch.setattr(svnvcs, "_run", failing)
        new_task(svn_repos.a, "Mine")
        assert tb(svn_repos.a, "done", "T001") == 1
        assert "svn commit failed: E170013 Unable to connect" in capsys.readouterr().err
