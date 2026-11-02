"""
Tests for sv.py against a real local SVN repository (svnadmin + file:// URL). Each test gets a main trunk
checkout at <tmp>/main with sv.py's REPO_ROOT pointed at it, so worktrees go to <tmp>/main.worktrees.\n"""
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sv  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("svn") is None or shutil.which("svnadmin") is None,
                                reason="svn not installed")


def run_svn(cwd, *args) -> str:
    return subprocess.run(["svn", "--non-interactive", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def main(tmp_path, monkeypatch):
    server = tmp_path / "server"
    subprocess.run(["svnadmin", "create", str(server)], check=True, capture_output=True)
    url = server.as_uri()
    run_svn(tmp_path, "mkdir", "-m", "layout", f"{url}/trunk", f"{url}/branches")
    checkout = tmp_path / "main"
    run_svn(tmp_path, "checkout", f"{url}/trunk", str(checkout))
    (checkout / "a.txt").write_text("one\n", encoding="utf-8")
    run_svn(checkout, "add", "a.txt")
    run_svn(checkout, "commit", "-m", "init")
    monkeypatch.setattr(sv, "REPO_ROOT", str(checkout))
    # Never touch a real repository: every default must resolve to this test server and folder.
    assert sv.repo_url() == url and sv.worktrees_dir() == str(tmp_path / "main.worktrees")
    return checkout


def edit_and_commit(wc, name, text, message="change"):
    path = wc / name
    new = not path.exists()
    path.write_text(text, encoding="utf-8")
    if new:
        run_svn(wc, "add", name)
    return sv.commit(str(wc), message)


class TestWorktrees:
    def test_add_list_remove(self, main, tmp_path):
        path = sv.wt_add("agent-1")
        assert path == str(tmp_path / "main.worktrees" / "agent-1")
        assert sv.line_of(path) == "^/trunk"
        assert [(w["name"], w["line"], w["changes"]) for w in sv.wt_list()] == [("agent-1", "^/trunk", 0)]
        assert sv.wt_remove("agent-1") == os.path.realpath(path)
        assert not os.path.exists(path)

    def test_remove_refuses_uncommitted_work(self, main):
        path = sv.wt_add("agent-2")
        with open(os.path.join(path, "new.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        with pytest.raises(sv.SvError, match="uncommitted changes"):
            sv.wt_remove("agent-2")
        assert os.path.exists(path)
        sv.wt_remove("agent-2", force=True)
        assert not os.path.exists(path)

    def test_remove_refuses_paths_outside_the_worktrees_folder(self, main):
        with pytest.raises(sv.SvError, match="refusing"):
            sv.wt_remove(str(main))
        assert main.exists()

    def test_invalid_names(self, main):
        for name in ("../x", "A", "", "a b"):
            with pytest.raises(sv.SvError, match="invalid name"):
                sv.wt_add(name)

    def test_worktrees_dir_from_inside_a_worktree(self, main, tmp_path):
        path = sv.wt_add("agent-3")
        assert sv.worktrees_dir(path) == str(tmp_path / "main.worktrees")


class TestBranchFlow:
    def test_create_fails_on_other_svn_info_errors(self, main, monkeypatch):
        # Only "not found" means absent; any other failure must not fall through to svn copy.
        monkeypatch.setattr(sv, "repo_url", lambda path=None: "nosuchscheme://localhost/repo")
        with pytest.raises(sv.SvError, match="cannot check whether branch"):
            sv.branch_create("t011-other", None)

    def test_create_refuses_existing_branch(self, main):
        sv.branch_create("t010-dup", None)
        head = run_svn(main, "info", "--show-item", "last-changed-revision", sv.repo_url())
        with pytest.raises(sv.SvError, match=r"branch \^/branches/t010-dup already exists"):
            sv.branch_create("t010-dup", None)
        assert run_svn(main, "info", "--show-item", "last-changed-revision", sv.repo_url()) == head
        listing = run_svn(main, "list", f"{sv.repo_url()}/branches/t010-dup").split()
        assert "trunk/" not in listing and "a.txt" in listing
        assert sv.main(["branch", "create", "t010-dup"]) == 1

    @pytest.mark.parametrize("use_mucc", [True, False])
    def test_create_race_never_nests(self, main, monkeypatch, use_mucc):
        # The branch appears between the existence check and the copy (another session was faster).
        if use_mucc and shutil.which("svnmucc") is None:
            pytest.skip("svnmucc not installed")
        if not use_mucc:
            monkeypatch.setattr(sv.shutil, "which", lambda _name: None)
        url, name = sv.repo_url(), "t040-race"
        real_svn = sv.svn
        raced = []

        def racing_svn(*args, **kwargs):
            result = real_svn(*args, **kwargs)
            if args[:1] == ("info",) and args[1].endswith(f"/branches/{name}") and not raced:
                raced.append(True)  # the probe said "absent"; now the other session creates it
                real_svn("copy", f"{url}/trunk", f"{url}/branches/{name}", "-m", "other session")
            return result

        monkeypatch.setattr(sv, "svn", racing_svn)
        with pytest.raises(sv.SvError, match=r"branch \^/branches/t040-race already exists"):
            sv.branch_create(name, None)
        assert raced
        listing = run_svn(main, "list", f"{url}/branches/{name}").split()
        assert "trunk/" not in listing and "a.txt" in listing

    def test_full_flow(self, main):
        assert sv.branch_create("t001-x", None) == "^/branches/t001-x"
        assert sv.branch_list() == ["t001-x"]
        wt = sv.wt_add("agent-4")
        assert sv.switch("t001-x", wt) == "^/branches/t001-x"
        rev = edit_and_commit(main.parent / "main.worktrees" / "agent-4", "b.txt", "branch work\n", "T001: b")
        assert rev.startswith("r")
        d = sv.diff("t001-x")
        assert "b.txt" in d and "branch work" in d and "WARNING" not in d
        landed = sv.land("t001-x", "T001: add b")
        assert landed.startswith("r")
        assert (main / "b.txt").read_text(encoding="utf-8") == "branch work\n"
        assert sv.branch_list() == []
        assert "T001: add b" in run_svn(main, "log", "-l", "1")

    def test_sync_brings_trunk_into_the_branch(self, main):
        sv.branch_create("t002-y", None)
        wt = main.parent / "main.worktrees" / "agent-5"
        sv.wt_add("agent-5", branch="t002-y")
        (main / "c.txt").write_text("trunk work\n", encoding="utf-8")
        run_svn(main, "add", "c.txt")
        run_svn(main, "commit", "-m", "trunk change")
        assert "WARNING" in sv.diff("t002-y")
        assert "merged trunk" in sv.sync(str(wt))
        sv.commit(str(wt), "sync with trunk")
        assert "WARNING" not in sv.diff("t002-y")
        assert sv.diff("t002-y", summarize=True).strip() == ""

    def test_commit_refused_on_trunk_in_a_worktree(self, main):
        wt = sv.wt_add("agent-6")
        with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("changed\n")
        with pytest.raises(sv.SvError, match="task branch only"):
            sv.commit(wt, "oops")

    def test_switch_refused_in_the_main_checkout(self, main):
        sv.branch_create("t003-z", None)
        with pytest.raises(sv.SvError, match="main checkout stays on trunk"):
            sv.switch("t003-z", str(main))

    def test_land_refuses_local_changes_and_conflicts(self, main):
        sv.branch_create("t004-c", None)
        wt = main.parent / "main.worktrees" / "agent-7"
        sv.wt_add("agent-7", branch="t004-c")
        edit_and_commit(wt, "a.txt", "branch\n")
        (main / "a.txt").write_text("local\n", encoding="utf-8")
        with pytest.raises(sv.SvError, match="local changes"):
            sv.land("t004-c", "x")
        run_svn(main, "commit", "-m", "trunk edits a.txt")
        with pytest.raises(sv.SvError, match="conflicts; the merge was reverted"):
            sv.land("t004-c", "x")
        assert sv.changes(str(main), unversioned=False) == []
        assert sv.branch_list() == ["t004-c"]

    def test_unversioned_files_do_not_block_landing(self, main):
        sv.branch_create("t005-u", None)
        wt = main.parent / "main.worktrees" / "agent-u"
        sv.wt_add("agent-u", branch="t005-u")
        edit_and_commit(wt, "d.txt", "branch\n")
        (main / "stray.log").write_text("x", encoding="utf-8")
        assert sv.land("t005-u", "T005: d").startswith("r")
        assert (main / "d.txt").exists()
        assert sv.changes(str(main)) == ["?       " + str(main / "stray.log")]


class TestTasksExcluded:
    """Task files change on trunk only: sync and land never move AI/Tasks, whatever happened to it."""

    @staticmethod
    def setup_board(main):
        for d in ("AI/Tasks/01-Refinement", "AI/Tasks/02-Ready", "AI/Tasks/03-Doing"):
            (main / d).mkdir(parents=True, exist_ok=True)
        (main / "AI/Tasks/01-Refinement/T1.md").write_text("task 1\n", encoding="utf-8")
        (main / "AI/Tasks/01-Refinement/T2.md").write_text("task 2\n", encoding="utf-8")
        (main / "AI/Context.md").write_text("ctx\n", encoding="utf-8")
        run_svn(main, "add", "--force", "AI")
        run_svn(main, "commit", "-m", "board")

    def test_sync_does_not_pull_task_moves(self, main):
        self.setup_board(main)
        sv.branch_create("t020-s", None)
        wt = main.parent / "main.worktrees" / "agent-s"
        sv.wt_add("agent-s", branch="t020-s")
        run_svn(main, "move", "AI/Tasks/01-Refinement/T1.md", "AI/Tasks/02-Ready/T1.md")
        (main / "AI/Tasks/01-Refinement/T3.md").write_text("new\n", encoding="utf-8")
        run_svn(main, "add", "AI/Tasks/01-Refinement/T3.md")
        (main / "AI/Context.md").write_text("ctx2\n", encoding="utf-8")
        (main / "c.txt").write_text("trunk\n", encoding="utf-8")
        run_svn(main, "add", "c.txt")
        run_svn(main, "commit", "-m", "trunk: board and code")
        assert "merged trunk" in sv.sync(str(wt))
        assert (wt / "c.txt").exists() and (wt / "AI/Context.md").read_text(encoding="utf-8") == "ctx2\n"
        assert (wt / "AI/Tasks/01-Refinement/T1.md").exists() and not (wt / "AI/Tasks/02-Ready/T1.md").exists()
        assert not (wt / "AI/Tasks/01-Refinement/T3.md").exists()
        assert not any("AI/Tasks" in line.replace("\\", "/") for line in sv.changes(str(wt)))
        sv.commit(str(wt), "sync with trunk")
        assert "WARNING" not in sv.diff("t020-s")
        assert sv.diff("t020-s", summarize=True).strip() == ""

    def test_sync_with_only_task_changes_leaves_just_mergeinfo(self, main):
        self.setup_board(main)
        sv.branch_create("t021-o", None)
        wt = main.parent / "main.worktrees" / "agent-o"
        sv.wt_add("agent-o", branch="t021-o")
        run_svn(main, "move", "AI/Tasks/01-Refinement/T1.md", "AI/Tasks/02-Ready/T1.md")
        run_svn(main, "commit", "-m", "trunk: board only")
        sv.sync(str(wt))
        sv.commit(str(wt), "sync with trunk")
        assert "WARNING" not in sv.diff("t021-o")

    def test_land_with_stale_task_files_on_the_branch(self, main):
        self.setup_board(main)
        sv.branch_create("t022-l", None)
        wt = main.parent / "main.worktrees" / "agent-l"
        sv.wt_add("agent-l", branch="t022-l")
        # the branch touches task files (an old workflow, or a stale copy) and code
        (wt / "AI/Tasks/01-Refinement/T1.md").write_text("branch edit\n", encoding="utf-8")
        run_svn(wt, "delete", "AI/Tasks/01-Refinement/T2.md")
        (wt / "AI/Tasks/03-Doing/T9.md").write_text("branch new\n", encoding="utf-8")
        run_svn(wt, "add", "AI/Tasks/03-Doing/T9.md")
        edit_and_commit(wt, "b.txt", "code\n", "T022: b")
        # meanwhile trunk moved T1 and T2 (would be tree conflicts) and changed other things
        run_svn(main, "move", "AI/Tasks/01-Refinement/T1.md", "AI/Tasks/02-Ready/T1.md")
        run_svn(main, "move", "AI/Tasks/01-Refinement/T2.md", "AI/Tasks/03-Doing/T2.md")
        run_svn(main, "commit", "-m", "trunk: board moves")
        assert sv.land("t022-l", "T022: land").startswith("r")
        assert (main / "b.txt").exists()
        assert (main / "AI/Tasks/02-Ready/T1.md").read_text(encoding="utf-8") == "task 1\n"
        assert (main / "AI/Tasks/03-Doing/T2.md").exists()
        assert not (main / "AI/Tasks/01-Refinement/T1.md").exists()
        assert not (main / "AI/Tasks/03-Doing/T9.md").exists()
        assert sv.changes(str(main)) == []
        assert sv.branch_list() == []

    def moved_task_board(self, main, branch, wt_name):
        """Branch made while T1 sits in 01-Refinement; trunk then moves it on, back and on again, edits it, and
        moves T2 too (the shape that made svn 1.14 crash in its interactive tree-conflict resolver)."""
        self.setup_board(main)
        sv.branch_create(branch, None)
        wt = main.parent / "main.worktrees" / wt_name
        sv.wt_add(wt_name, branch=branch)
        (wt / "AI/Tasks/01-Refinement/T1.md").write_text("branch edit\n", encoding="utf-8")
        edit_and_commit(wt, "b.txt", "code\n", "T: b")
        t = "AI/Tasks/"
        run_svn(main, "move", t + "01-Refinement/T1.md", t + "02-Ready/T1.md")
        run_svn(main, "commit", "-m", "trunk: T1 to ready")
        run_svn(main, "move", t + "02-Ready/T1.md", t + "03-Doing/T1.md")
        run_svn(main, "commit", "-m", "trunk: T1 to doing")
        run_svn(main, "move", t + "03-Doing/T1.md", t + "01-Refinement/T1.md")
        run_svn(main, "commit", "-m", "trunk: T1 back")
        (main / t / "01-Refinement/T1.md").write_text("trunk edit\n", encoding="utf-8")
        run_svn(main, "move", t + "01-Refinement/T2.md", t + "03-Doing/T2.md")
        (main / t / "02-Ready/T7.md").write_text("new\n", encoding="utf-8")
        run_svn(main, "add", t + "02-Ready/T7.md")
        run_svn(main, "commit", "-m", "trunk: edit, T2 to doing, T7 new")
        return wt

    def test_sync_survives_moved_task_files(self, main):
        wt = self.moved_task_board(main, "t023-m", "agent-m")
        out = sv.sync(str(wt))
        assert "merged trunk" in out, out
        assert not any("AI/Tasks" in line.replace("\\", "/") for line in sv.changes(str(wt)))
        assert (wt / "AI/Tasks/01-Refinement/T2.md").exists() and not (wt / "AI/Tasks/02-Ready/T7.md").exists()
        sv.commit(str(wt), "sync with trunk")
        assert "WARNING" not in sv.diff("t023-m")

    def test_land_survives_moved_task_files(self, main):
        self.moved_task_board(main, "t024-m", "agent-m2")
        assert sv.land("t024-m", "T024: land").startswith("r")
        assert (main / "b.txt").exists()
        assert (main / "AI/Tasks/03-Doing/T2.md").exists() and (main / "AI/Tasks/02-Ready/T7.md").exists()
        assert (main / "AI/Tasks/01-Refinement/T1.md").read_text(encoding="utf-8") == "trunk edit\n"
        assert sv.changes(str(main)) == []

    def test_unversioned_task_files_survive_a_merge(self, main):
        self.setup_board(main)
        sv.branch_create("t025-u", None)
        wt = main.parent / "main.worktrees" / "agent-u"
        sv.wt_add("agent-u", branch="t025-u")
        edit_and_commit(wt, "b.txt", "code\n", "T: b")
        (main / "AI/Tasks/01-Refinement/Draft.md").write_text("draft\n", encoding="utf-8")
        sv.land("t025-u", "T025: land")
        assert (main / "AI/Tasks/01-Refinement/Draft.md").exists()

    @staticmethod
    def conflicting_board(main, branch, wt_name):
        """Every tree-conflict kind svn postpones: local missing + incoming delete/move and edit, local obstruction
        + incoming add."""
        TestTasksExcluded.setup_board(main)
        t = "AI/Tasks/"
        (main / t / "01-Refinement/T3.md").write_text("task 3\n", encoding="utf-8")
        run_svn(main, "add", t + "01-Refinement/T3.md")
        run_svn(main, "commit", "-m", "T3")
        sv.branch_create(branch, None)
        wt = main.parent / "main.worktrees" / wt_name
        sv.wt_add(wt_name, branch=branch)
        run_svn(wt, "delete", t + "01-Refinement/T2.md")
        run_svn(wt, "move", t + "01-Refinement/T3.md", t + "03-Doing/T3.md")
        (wt / t / "02-Ready/T9.md").write_text("branch\n", encoding="utf-8")
        run_svn(wt, "add", t + "02-Ready/T9.md")
        edit_and_commit(wt, "b.txt", "code\n", "T: b")
        run_svn(main, "move", t + "01-Refinement/T2.md", t + "02-Ready/T2.md")
        (main / t / "01-Refinement/T3.md").write_text("trunk edit\n", encoding="utf-8")
        (main / t / "02-Ready/T9.md").write_text("trunk\n", encoding="utf-8")
        run_svn(main, "add", t + "02-Ready/T9.md")
        (main / t / "01-Refinement/Draft.md").write_text("draft\n", encoding="utf-8")
        run_svn(main, "commit", "-m", "trunk: board", t + "01-Refinement/T3.md", t + "02-Ready", t + "01-Refinement/T2.md")
        return wt

    def test_sync_clears_postponed_tree_conflicts(self, main):
        wt = self.conflicting_board(main, "t026-c", "agent-c")
        out = sv.sync(str(wt))
        assert "CONFLICTS" not in out, out
        assert not any("AI" + os.sep + "Tasks" in line for line in sv.changes(str(wt)))
        assert not [line for line in run_svn(wt, "status").splitlines() if line[:7].strip() and "C" in line[:7]]
        assert (wt / "AI/Tasks/03-Doing/T3.md").exists() and not (wt / "AI/Tasks/02-Ready/T2.md").exists()

    def test_land_clears_postponed_tree_conflicts(self, main):
        self.conflicting_board(main, "t027-c", "agent-c2")
        assert sv.land("t027-c", "T027: land").startswith("r")
        assert (main / "AI/Tasks/02-Ready/T2.md").exists() and (main / "b.txt").exists()
        assert not (main / "AI/Tasks/03-Doing/T3.md").exists()
        assert (main / "AI/Tasks/01-Refinement/Draft.md").exists()
        assert run_svn(main, "status").strip() == "?       AI" + os.sep + "Tasks" + os.sep + "01-Refinement" + os.sep + "Draft.md"

    def test_land_of_task_only_branch_leaves_trunk_files_alone(self, main):
        self.setup_board(main)
        sv.branch_create("t023-n", None)
        wt = main.parent / "main.worktrees" / "agent-n"
        sv.wt_add("agent-n", branch="t023-n")
        edit_and_commit(wt, "AI/Tasks/01-Refinement/T1.md", "branch edit\n")
        assert sv.land("t023-n", "T023: land").startswith("r")  # only svn:mergeinfo is committed
        assert (main / "AI/Tasks/01-Refinement/T1.md").read_text(encoding="utf-8") == "task 1\n"
        assert sv.changes(str(main)) == []

    def test_real_code_conflict_still_stops(self, main):
        self.setup_board(main)
        sv.branch_create("t024-c", None)
        wt = main.parent / "main.worktrees" / "agent-c"
        sv.wt_add("agent-c", branch="t024-c")
        edit_and_commit(wt, "a.txt", "branch\n")
        (wt / "AI/Tasks/01-Refinement/T1.md").write_text("stale\n", encoding="utf-8")
        sv.commit(str(wt), "stale task")
        run_svn(main, "move", "AI/Tasks/01-Refinement/T1.md", "AI/Tasks/02-Ready/T1.md")
        (main / "a.txt").write_text("trunk\n", encoding="utf-8")
        run_svn(main, "commit", "-m", "trunk edits a.txt and moves T1")
        with pytest.raises(sv.SvError, match="conflicts; the merge was reverted"):
            sv.land("t024-c", "x")
        assert sv.changes(str(main), unversioned=False) == []
        assert (main / "AI/Tasks/02-Ready/T1.md").exists()

    def test_sync_survives_text_conflict_on_a_stale_task_file(self, main):
        # svn stops a multi-revision merge at the first conflict; the task conflict must not hide later revisions.
        self.setup_board(main)
        sv.branch_create("t026-t", None)
        wt = main.parent / "main.worktrees" / "agent-t"
        sv.wt_add("agent-t", branch="t026-t")
        edit_and_commit(wt, "AI/Tasks/01-Refinement/T1.md", "stale branch copy\n")
        (main / "AI/Tasks/01-Refinement/T1.md").write_text("trunk edit\n", encoding="utf-8")
        run_svn(main, "commit", "-m", "trunk: task edit")
        (main / "c.txt").write_text("later code\n", encoding="utf-8")
        run_svn(main, "add", "c.txt")
        run_svn(main, "commit", "-m", "trunk: code after the task edit")
        assert "merged trunk" in sv.sync(str(wt))
        assert (wt / "c.txt").exists()
        assert not any("C" in line[:7] for line in sv.changes(str(wt), unversioned=False))
        sv.commit(str(wt), "sync with trunk")
        assert "WARNING" not in sv.diff("t026-t")

    def test_land_reverts_a_failed_merge(self, main, monkeypatch):
        sv.branch_create("t027-f", None)
        wt = main.parent / "main.worktrees" / "agent-f"
        sv.wt_add("agent-f", branch="t027-f")
        edit_and_commit(wt, "b.txt", "code\n", "T027: b")
        real = sv.merge_without_tasks

        def failing(root, source):
            real(root, source)  # applies the merge, then something goes wrong
            raise sv.SvError("boom")

        monkeypatch.setattr(sv, "merge_without_tasks", failing)
        with pytest.raises(sv.SvError, match="boom"):
            sv.land("t027-f", "x")
        assert sv.changes(str(main)) == [] and not (main / "b.txt").exists()
        assert sv.branch_list() == ["t027-f"]

    def test_diff_hides_task_files(self, main):
        self.setup_board(main)
        sv.branch_create("t025-d", None)
        wt = main.parent / "main.worktrees" / "agent-d"
        sv.wt_add("agent-d", branch="t025-d")
        edit_and_commit(wt, "AI/Tasks/01-Refinement/T1.md", "branch edit\n")
        edit_and_commit(wt, "AI/Context.md", "ctx branch\n")
        edit_and_commit(wt, "b.txt", "code\n")
        summary = sv.diff("t025-d", summarize=True)
        assert "AI/Tasks" not in summary and "b.txt" in summary and "Context.md" in summary
        full = sv.diff("t025-d")
        assert "AI/Tasks" not in full and "T1.md" not in full and "ctx branch" in full and "code" in full


class TestHooks:
    def test_create_prints_the_path_last(self, main, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"name": "Agent A1b2", "cwd": str(main)})))
        assert sv.main(["hook", "worktree-create"]) == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert out[-1] == str(main.parent / "main.worktrees" / "agent-a1b2")
        assert os.path.isdir(out[-1])

    def test_remove_keeps_changed_worktrees(self, main):
        path = sv.wt_add("agent-8")
        with open(os.path.join(path, "a.txt"), "w", encoding="utf-8") as f:
            f.write("changed\n")
        assert sv.hook_worktree_remove({"worktree_path": path}) == 1
        assert os.path.isdir(path)
        sv.wt_remove("agent-8", force=True)
        path = sv.wt_add("agent-9")
        assert sv.hook_worktree_remove({"worktree_path": path}) == 0
        assert not os.path.exists(path)

    @pytest.mark.parametrize("command, blocked", [
        ("svn commit -m x", True),
        ("cd sub && svn ci -m x", True),
        ("svn.exe commit -m x", True),
        ("python .claude/svn/sv.py commit -m x", False),
        ("svn status", False),
        ("echo svncommit", False),
    ])
    def test_guard(self, main, command, blocked):
        wt = sv.wt_add("agent-g")
        assert sv.hook_guard({"tool_input": {"command": command}, "cwd": wt}) == (2 if blocked else 0)
        assert sv.hook_guard({"tool_input": {"command": command}, "cwd": str(main)}) == 0


class TestConsoleEncoding:
    def test_diff_with_non_ascii_text_on_a_cp1252_console(self, main, monkeypatch):
        sv.branch_create("t031-u", None)
        wt = sv.wt_add("agent-u8", branch="t031-u")
        edit_and_commit(pathlib.Path(wt), "arrow.txt", "a \u2192 b\n", "T031: arrow")
        raw_out, raw_err = io.BytesIO(), io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw_out, encoding="cp1252"))
        monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw_err, encoding="cp1252"))
        assert sv.main(["diff", "t031-u"]) == 0
        print("\u2192", file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        assert "a \u2192 b".encode("utf-8") in raw_out.getvalue()
        assert raw_err.getvalue().strip() == "\u2192".encode("utf-8")
