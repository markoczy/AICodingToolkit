"""
sv: the SVN side of the AI workflow - task branches, isolated working copies ("worktrees") for agents, review
diffs and landing. Standard library only; needs the `svn` command line client.

Layout (see README.md next to this file):
  ^/trunk                 the main line; task files and landed work
  ^/branches/<name>       one branch per task (e.g. t012-fix-login)
  <main>.worktrees/<name> working copies for agents, next to the main checkout <main>

Commands (`python .claude/svn/sv.py <command> --help`):
  branch create|delete|list    task branches on the server
  wt add|list|remove           working copies in the worktrees folder
  switch BRANCH                switch a worktree to a task branch
  sync                         merge new trunk changes into the branch working copy (AI/Tasks excluded)
  commit -m MSG                commit a branch working copy (refused on trunk inside a worktree)
  diff BRANCH                  the change a branch makes against trunk (for the reviewer)
  land BRANCH -m MSG           merge a branch into the trunk working copy, commit, delete the branch
  hook worktree-create|worktree-remove|guard
                               Claude Code hooks (JSON on stdin), configured in .claude/settings.json\n"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
SVN_COMMIT = re.compile(r"(^|[;&|(]\s*|\s)svn(\.exe)?\s+(commit|ci)\b")
SCRIPT = os.path.abspath(__file__)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT)))


# svn codes for "URL does not exist". 160013 is path-not-found; 170000 is also used for e.g. "unrecognized URL
# scheme", so it only counts together with the "non-existent" / "doesn't exist" text (file:// vs http(s):// differ).
NOT_FOUND = ("E160013", "W160013")


class SvError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------
# svn helpers


# Windows: no console window for svn children when sv runs without a console (e.g. under the board server).
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def svn(*args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        # English messages whatever the user's locale: the output is parsed ("Committed revision N", ...).
        result = subprocess.run(["svn", "--non-interactive", *args], cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                                env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    except OSError as exc:
        raise SvError(f"cannot run svn: {exc}") from exc
    if check and result.returncode != 0:
        raise SvError(f"svn {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def info(path: str, item: str) -> str | None:
    result = svn("info", "--show-item", item, path, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def repo_url(path: str | None = None) -> str:
    path = path or REPO_ROOT
    url = info(path, "repos-root-url")
    if not url:
        raise SvError(f"{path} is not an SVN working copy")
    return url


def line_of(path: str) -> str | None:
    """`^/trunk`, `^/branches/<name>`, ... for the working copy root at path."""
    return info(path, "relative-url")


def changes(path: str, unversioned: bool = True) -> list[str]:
    """`svn status` lines: local modifications, additions, deletions, conflicts (and unversioned files)."""
    out = svn("status", path).stdout
    lines = [line for line in out.splitlines() if line.strip() and not line.startswith(("X", "Performing"))]
    return lines if unversioned else [line for line in lines if not line.startswith("?")]


def check_name(name: str) -> str:
    if not NAME.match(name):
        raise SvError(f"invalid name {name!r}: lowercase letters, digits, '.', '_' and '-' only")
    return name


def worktrees_dir(root: str | None = None) -> str:
    """`<main>.worktrees` next to the main checkout; from inside a worktree, the folder that holds it."""
    root = root or REPO_ROOT
    parent = os.path.dirname(root)
    if os.path.basename(parent).endswith(".worktrees"):
        return parent
    return os.path.join(parent, os.path.basename(root) + ".worktrees")


def in_worktrees(path: str, root: str | None = None) -> bool:
    base = os.path.normcase(os.path.realpath(worktrees_dir(root)))
    target = os.path.normcase(os.path.realpath(path))
    return target.startswith(base + os.sep)


def wc_root(path: str) -> str:
    root = info(path, "wc-root")
    if not root:
        raise SvError(f"{path} is not in an SVN working copy")
    return root


def _remove_tree(path: str) -> None:
    def on_error(func, p, _exc):  # SVN marks pristine files read-only on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:
        shutil.rmtree(path, onerror=on_error)


# ---------------------------------------------------------------------------------------------------------------
# commands


def branch_create(name: str, message: str | None) -> str:
    url = repo_url()
    check_name(name)
    probe = svn("info", f"{url}/branches/{name}", check=False)
    if probe.returncode == 0:
        raise SvError(f"branch ^/branches/{name} already exists")
    err = probe.stderr
    absent = any(code in err for code in NOT_FOUND) or (
        ("W170000" in err or "E170000" in err) and ("non-existent" in err or "doesn't exist" in err))
    if not absent:  # anything but "not found"
        raise SvError(f"cannot check whether branch ^/branches/{name} exists: {probe.stderr.strip()}")
    _copy_trunk(url, name, message or f"Create branch {name} from trunk")
    return f"^/branches/{name}"


def _copy_trunk(url: str, name: str, message: str) -> None:
    """Copy trunk to the branch in one server operation that cannot nest into a branch that appeared after the
    pre-check. `svnmucc cp` fails on an existing target; without svnmucc, `svn copy` is used and the result is
    verified (a copy onto an existing folder lands in `<branch>/trunk`: that copy is removed again)."""
    target = f"{url}/branches/{name}"
    exists = f"branch ^/branches/{name} already exists (created concurrently)"
    mucc = shutil.which("svnmucc")
    if mucc:
        try:
            result = subprocess.run([mucc, "--non-interactive", "-m", message, "cp", "HEAD", f"{url}/trunk", target],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    creationflags=NO_WINDOW)
        except OSError as exc:
            raise SvError(f"cannot run svnmucc: {exc}") from exc
        if result.returncode != 0:
            if svn("info", target, check=False).returncode == 0:
                raise SvError(exists)
            raise SvError(f"svnmucc cp failed: {result.stderr.strip()}")
        return
    svn("copy", f"{url}/trunk", target, "-m", message)
    if svn("info", f"{target}/trunk", check=False).returncode == 0 and \
            svn("info", f"{url}/trunk/trunk", check=False).returncode != 0:
        svn("delete", f"{target}/trunk", "-m", f"Remove nested trunk copy created while creating branch {name}")
        raise SvError(exists)


def branch_delete(name: str, message: str | None) -> None:
    url = repo_url()
    svn("delete", f"{url}/branches/{check_name(name)}", "-m", message or f"Delete branch {name}")


def branch_list() -> list[str]:
    out = svn("list", f"{repo_url()}/branches").stdout
    return [line.rstrip("/") for line in out.splitlines() if line.strip()]


def wt_add(name: str, branch: str | None = None) -> str:
    url = repo_url()
    target = os.path.join(worktrees_dir(), check_name(name))
    if os.path.exists(target):
        raise SvError(f"{target} already exists")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    line = f"branches/{check_name(branch)}" if branch else "trunk"
    svn("checkout", f"{url}/{line}", target)
    return target


def wt_list() -> list[dict]:
    base = worktrees_dir()
    result = []
    if not os.path.isdir(base):
        return result
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name)
        if os.path.isdir(os.path.join(path, ".svn")):
            result.append({"name": name, "path": path, "line": line_of(path),
                           "revision": info(path, "revision"), "changes": len(changes(path))})
    return result


def resolve_worktree(name_or_path: str) -> str:
    path = name_or_path if os.path.isabs(name_or_path) else os.path.join(worktrees_dir(), name_or_path)
    if not in_worktrees(path):
        raise SvError(f"{path} is not inside {worktrees_dir()}; refusing to touch it")
    return os.path.realpath(path)


def wt_remove(name_or_path: str, force: bool = False) -> str:
    path = resolve_worktree(name_or_path)
    if not os.path.isdir(path):
        raise SvError(f"{path} does not exist")
    if os.path.isdir(os.path.join(path, ".svn")) and not force:
        pending = changes(path)
        if pending:
            shown = "\n  ".join(pending[:20])
            raise SvError(f"{path} has uncommitted changes; commit them or pass --force:\n  {shown}")
    _remove_tree(path)
    return path


def switch(branch: str, path: str) -> str:
    root = wc_root(path)
    if not in_worktrees(root):
        raise SvError("switch only in a worktree; the main checkout stays on trunk")
    svn("switch", f"{repo_url(root)}/branches/{check_name(branch)}", root)
    return line_of(root)


TASKS_DIR = "AI/Tasks"
TASKS_PATH = re.compile(r"(^|[/\\])AI[/\\]Tasks([/\\]|$)")


def _task_status(tasks: str) -> list[str]:
    return svn("status", tasks, check=False).stdout.splitlines()


def drop_task_changes(root: str, keep: frozenset[str] = frozenset()) -> int:
    """Task files change on trunk only (through tb). After a merge, undo everything the merge did under AI/Tasks
    (edits, moves, additions, deletions, tree conflicts) so that neither a sync pulls the board into a branch
    nor a land pushes a stale board into trunk. The merge itself stays recorded in svn:mergeinfo on the root.
    Postponed tree conflicts (local missing, local obstruction, incoming delete or move) are cleared too, and so
    are the unversioned files the merge left behind; unversioned files listed in `keep` (there before the merge)
    stay. Returns the number of task paths dropped."""
    tasks = os.path.join(root, *TASKS_DIR.split("/"))
    out = "\n".join(_task_status(tasks))
    # Files the merge added become unversioned again after the revert; remember them to delete them.
    added = [line[8:].strip() for line in out.splitlines() if line[:1] in ("A", "R") or line[:1] == "?" and
             "C" in line[:7]]
    dropped = len([line for line in out.splitlines() if line.strip() and not line.startswith("?")])
    if dropped:
        svn("revert", "-R", tasks, check=False)
        # a conflict entry that survived the revert (missing victim): mark it resolved, revert once more
        if any("C" in line[:7] for line in _task_status(tasks)):
            svn("resolve", "-R", "--accept", "working", tasks, check=False)
            svn("revert", "-R", tasks, check=False)
        for path in sorted(added, reverse=True):
            if os.path.normcase(os.path.abspath(path)) in keep:
                continue
            if os.path.isdir(path):
                if not os.listdir(path):
                    os.rmdir(path)
            elif os.path.isfile(path) and info(path, "kind") is None:
                os.remove(path)
        # directories the merge added hold only those files; remove the empty leftovers
        for path in sorted(added, reverse=True):
            if os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
    return dropped


def _unversioned_tasks(root: str) -> frozenset[str]:
    tasks = os.path.join(root, *TASKS_DIR.split("/"))
    return frozenset(os.path.normcase(os.path.abspath(line[8:].strip())) for line in _task_status(tasks)
                     if line.startswith("?"))


def merge_without_tasks(root: str, source: str) -> str:
    """`svn merge source` into root, then drop the AI/Tasks part. The merge runs with `--non-interactive --accept
    postpone`: a tree conflict on a moved task file is postponed instead of sending svn 1.14 into its interactive
    resolver (crash: access violation, or E235000 token.c). svn may still stop with E155015 (or E235000) on a
    task-file conflict before the remaining revisions are applied: drop it and rerun until the merge completes.
    A conflict anywhere else is left for the caller to report."""
    out = ""
    keep = _unversioned_tasks(root)
    retryable = ("E155015", "E235000")
    for _ in range(50):
        result = svn("merge", "--non-interactive", "--accept", "postpone", source, root, check=False)
        out += result.stdout
        drop_task_changes(root, keep)
        if result.returncode == 0:
            return out
        if not any(code in result.stderr for code in retryable) or any(
                "C" in line[:7] for line in changes(root, unversioned=False)):
            if result.returncode != 0 and not any(code in result.stderr for code in retryable):
                raise SvError(f"svn merge {source} failed:\n{result.stderr.strip()}")
            return out  # conflicts outside AI/Tasks: reported by the caller
    raise SvError("merge did not complete: too many task-file conflicts")


def revert_merge(root: str) -> None:
    """Undo a half-applied merge in a clean working copy: revert everything and delete the files it added."""
    added = [line[8:].strip() for line in svn("status", root, check=False).stdout.splitlines()
             if line[:1] in "AR" and line[:1].strip()]
    svn("revert", "-R", root, check=False)
    for path in sorted(added, reverse=True):
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)


def require_branch_wc(path: str) -> str:
    root = wc_root(path)
    line = line_of(root) or ""
    if not line.startswith("^/branches/"):
        raise SvError(f"{root} is a working copy of {line or '?'}, not of a task branch; "
                      f"use `sv switch <branch>` first")
    return root


def sync(path: str) -> str:
    root = require_branch_wc(path)
    if changes(root, unversioned=False):
        raise SvError("commit or revert local changes before syncing")
    svn("update", root)
    out = merge_without_tasks(root, f"{repo_url(root)}/trunk")
    conflicts = [line for line in changes(root, unversioned=False) if "C" in line[:7]]
    if conflicts:
        return "CONFLICTS - resolve them, run the tests, then `sv commit`:\n" + "\n".join(conflicts)
    if not changes(root, unversioned=False):
        return "branch is up to date with trunk; nothing to commit"
    return out.strip() + "\nmerged trunk; run the tests, then `sv commit -m \"sync with trunk\"`"


def commit(path: str, message: str) -> str:
    root = wc_root(path)
    line = line_of(root) or ""
    if in_worktrees(root) and not line.startswith("^/branches/"):
        raise SvError(f"refused: this worktree is on {line}; agents commit to their task branch only "
                      f"(`sv switch <branch>`)")
    if not message.strip():
        raise SvError("a commit message is needed (-m)")
    out = svn("commit", root, "-m", message).stdout
    m = re.search(r"Committed revision (\d+)", out)
    return f"r{m.group(1)}" if m else "nothing to commit"


def diff(branch: str, summarize: bool = False) -> str:
    url = repo_url()
    name = check_name(branch)
    missing = svn("mergeinfo", "--show-revs", "eligible", f"{url}/trunk", f"{url}/branches/{name}").stdout.split()
    args = ["diff", "--old", f"{url}/trunk", "--new", f"{url}/branches/{name}"]
    # svn:mergeinfo is SVN's merge bookkeeping, not part of the change; other property changes are shown.
    props = svn(*args, "--properties-only").stdout
    real_props = [p for p in re.findall(r"^(?:Added|Modified|Deleted): (\S+)", props, re.M) if p != "svn:mergeinfo"]
    if summarize:
        out = "".join(line + "\n" for line in svn(*args, "--summarize", "--ignore-properties").stdout.splitlines()
                      if not TASKS_PATH.search(line[8:]))
        if real_props:
            out += f"(property changes: {', '.join(sorted(set(real_props)))})\n"
    else:
        blocks = re.split(r"(?m)^(?=Index: )", svn(*args, "--ignore-properties").stdout)
        out = "".join(b for b in blocks if not TASKS_PATH.match(b[7:].strip().split("\n", 1)[0]))
        if real_props:
            out += props
    if missing:
        out = (f"WARNING: trunk revisions {', '.join(missing)} are not merged into the branch; the diff also shows "
               f"them reversed. Sync the branch first (`sv sync`).\n") + out
    return out


def land(branch: str, message: str, keep_branch: bool = False) -> str:
    root = wc_root(REPO_ROOT)
    url = repo_url(root)
    name = check_name(branch)
    if line_of(root) != "^/trunk":
        raise SvError(f"land runs in the main trunk checkout; {root} is on {line_of(root)}")
    if changes(root, unversioned=False):
        raise SvError("the trunk working copy has local changes; commit or revert them first (task files: commit "
                      "them with their own `tasks: ...` message)")
    svn("update", root)
    try:
        merge_without_tasks(root, f"{url}/branches/{name}")
    except SvError:
        revert_merge(root)
        raise
    pending = changes(root, unversioned=False)
    if any("C" in line[:7] for line in pending):
        revert_merge(root)
        raise SvError("merge conflicts; the merge was reverted. Sync the branch with trunk (`sv sync` in its "
                      "worktree), resolve, commit, and land again:\n" + "\n".join(pending))
    if not pending:
        raise SvError(f"branch {name} brings no changes")
    out = svn("commit", root, "-m", message).stdout
    m = re.search(r"Committed revision (\d+)", out)
    rev = f"r{m.group(1)}" if m else "?"
    if not keep_branch:
        branch_delete(name, f"Delete branch {name}, landed in {rev}")
    return rev


# ---------------------------------------------------------------------------------------------------------------
# Claude Code hooks


def hook_worktree_create(data: dict) -> int:
    name = re.sub(r"[^a-z0-9._-]+", "-", str(data.get("name", "")).lower()).strip("-.") or "agent"
    path = wt_add(name[:80])
    print(f"sv: checked out ^/trunk into {path}", file=sys.stderr)
    print(path)
    return 0


def hook_worktree_remove(data: dict) -> int:
    try:
        path = wt_remove(str(data.get("worktree_path", "")))
    except SvError as exc:
        print(f"sv: kept the worktree: {exc}", file=sys.stderr)
        return 1
    print(f"sv: removed {path}", file=sys.stderr)
    return 0


def hook_guard(data: dict) -> int:
    """PreToolUse (Bash/PowerShell): inside a worktree, commits go through `sv commit` (which refuses trunk)."""
    command = str((data.get("tool_input") or {}).get("command", ""))
    cwd = str(data.get("cwd") or "")
    if cwd and in_worktrees(cwd) and SVN_COMMIT.search(command):
        print("Blocked: in an agent worktree, commit with `python .claude/svn/sv.py commit -m \"...\"`; it commits "
              "to the task branch only and refuses trunk.", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------------------------------------------------------
# CLI


def _use_utf8_output() -> None:
    """svn output is decoded as UTF-8; a legacy console codepage (cp1252) must not crash printing it."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    parser = argparse.ArgumentParser(prog="sv", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("branch", help="task branches")
    bsub = p.add_subparsers(dest="action", required=True)
    q = bsub.add_parser("create", help="copy ^/trunk to ^/branches/NAME")
    q.add_argument("name")
    q.add_argument("-m", "--message")
    q = bsub.add_parser("delete", help="delete ^/branches/NAME")
    q.add_argument("name")
    q.add_argument("-m", "--message")
    bsub.add_parser("list", help="list ^/branches")

    p = sub.add_parser("wt", help="agent working copies")
    wsub = p.add_subparsers(dest="action", required=True)
    q = wsub.add_parser("add", help="check out trunk (or --branch) into the worktrees folder; prints the path")
    q.add_argument("name")
    q.add_argument("--branch")
    wsub.add_parser("list", help="worktrees with their line, revision and number of local changes")
    q = wsub.add_parser("remove", help="delete a worktree (refused with uncommitted changes unless --force)")
    q.add_argument("name_or_path")
    q.add_argument("--force", action="store_true")

    p = sub.add_parser("switch", help="switch a worktree to ^/branches/BRANCH")
    p.add_argument("branch")
    p.add_argument("--path", default=".")

    p = sub.add_parser("sync", help="update and merge ^/trunk into a branch working copy")
    p.add_argument("--path", default=".")

    p = sub.add_parser("commit", help="commit a branch working copy (refused on trunk inside a worktree)")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--path", default=".")

    p = sub.add_parser("diff", help="what ^/branches/BRANCH changes against ^/trunk")
    p.add_argument("branch")
    p.add_argument("--summarize", action="store_true")

    p = sub.add_parser("land", help="merge a branch into the main trunk checkout, commit, delete the branch")
    p.add_argument("branch")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--keep-branch", action="store_true")

    p = sub.add_parser("hook", help="Claude Code hooks (JSON on stdin)")
    p.add_argument("event", choices=["worktree-create", "worktree-remove", "guard"])

    args = parser.parse_args(argv)
    try:
        if args.cmd == "hook":
            raw = sys.stdin.read()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except ValueError as exc:
                raise SvError(f"hook input is not JSON: {exc}") from exc
            return {"worktree-create": hook_worktree_create, "worktree-remove": hook_worktree_remove,
                    "guard": hook_guard}[args.event](data)
        if args.cmd == "branch":
            if args.action == "create":
                print(branch_create(args.name, args.message))
            elif args.action == "delete":
                branch_delete(args.name, args.message)
                print(f"deleted ^/branches/{args.name}")
            else:
                print("\n".join(branch_list()))
        elif args.cmd == "wt":
            if args.action == "add":
                print(wt_add(args.name, args.branch))
            elif args.action == "remove":
                print(f"removed {wt_remove(args.name_or_path, args.force)}")
            else:
                for w in wt_list():
                    print(f"{w['name']:<32} {w['line'] or '?':<40} r{w['revision']}  {w['changes']} change(s)")
        elif args.cmd == "switch":
            print(f"now on {switch(args.branch, args.path)}")
        elif args.cmd == "sync":
            print(sync(args.path))
        elif args.cmd == "commit":
            print(commit(args.path, args.message))
        elif args.cmd == "diff":
            sys.stdout.write(diff(args.branch, args.summarize))
        elif args.cmd == "land":
            print(f"landed {args.branch} in trunk {land(args.branch, args.message, args.keep_branch)}")
        return 0
    except SvError as exc:
        print(f"sv: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
