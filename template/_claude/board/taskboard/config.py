"""
Board config (`.claude/board/board.json`): loaded with the stdlib json module and validated strictly.

Paths in the config (`repo_root`, each source's `root`, `plans_dir`) are relative: `repo_root` to the folder of
the config file, everything else to the repo root.
"""

import copy
import hashlib
import ipaddress
import json
import os
import re

from .model import DEFAULT_ROLES, DEFAULT_TYPES
from .workflow import ALLOWED, DEFAULT_TYPE_RULES, TYPE_RULE_KEYS, resolve_state

BOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(BOARD_DIR, "board.json")

CONFIG_KEYS = {"title", "repo_root", "vcs", "main_branch", "ownership", "plans_dir", "types", "roles", "host",
               "port", "refresh_seconds", "watch", "sources", "people", "default_owner", "handover", "type_rules",
               "reload"}
SOURCE_KEYS = {"name", "root", "prefix", "editable"}
WATCH_KEYS = {"quiet_seconds", "poll_seconds", "update_seconds", "update_paths", "update_when_dirty",
              "board_idle_minutes"}
HANDOVER_KEYS = {"path", "max_lines", "max_chars", "max_line_chars"}
HANDOVER_DEFAULTS = {"path": "AI/SupervisorHandover.md", "max_lines": 60, "max_chars": 6000, "max_line_chars": 160}
RELOAD_KEYS = {"budget_bytes"}
SOURCE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
PREFIX = re.compile(r"^[A-Z]+$")
# The version control systems the board knows (taskboard/vcs) with the main line each defaults to.
VCS_NAMES = ("git", "svn")
MAIN_BRANCH_DEFAULTS = {"git": "master", "svn": "trunk"}
# Who may set a task's owner: strict = only the current owner transfers it (`--force --reason` for anyone else)
# and `--answered-by` / `--decided-by` are mandatory; open = anyone sets the owner, and the two flags default
# to this checkout's person (`tb me`), else `user`.
OWNERSHIP_MODES = ("strict", "open")
DEFAULTS = {
    "title": "Task Board",
    "repo_root": "../..",
    "vcs": "git",
    "ownership": "strict",
    "plans_dir": "AI/Plans",
    "host": "127.0.0.1",
    "port": 5070,
    "refresh_seconds": 5,
}
WATCH_DEFAULTS = {"quiet_seconds": 120, "poll_seconds": 5, "update_seconds": 60, "update_paths": ["AI"],
                  "update_when_dirty": False, "board_idle_minutes": 15}
# Below this the periodic update would hammer the server; 0 disables it.
MIN_UPDATE_SECONDS = 10
REQUIRED_ROLES = {"user", "supervisor"}
LOCAL_FILE = "local.json"
LOCAL_KEYS = {"me", "sources", "reload", "board_commits", "role"}
PERSON = re.compile(r"[\w-]+")


class BoardConfigError(ValueError):
    pass


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _word_list(value, what: str, required: set[str] = frozenset()) -> list[str]:
    if not isinstance(value, list) or not value or not all(
            isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9-]*", v) for v in value):
        raise BoardConfigError(f"{what} must be a non-empty list of lowercase words, got {value!r}")
    missing = sorted(required - set(value))
    if missing:
        raise BoardConfigError(f"{what} must contain {', '.join(missing)}")
    return list(dict.fromkeys(value))


def _state_list(value, what: str) -> list[str]:
    """A list of states (full names, numbers or names without the number) as full state names, without repeats."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BoardConfigError(f"{what} must be a list of states, got {value!r}")
    try:
        return list(dict.fromkeys(resolve_state(v) for v in value))
    except ValueError as e:
        raise BoardConfigError(f"{what}: {e}") from None


def _state_map(value, what: str) -> dict[str, list[str]]:
    """{source state: [target states]} as full state names."""
    if not isinstance(value, dict):
        raise BoardConfigError(f"{what} must be an object of state: [states], got {value!r}")
    result: dict[str, list[str]] = {}
    for key, targets in value.items():
        try:
            src = resolve_state(key)
        except ValueError as e:
            raise BoardConfigError(f"{what}: {e}") from None
        if src in result:
            raise BoardConfigError(f"{what}: state {src} appears twice")
        result[src] = _state_list(targets, f"{what}.{key}")
        if src in result[src]:
            raise BoardConfigError(f"{what}.{key}: {src} cannot lead to itself")
    return result


def _type_rules(value, types: list[str]) -> dict:
    """
    The per-type move rules: {type: {"moves": {state: [states]}, "refuse": {state: [states]}, "no_branch": bool,
    "tokens_from": [states]}}, states normalized to full names. A type without an entry follows the default rules.
    """
    if value is None:
        return copy.deepcopy(DEFAULT_TYPE_RULES)
    if not isinstance(value, dict):
        raise BoardConfigError(f"type_rules must be an object of type: rules, got {value!r}")
    result: dict = {}
    for task_type, rules in value.items():
        if task_type not in types:
            raise BoardConfigError(f"type_rules: unknown type {task_type!r}; one of {', '.join(types)}")
        what = f"type_rules.{task_type}"
        if not isinstance(rules, dict):
            raise BoardConfigError(f"{what} must be an object with {', '.join(sorted(TYPE_RULE_KEYS))}")
        unknown = sorted(set(rules) - TYPE_RULE_KEYS)
        if unknown:
            raise BoardConfigError(f"{what}: unknown key(s) {', '.join(unknown)}; "
                                   f"allowed: {', '.join(sorted(TYPE_RULE_KEYS))}")
        entry: dict = {}
        if "moves" in rules:
            entry["moves"] = _state_map(rules["moves"], f"{what}.moves")
        if "refuse" in rules:
            entry["refuse"] = _state_map(rules["refuse"], f"{what}.refuse")
            for src, dsts in entry["refuse"].items():
                path = entry.get("moves", {}).get(src, ALLOWED[src])
                if any(dst in path for dst in dsts):
                    raise BoardConfigError(f"{what}.refuse: {src} -> {', '.join(d for d in dsts if d in path)} "
                                           f"is also an allowed move for the type")
        if "no_branch" in rules:
            if not isinstance(rules["no_branch"], bool):
                raise BoardConfigError(f"{what}.no_branch must be true or false, got {rules['no_branch']!r}")
            entry["no_branch"] = rules["no_branch"]
        if "tokens_from" in rules:
            entry["tokens_from"] = _state_list(rules["tokens_from"], f"{what}.tokens_from")
        result[task_type] = entry
    return result


def _people(value, roles: list[str]) -> list[str]:
    """The people who may answer and decide (names as written in task files), e.g. ["Alex", "Robin"]."""
    if not isinstance(value, list) or not all(isinstance(v, str) and PERSON.fullmatch(v) for v in value):
        raise BoardConfigError(f"people must be a list of single words (letters, digits, '-', '_'), "
                               f"got {value!r}")
    doubled = sorted({v for v in value if value.count(v) > 1})
    if doubled:
        raise BoardConfigError(f"people names {', '.join(doubled)} more than once")
    clash = sorted(set(value) & set(roles))
    if clash:
        raise BoardConfigError(f"people must not repeat a role: {', '.join(clash)}")
    return list(value)


def _default_owner(value, people: list[str]) -> str | None:
    """The person a task's `owner: null` means (e.g. "Alex"), or None: null then means unowned."""
    if value is None:
        return None
    if value not in people:
        raise BoardConfigError(f"default_owner must be one of the people "
                               f"({', '.join(people) or 'none configured'}), got {value!r}")
    return value


def _choice(value, what: str, choices: tuple) -> str:
    if not isinstance(value, str) or value not in choices:
        raise BoardConfigError(f"{what} must be one of {', '.join(choices)}, got {value!r}")
    return value


def check_update_seconds(value, what: str = "update_seconds"):
    """0 (no periodic update) or a number of at least MIN_UPDATE_SECONDS."""
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not (value == 0 or value >= MIN_UPDATE_SECONDS)):
        raise BoardConfigError(f"{what} must be 0 (off) or a number of at least {MIN_UPDATE_SECONDS}, "
                               f"got {value!r}")
    return value


def check_idle_minutes(value, what: str = "board_idle_minutes"):
    """Minutes without a request before a watcher-started board stops: a number of at least 0 (0 = never)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise BoardConfigError(f"{what} must be a number of minutes, at least 0 (0 = never), got {value!r}")
    return value


def board_id(config: dict) -> str:
    """The identity of this checkout's board (a hash of its config path, no path exposed): /api/fingerprint."""
    path = os.path.normcase(os.path.abspath(config.get("config_path") or DEFAULT_CONFIG))
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def _handover(value, repo_root: str) -> dict | None:
    """The handover size limits with defaults filled in (path made absolute as "path_abs"); None when not configured."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - HANDOVER_KEYS:
        raise BoardConfigError(f"handover must be an object with {', '.join(sorted(HANDOVER_KEYS))}")
    result = {**HANDOVER_DEFAULTS, **value}
    path = result["path"]
    if (not isinstance(path, str) or not path.strip() or os.path.isabs(path) or path.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:", path) or ".." in re.split(r"[\\/]", path)):
        raise BoardConfigError(f"handover.path must be a relative path inside the repo without '..', got {path!r}")
    for key in ("max_lines", "max_chars", "max_line_chars"):
        if not isinstance(result[key], int) or isinstance(result[key], bool) or result[key] < 1:
            raise BoardConfigError(f"handover.{key} must be a whole number of at least 1, got {result[key]!r}")
    result["path_abs"] = os.path.normpath(os.path.join(repo_root, path))
    return result


def _check_reload_budget(value) -> None:
    """`reload` of board.json: the settings of the SessionStart reload hook (it reads the file itself)."""
    if value is None:
        return
    if not isinstance(value, dict) or set(value) - RELOAD_KEYS:
        raise BoardConfigError(f"reload must be an object with {', '.join(sorted(RELOAD_KEYS))}")
    budget = value.get("budget_bytes", 1)
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise BoardConfigError(f"reload.budget_bytes must be a whole number of at least 1, got {budget!r}")


def _update_paths(value, repo_root: str, must_exist: bool) -> list[str]:
    """The folders of the periodic update (git: whose local edits pause it; svn: the ones updated): relative to
    the repo root, inside it, never '..'."""
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise BoardConfigError(f"watch.update_paths must be a non-empty list of folders, got {value!r}")
    root = os.path.realpath(repo_root)
    paths = []
    for v in value:
        parts = re.split(r"[\\/]", v)
        if os.path.isabs(v) or v.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", v) or ".." in parts:
            raise BoardConfigError(f"watch.update_paths: {v!r} must be a relative path inside the repo without '..'")
        full = os.path.realpath(os.path.join(root, v))
        if full == root or not full.startswith(root + os.sep):
            raise BoardConfigError(f"watch.update_paths: {v!r} must be a folder inside the repo, not the repo root")
        if must_exist and not os.path.isdir(full):
            raise BoardConfigError(f"watch.update_paths: folder not found: {full}")
        paths.append(v.replace("\\", "/").strip("/"))
    return paths


def _check_reload(reload) -> None:
    """`reload` of local.json: a list of repo-relative paths read by the SessionStart reload hook (not by tb)."""
    if reload is None:
        return
    if not isinstance(reload, list):
        raise BoardConfigError("local.json: reload must be a list of repo-relative paths")
    for value in reload:
        if not isinstance(value, str) or not value.strip():
            raise BoardConfigError(f"local.json: reload entries must be non-empty strings, got {value!r}")
        norm = value.replace("\\", "/")
        # Same rule as the reload hook (it must stay stdlib-only, so the rule is repeated there).
        if norm.startswith("/") or ":" in norm or ".." in norm.split("/"):
            raise BoardConfigError(f"local.json: reload entries must be repo-relative paths, got {value!r}")


def validate_local(local, people: list[str]) -> str | None:
    """`me` of a local.json object (the person working in this checkout), or None when it names nobody."""
    if not isinstance(local, dict):
        raise BoardConfigError("local.json must be an object like {\"me\": \"<name>\"}.")
    unknown = sorted(k for k in set(local) - LOCAL_KEYS if not k.startswith("_"))
    if unknown:
        raise BoardConfigError(f"local.json: unknown key(s) {', '.join(unknown)}")
    _check_reload(local.get("reload"))
    if not isinstance(local.get("board_commits", False), bool):
        raise BoardConfigError("local.json: board_commits must be true or false. Fix it with: "
                               "tb me --board-commits on|off")
    role = local.get("role")
    if role is not None and role != "supervisor":
        raise BoardConfigError(f"local.json: role must be \"supervisor\" or absent, got {role!r}. Fix it with: "
                               "tb me --role supervisor|none")
    me = local.get("me")
    if me is None:
        return None
    if me not in people:
        raise BoardConfigError(f"local.json: me must be one of the people in board.json "
                               f"({', '.join(people) or 'none configured'}), got {me!r}. "
                               "Fix it with: tb me <name>")
    return me


def local_path_of(config: dict) -> str:
    """The checkout's own local.json, next to the config file (never versioned)."""
    return os.path.join(os.path.dirname(config.get("config_path") or DEFAULT_CONFIG), LOCAL_FILE)


def validate_board_config(config: dict, base_dir: str = BOARD_DIR) -> dict:
    """
    The validated config with defaults filled in, plus "repo_root_path" (absolute); each source gets
    "root_path" (absolute), "prefix" (default "T") and "editable" (default False). Raises BoardConfigError.
    """
    if not isinstance(config, dict):
        raise BoardConfigError("The config must be an object.")
    unknown = sorted(k for k in set(config) - CONFIG_KEYS if not k.startswith("_"))
    if unknown:
        raise BoardConfigError(f"Unknown config key(s): {', '.join(unknown)}")
    result = {**DEFAULTS, **{k: v for k, v in config.items() if k not in ("sources", "watch")
                             and not k.startswith("_")}}

    result["vcs"] = _choice(result["vcs"], "vcs", VCS_NAMES)
    result["ownership"] = _choice(result["ownership"], "ownership", OWNERSHIP_MODES)
    result.setdefault("main_branch", MAIN_BRANCH_DEFAULTS[result["vcs"]])
    for key in ("title", "repo_root", "main_branch", "plans_dir"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise BoardConfigError(f"{key} must be non-empty text, got {result[key]!r}")
    result["repo_root_path"] = os.path.normpath(os.path.join(base_dir, result["repo_root"]))
    if not os.path.isdir(result["repo_root_path"]):
        raise BoardConfigError(f"repo_root folder not found: {result['repo_root_path']}")
    result["types"] = _word_list(config.get("types", DEFAULT_TYPES), "types")
    result["type_rules"] = _type_rules(config.get("type_rules"), result["types"])
    result["roles"] = _word_list(config.get("roles", DEFAULT_ROLES), "roles", REQUIRED_ROLES)
    result["people"] = _people(config.get("people", []), result["roles"])
    result["default_owner"] = _default_owner(config.get("default_owner"), result["people"])
    result["me"] = None

    host = result["host"]
    if not isinstance(host, str) or not is_loopback(host):
        raise BoardConfigError(f"host must be a loopback address (the board has no authentication), got {host!r}")
    port = result["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise BoardConfigError(f"port must be a number from 1 to 65535, got {port!r}")
    refresh = result["refresh_seconds"]
    if not isinstance(refresh, (int, float)) or isinstance(refresh, bool) or refresh < 1:
        raise BoardConfigError(f"refresh_seconds must be a number of at least 1, got {refresh!r}")

    watch = config.get("watch", {})
    if not isinstance(watch, dict) or set(watch) - WATCH_KEYS:
        raise BoardConfigError(f"watch must be an object with {', '.join(sorted(WATCH_KEYS))}")
    result["watch"] = {**WATCH_DEFAULTS, **watch}
    for key in ("quiet_seconds", "poll_seconds"):
        value = result["watch"][key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 1:
            raise BoardConfigError(f"watch.{key} must be a number of at least 1, got {value!r}")
    result["watch"]["update_seconds"] = check_update_seconds(result["watch"]["update_seconds"],
                                                             "watch.update_seconds")
    result["watch"]["update_paths"] = _update_paths(result["watch"]["update_paths"], result["repo_root_path"],
                                                    must_exist="update_paths" in watch)
    result["watch"]["board_idle_minutes"] = check_idle_minutes(result["watch"]["board_idle_minutes"],
                                                               "watch.board_idle_minutes")
    if not isinstance(result["watch"]["update_when_dirty"], bool):
        raise BoardConfigError(f"watch.update_when_dirty must be true or false, "
                               f"got {result['watch']['update_when_dirty']!r}")

    result["handover"] = _handover(config.get("handover"), result["repo_root_path"])
    _check_reload_budget(config.get("reload"))

    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise BoardConfigError("sources must be a non-empty list")
    result["sources"] = _sources(sources, result["repo_root_path"], "sources", set(), set())
    return result


def _sources(sources: list, repo_root_path: str, what: str, names: set[str], prefixes: set[str]) -> list[dict]:
    """
    Validated task sources: each gets "root_path" (absolute), "prefix" (default "T") and "editable" (default
    False). names/prefixes are the ones already taken (by board.json, when local.json adds sources).
    """
    result = []
    for i, source in enumerate(sources):
        where = f"{what}[{i}]"
        if not isinstance(source, dict):
            raise BoardConfigError(f"{where} must be an object")
        unknown = sorted(set(source) - SOURCE_KEYS)
        if unknown:
            raise BoardConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
        name = source.get("name")
        if not isinstance(name, str) or not SOURCE_NAME.match(name):
            raise BoardConfigError(f"{where}: name must match [A-Za-z0-9_-]+, got {name!r}")
        if name in names:
            raise BoardConfigError(f"{where}: duplicate name {name!r}")
        names.add(name)
        prefix = source.get("prefix", "T")
        if not isinstance(prefix, str) or not PREFIX.match(prefix):
            raise BoardConfigError(f"{where} ({name}): prefix must be uppercase letters, got {prefix!r}")
        if prefix in prefixes:
            raise BoardConfigError(f"{where} ({name}): prefix {prefix} is used by another source")
        prefixes.add(prefix)
        root = source.get("root")
        if not isinstance(root, str) or not root:
            raise BoardConfigError(f"{where} ({name}): root is required")
        root_path = os.path.normpath(os.path.join(repo_root_path, root))
        if not os.path.isdir(root_path):
            raise BoardConfigError(f"{where} ({name}): root folder not found: {root_path}")
        editable = source.get("editable", False)
        if not isinstance(editable, bool):
            raise BoardConfigError(f"{where} ({name}): editable must be true or false")
        result.append({"name": name, "root": root, "root_path": root_path, "prefix": prefix,
                       "editable": editable})
    return result


def load_board_config(path: str | None = None, with_local: bool = True) -> dict:
    """
    The validated config of path (default board.json next to the package). "me" is the person of this checkout
    from local.json next to the config (None when the file is missing or with_local is False); the sources
    local.json lists are appended to the config's; "board_commits" is local.json's opt-in for board commits
    (True only when set there; see commit.py).
    """
    path = os.path.abspath(path or DEFAULT_CONFIG)
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError) as exc:
        raise BoardConfigError(f"Cannot read the config {path}: {exc}") from exc
    result = validate_board_config(config, os.path.dirname(path))
    result["config_path"] = path
    result["board_commits"] = False
    local_path = local_path_of(result)
    if with_local and os.path.exists(local_path):
        try:
            with open(local_path, encoding="utf-8") as f:
                local = json.load(f)
        except (OSError, ValueError) as exc:
            raise BoardConfigError(f"Cannot read {local_path}: {exc}") from exc
        result["me"] = validate_local(local, result["people"])
        result["board_commits"] = local.get("board_commits") is True
        # Private task folders of this checkout (e.g. an untracked P source): listed here, never in board.json.
        extra = local.get("sources", [])
        if not isinstance(extra, list):
            raise BoardConfigError(f"{local_path}: sources must be a list")
        result["sources"] += _sources(extra, result["repo_root_path"], "local.json sources",
                                      {s["name"] for s in result["sources"]}, {s["prefix"] for s in result["sources"]})
    return result


def schema_of(config: dict) -> dict:
    """What the consistency check needs of a config; review_field is the header field of the code review
    (`pr` for git, `rev` for svn)."""
    return {"types": config["types"], "roles": config["roles"], "people": config.get("people", []),
            "default_owner": config.get("default_owner"),
            "review_field": MAIN_REVIEW_FIELDS[config.get("vcs") or "git"]}


MAIN_REVIEW_FIELDS = {"git": "pr", "svn": "rev"}
