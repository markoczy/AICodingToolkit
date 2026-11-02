"""
Version control backends of the task board: everything that depends on git or SVN lives in `git.py` and `svn.py`,
behind the one interface below (`Backend`); the rest of the package (store, update, watch, commit, cli, web)
never runs a version control command itself. The backend is chosen by `vcs` in board.json (`backend(config)`).

A backend owns:

- the main-line guard (`main_branch_block`: task files change only in a checkout of `main_branch`), whether a
  file is versioned (`is_versioned`) and the versioned move of a task file (`move_file`);
- the availability check (`available`) and the environment in which nothing can prompt (`env`);
- the periodic update (`update`, the rules are in each module's docstring), the local revision (`revision`),
  the error-line parser (`first_error`), the conflicts of the update paths (`conflicted`), whether an update by
  anyone runs right now (`update_running`) and the note printed when that lasts too long (`lock_note`);
- the watcher's refinements of INCOMING: `own_commits_only` (a revision move made only of this checkout's own
  commits is no input) and `changed_tasks` (only tasks whose files changed between two revisions count). git
  implements them; svn answers "unknown" (False / None), so every new key counts;
- board commits (`commit_file`, `file_is_clean`, `revert_file`, `update_file`, commit.py): svn commits the one
  file to the repository and redoes the edit on the incoming version when the file is out of date; git commits
  locally (no push), so a commit is never out of date and the update pause for unpushed commits applies.

The review header field (`pr` for git, `rev` for svn) is a `ReviewField`; `review_field(config)` picks it.
"""

import os
import re
from dataclasses import dataclass, field
from typing import NamedTuple

NAMES = ("git", "svn")
SHORT = 7  # length of a short git commit id


class ReviewField(NamedTuple):
    """The header field that names what is handed to the code review: a pull request (git) or a revision (svn)."""
    name: str               # the header key: pr | rev
    flag: str               # the `tb move` option: --pr | --rev
    what: str               # for messages: "a pull request number" | "a revision number"
    missing: str            # the move precondition message into 04-CodeReview
    landing_example: str    # a --reason example for landing from review
    pattern: str            # what the CLI accepts: "#77" / "77" or "r12" / "12"
    strip: str              # the prefix characters stripped before int()
    prefix: str             # how a value is shown: "PR #" | "r"
    detail_prefix: str      # before the label in a move's Log line: "" (`PR #77`) | "review " (`review r12`)

    def label(self, value) -> str:
        """`PR #77` / `r12` for a set value, "" for none."""
        return f"{self.prefix}{value}" if value else ""

    def detail(self, value) -> str:
        """The move's Log detail: `PR #77` / `review r12`."""
        return f"{self.detail_prefix}{self.label(value)}"

    def parse(self, raw: str) -> int:
        """The number in raw (`#77`, `77` / `r12`, `12`); ValueError when it is none."""
        value = raw.strip()
        if not re.fullmatch(self.pattern, value):
            raise ValueError(f"{self.name} must be {self.what}, got {raw!r}")
        return int(value.lstrip(self.strip))


REVIEW_FIELDS = {
    "git": ReviewField("pr", "--pr", "a pull request number", "no pull request for the review (--pr)",
                       "merged PR #77 into master, reviewer approved", r"#?0*[1-9]\d*", "#", "PR #", ""),
    "svn": ReviewField("rev", "--rev", "a revision number", "no branch revision for the review (--rev)",
                       "merged into trunk r57, reviewer approved", r"r?0*[1-9]\d*", "r", "r", "review "),
}
DEFAULT_REVIEW = REVIEW_FIELDS["git"]


@dataclass
class UpdateResult:
    """
    One update attempt. status: ok | changed | conflict | diverged | dirty-skip | off-branch | busy | error |
    no-vcs | off (`diverged` and `off-branch` only happen with git). revision: the short local revision (git: the
    short HEAD commit, svn: the working-copy revision of the first update path).
    """
    status: str
    revision: object = None
    revision_before: object = None
    head: str | None = None                          # git: the full HEAD commit on the main branch (the watcher's)
    changed: list = field(default_factory=list)      # [(status letter, repo-relative path)]
    conflicts: list = field(default_factory=list)    # [repo-relative path]
    dirty: list = field(default_factory=list)        # [repo-relative path]
    message: str = ""


class Backend:
    """The interface (module docstring); `git.GitBackend` and `svn.SvnBackend` implement it."""

    name = ""
    default_main_branch = ""
    review: ReviewField = DEFAULT_REVIEW
    lock_note = ""
    update_verb = ""        # what an update is called in messages: "git pull" | "svn update"
    commit_by_hand = ""     # how the coordinator commits a saved, uncommitted board write
    supports_redo = False   # the out-of-date redo of a board commit (commit.py)

    # ---- checks and guards ----

    def available(self) -> bool:
        raise NotImplementedError

    def env(self) -> dict:
        """The environment of every command: nothing may prompt, messages are untranslated."""
        raise NotImplementedError

    def main_branch_block(self, root_path: str, main_branch: str) -> str | None:
        """Why task files under root_path can't be written now, or None."""
        raise NotImplementedError

    def is_versioned(self, path: str) -> bool:
        raise NotImplementedError

    def move_file(self, repo_root: str, src: str, dst: str) -> str:
        """Move a task file with the version control's rename (versioned files) or a plain rename; the caller
        holds the write lock and has checked that dst does not exist. Returns how it was moved."""
        raise NotImplementedError

    # ---- the periodic update ----

    def update(self, config: dict, lock_wait: float = 5.0) -> UpdateResult:
        raise NotImplementedError

    def revision(self, config: dict):
        """The short local revision, or None."""
        raise NotImplementedError

    def first_error(self, stderr: str) -> str:
        raise NotImplementedError

    def conflicted(self, config: dict) -> list[str]:
        """Conflicted paths in the update paths (local only)."""
        raise NotImplementedError

    def update_running(self, config: dict) -> bool:
        """True while an update (or a merge) by anyone holds the checkout's lock."""
        raise NotImplementedError

    def local_revision(self, config: dict):
        """The revision the watcher tracks between polls (local only); None when it can't be read, or when a
        move of it is no input (git: off the main branch)."""
        raise NotImplementedError

    def own_commits_only(self, config: dict, old, new) -> bool:
        """True when the revision went from old to new only through this checkout's own commits; False when
        other people's changes may have arrived or that can't be told."""
        return False

    def changed_tasks(self, config: dict, old, new) -> set[tuple[str, str]] | None:
        """(source name, task id) of every task file that differs between old and new; None when that can't be
        told (every key then counts)."""
        return None

    def format_revision(self, rev) -> str:
        """A revision for messages: `a1b2c3d` / `r12`; "" for None."""
        return "" if rev is None else str(rev)

    def incoming_header(self, revisions: tuple | None) -> str:
        """The INCOMING heading's detail: `git pull, a1b2c3d -> 5d6e7f8` / `svn update, r123 -> r127`."""
        span = ""
        if revisions and revisions[0] is not None and revisions[1] is not None:
            span = f", {self.format_revision(revisions[0])} -> {self.format_revision(revisions[1])}"
        return f"{self.update_verb}{span}"

    def conflict_message(self, paths: list[str]) -> str:
        raise NotImplementedError

    def conflict_banner(self, conflicts: list[str]) -> str:
        """The board header's text for a stopped update."""
        raise NotImplementedError

    def update_description(self, config: dict, interval: float) -> str:
        """What the periodic update does, for the start-up lines of `tb serve` and `tb watch`."""
        raise NotImplementedError

    # ---- board commits (commit.py) ----

    def commit_file(self, config: dict, path: str, message: str) -> tuple:
        """Commit one file: (revision text, "", False) on success, else (None, the error, whether the file is
        out of date against the repository)."""
        raise NotImplementedError

    def file_is_clean(self, config: dict, path: str) -> bool:
        """True when path has no local change (RuntimeError when the status can't be read)."""
        raise NotImplementedError

    def revert_file(self, config: dict, path: str) -> str:
        """Drop the local change of path; "" on success, else the error."""
        raise NotImplementedError

    def update_file(self, config: dict, path: str) -> str:
        """Bring path to the repository's version; "" on success, else the error."""
        raise NotImplementedError

    def commit_note(self, revision: str) -> str:
        """How a committed board write is reported: `committed (r12)` / `committed locally (a1b2c3d); push ...`."""
        raise NotImplementedError

    def commit_revision_pattern(self) -> str:
        """The regex a revision text of commit_file matches (the web board passes it through a query string)."""
        raise NotImplementedError


_BACKENDS: dict[str, Backend] = {}


def backend_named(name: str) -> Backend:
    """The backend of a name (`git` / `svn`); one instance per name."""
    if name not in NAMES:
        raise ValueError(f"unknown vcs {name!r}; one of {', '.join(NAMES)}")
    if name not in _BACKENDS:
        if name == "git":
            from .git import GitBackend
            _BACKENDS[name] = GitBackend()
        else:
            from .svn import SvnBackend
            _BACKENDS[name] = SvnBackend()
    return _BACKENDS[name]


def backend(config: dict | str) -> Backend:
    """The backend of a validated config (its `vcs`, default git), or of a name."""
    if isinstance(config, str):
        return backend_named(config)
    return backend_named(config.get("vcs") or "git")


def review_field(config: dict | None) -> ReviewField:
    """The review header field of a config (or of a schema dict carrying `review_field`); `pr` by default."""
    if not config:
        return DEFAULT_REVIEW
    name = config.get("review_field")
    if name:
        return next(r for r in REVIEW_FIELDS.values() if r.name == name)
    return REVIEW_FIELDS[config.get("vcs") or "git"]


def norm_path(path: str) -> str:
    return path.strip().replace("\\", "/")


def inside(root: str, path: str) -> bool:
    """True when path is inside the folder root (both absolute)."""
    root_real = os.path.realpath(root)
    return os.path.realpath(path).startswith(root_real + os.sep)
