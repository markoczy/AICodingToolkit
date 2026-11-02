#!/usr/bin/env python3
"""
Build a project-specific AI coding harness from the templates in `template/`.

    python build/build.py --answers answers.json --out <dir> [--zip <file.zip>] [--force]
    python build/build.py --questions              # the question schema (JSON) the setup skill asks from
    python build/build.py --example git|svn        # example answers to start from
    python build/build.py --check answers.json     # validate answers, print the derived context, build nothing

Standard library only, Python 3.10+.

Template syntax (rendered files only, see RENDER_SUFFIXES / RENDER_NAMES):

    {{name}}                      a value from the derived context (unknown names are an error)
    {{#if expr}} ... {{/if}}      a block kept only when `expr` is true; `expr` is a Python boolean expression
    {{#if expr}} ... {{else}} ... {{/if}}   over the context's flags (`svn`, `git and multiuser`, `not analyst`)

A block tag alone on its line takes the whole line with it, so templates stay readable. Tags may also sit inline
(`merged {{#if git}}PR #<n>{{else}}r<n>{{/if}}`). Blocks nest. Every other file is copied verbatim.

Which files go into a build is decided by PATH_CONDITIONS (glob -> expression); a path without a rule is always
included. Empty directories are kept.
"""
from __future__ import annotations

import argparse
import colorsys
import fnmatch
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "template"
QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.json"

# Files that are rendered (template tags replaced). Everything else is copied byte for byte: the board's Python,
# JavaScript and Jinja templates read their configuration from board.json at run time and contain `{{` of their own.
RENDER_SUFFIXES = {".md", ".json", ".css", ".txt", ".gitignore"}
RENDER_NAMES = {"CLAUDE.md", "gitignore", "svn-ignore.txt"}
NEVER_RENDER = {".claude/board/taskboard/web/templates"}  # Jinja templates: `{{ }}` belongs to Flask

# Paths (relative to template/, forward slashes, fnmatch globs) included only when the expression is true.
# A directory rule covers everything below it.
PATH_CONDITIONS = [
    (".claude/svn", "svn"),
    (".claude/svn/**", "svn"),
    (".claude/skills/coordinator", "multiuser"),
    (".claude/skills/coordinator/**", "multiuser"),
    (".claude/agents/analyst.md", "analyst"),
    ("harness.gitignore", "git"),
    (".claude/board/gitignore", "git"),
    ("svn-ignore.txt", "svn"),
]

TAG = re.compile(r"\{\{(#if\s+(?P<cond>[^}]+?)|(?P<else>else)|(?P<end>/if)|(?P<var>[A-Za-z_][\w.]*))\}\}")
STANDALONE = re.compile(r"^[ \t]*(\{\{(?:#if\b[^}]*|else|/if)\}\})[ \t]*\r?\n", re.MULTILINE)


class BuildError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------
# Template engine


def _parse(text: str, where: str):
    """Parse into a tree: list of str | ("var", name) | ("if", cond, then_nodes, else_nodes)."""
    text = STANDALONE.sub(r"\1", text)
    pos = 0
    stack = [[]]          # node lists being filled
    frames = []           # (cond, then_nodes) for open ifs; the current list is the branch being filled
    in_else = []
    for match in TAG.finditer(text):
        if match.start() > pos:
            stack[-1].append(text[pos:match.start()])
        pos = match.end()
        if match.group("cond") is not None:
            frames.append((match.group("cond").strip(), stack[-1]))
            in_else.append(False)
            stack.append([])
        elif match.group("else") is not None:
            if not frames or in_else[-1]:
                raise BuildError(f"{where}: {{{{else}}}} without an open {{{{#if}}}}")
            then_nodes = stack.pop()
            cond, parent = frames[-1]
            frames[-1] = (cond, parent, then_nodes)  # type: ignore[assignment]
            in_else[-1] = True
            stack.append([])
        elif match.group("end") is not None:
            if not frames:
                raise BuildError(f"{where}: {{{{/if}}}} without an open {{{{#if}}}}")
            branch = stack.pop()
            frame = frames.pop()
            if in_else.pop():
                cond, parent, then_nodes = frame
                parent.append(("if", cond, then_nodes, branch))
            else:
                cond, parent = frame
                parent.append(("if", cond, branch, []))
        else:
            stack[-1].append(("var", match.group("var")))
    if frames:
        raise BuildError(f"{where}: unclosed {{{{#if {frames[-1][0]}}}}}")
    if pos < len(text):
        stack[-1].append(text[pos:])
    return stack[0]


def _evaluate(cond: str, ctx: dict, where: str) -> bool:
    try:
        return bool(eval(cond, {"__builtins__": {}}, ctx))  # noqa: S307 - templates are the toolkit's own files
    except NameError as exc:
        raise BuildError(f"{where}: unknown name in condition {{{{#if {cond}}}}}: {exc}") from exc
    except SyntaxError as exc:
        raise BuildError(f"{where}: bad condition {{{{#if {cond}}}}}: {exc}") from exc


def _render_nodes(nodes, ctx: dict, where: str, out: list) -> None:
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif node[0] == "var":
            name = node[1]
            if name not in ctx:
                raise BuildError(f"{where}: unknown template value {{{{{name}}}}}")
            value = ctx[name]
            out.append(value if isinstance(value, str) else json.dumps(value))
        else:
            _, cond, then_nodes, else_nodes = node
            _render_nodes(then_nodes if _evaluate(cond, ctx, where) else else_nodes, ctx, where, out)


def render(text: str, ctx: dict, where: str = "<text>") -> str:
    out: list = []
    _render_nodes(_parse(text, where), ctx, where, out)
    result = "".join(out)
    if "{{" in result:
        line = result[: result.index("{{")].count("\n") + 1
        raise BuildError(f"{where}:{line}: '{{{{' left in the rendered text")
    return result


# ---------------------------------------------------------------------------------------------------------------
# Questions and answers


def load_questions() -> list:
    with open(QUESTIONS_FILE, encoding="utf-8") as handle:
        return json.load(handle)["questions"]


class _Answers(dict):
    """Answers as an eval namespace: an answer not given yet reads as None."""

    def __missing__(self, key):
        return None


def _applies(question: dict, answers: dict) -> bool:
    when = question.get("when")
    return True if not when else bool(eval(when, {"__builtins__": {}}, _Answers(answers)))  # noqa: S307


def _default(question: dict, answers: dict):
    default = question.get("default")
    if isinstance(default, dict):  # per value of another answer, e.g. main_branch by vcs
        key = default["by"]
        return default["values"].get(str(answers.get(key)), default.get("else"))
    return default


def validate(answers: dict) -> dict:
    """Return the answers completed with defaults; raise BuildError on a missing or invalid one."""
    result = dict(answers)
    problems = []
    for question in load_questions():
        qid = question["id"]
        if not _applies(question, result):
            result.pop(qid, None)
            continue
        if qid not in result or result[qid] in (None, ""):
            default = _default(question, result)
            if default is None and question.get("required", True):
                problems.append(f"{qid}: missing ({question['prompt']})")
                continue
            result[qid] = default
        value = result[qid]
        kind = question["type"]
        if kind == "choice":
            allowed = [option["value"] for option in question["options"]]
            if value not in allowed:
                problems.append(f"{qid}: {value!r} is not one of {allowed}")
        elif kind == "bool":
            if isinstance(value, str):
                value = value.strip().lower() in ("y", "yes", "true", "1", "on")
                result[qid] = value
            if not isinstance(value, bool):
                problems.append(f"{qid}: expected true/false")
        elif kind == "int":
            try:
                result[qid] = int(value)
            except (TypeError, ValueError):
                problems.append(f"{qid}: expected a whole number")
        elif kind == "list":
            if isinstance(value, str):
                value = [item.strip() for item in re.split(r"[,\s]+", value) if item.strip()]
                result[qid] = value
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                problems.append(f"{qid}: expected a list of words")
            elif question.get("pattern") and any(not re.fullmatch(question["pattern"], item) for item in value):
                problems.append(f"{qid}: every entry must match {question['pattern']}")
        elif kind == "text":
            if not isinstance(value, str):
                problems.append(f"{qid}: expected text")
            elif question.get("pattern") and not re.fullmatch(question["pattern"], value):
                problems.append(f"{qid}: {value!r} must match {question['pattern']}")
    if result.get("mode") == "multi":
        people = result.get("people") or []
        if result.get("user_name") not in people:
            problems.append("user_name must be one of people")
    if problems:
        raise BuildError("invalid answers:\n  " + "\n  ".join(problems))
    return result


# ---------------------------------------------------------------------------------------------------------------
# Derived context


def _hex_to_rgb(value: str) -> tuple:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _rgb_to_hex(rgb: tuple) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)


def _luminance(color: str) -> float:
    """Relative luminance (0 dark .. 1 light), to pick readable text on a coloured button."""
    r, g, b = _hex_to_rgb(color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def shade(color: str, lightness_delta: float) -> str:
    """The colour with its HLS lightness moved by `lightness_delta` (-1..1), clamped."""
    h, l, s = colorsys.rgb_to_hls(*_hex_to_rgb(color))
    return _rgb_to_hex(colorsys.hls_to_rgb(h, max(0.0, min(1.0, l + lightness_delta)), s))


def context(answers: dict) -> dict:
    """Flags and values the templates use, from validated answers."""
    a = validate(answers)
    vcs = a["vcs"]
    git, svn = vcs == "git", vcs == "svn"
    multiuser = a["mode"] == "multi"
    strict = multiuser and a.get("ownership") == "strict"
    people = list(a["people"]) if multiuser else [a["user_name"]]
    others = [p for p in people if p != a["user_name"]]
    main = a.get("main_branch") or ("trunk" if svn else "master")
    a["main_branch"] = main
    deploy = a["deploy"]
    types = list(a["task_types"])
    roles = ["user", "supervisor", "executor", "reviewer", "researcher"] + (["analyst"] if a["analyst"] else [])
    primary, accent = a["color_primary"].lower(), a["color_accent"].lower()

    ctx = {
        # flags
        "git": git, "svn": svn, "vcs": vcs,
        "multiuser": multiuser, "single": not multiuser,
        "strict": strict, "open": multiuser and not strict,
        "analyst": bool(a["analyst"]),
        "deploy_prod": deploy == "production", "deploy_test": deploy == "test", "deploy_none": deploy == "none",
        "has_deploy": deploy != "none",
        # project
        "project": a["project_name"],
        "board_title": f"{a['project_name']} · AI Task Board",
        "user": a["user_name"],
        "dev": others[0] if others else "Robin",   # a second person for examples
        "people": people,
        "people_json": json.dumps(people),
        "roles_json": json.dumps(roles),
        "roles_list": " | ".join(roles),
        "types_json": json.dumps(types),
        "types_list": " | ".join(types),
        "port": str(a["port"]),
        "port_alt": str(a["port"] + 1),
        "python": a["python"],
        "confidential": a["confidential"],
        "main": main,
        # colours (dark theme gets the given colours; hover/link are lighter, the light theme darker)
        "color_primary": primary,
        "color_primary_hover": shade(primary, 0.10),
        "color_link": shade(primary, 0.12),
        "color_accent": accent,
        "color_primary_light": shade(primary, -0.08),
        "color_primary_hover_light": shade(primary, -0.16),
        "color_link_light": shade(primary, -0.14),
        "color_accent_light": shade(accent, -0.15),
        "color_primary_rgb": ", ".join(str(round(c * 255)) for c in _hex_to_rgb(primary)),
        "color_on_primary": "#ffffff" if _luminance(primary) < 0.45 else "#1a1a1a",
        "color_on_primary_light": "#ffffff" if _luminance(shade(primary, -0.08)) < 0.45 else "#1a1a1a",
    }

    # Version-control vocabulary. Prose that differs by more than a word uses {{#if git}} blocks instead.
    if git:
        ctx.update({
            "main_ref": f"`{main}`",
            "vcs_name": "git",
            "vcs_update": f"git fetch -q origin && git merge --ff-only @{{u}}",
            "vcs_mv": "git mv",
            "vcs_commit_tasks": 'git add AI && git commit -m "tasks: ..." && git push',
            "commit_push": "commit and push",
            "committed_pushed": "committed and pushed",
            "review_field": "pr",
            "review_flag": "--pr <n>",
            "review_ref": "PR #<n>",
            "review_noun": "pull request",
            "landed_reason": f"merged PR #<n> into {main}, reviewer approved",
            "landed_status": "implemented in PR #<n>",
            "worktree": "worktree",
            "worktrees": "worktrees",
            "worktrees_dir": ".claude/worktrees/",
            "tracked": "tracked",
            "untracked": "untracked",
            "checkout_phrase": f"the main checkout, which is always on `{main}`",
            "on_main": f"on `{main}`",
            "own_checkout": f"their own checkout on `{main}`",
        })
    else:
        ctx.update({
            "main_ref": "`^/trunk`",
            "vcs_name": "Subversion",
            "vcs_update": "svn update",
            "vcs_mv": "svn mv",
            "vcs_commit_tasks": 'svn add --force AI && svn commit AI -m "tasks: ..."',
            "commit_push": "commit",
            "committed_pushed": "committed",
            "review_field": "rev",
            "review_flag": "--rev <n>",
            "review_ref": "r<n>",
            "review_noun": "branch revision",
            "landed_reason": "merged into trunk r<n>, reviewer approved",
            "landed_status": "implemented in trunk r<n>",
            "worktree": "working copy",
            "worktrees": "working copies",
            "worktrees_dir": "`<checkout>.worktrees/` next to the main checkout",
            "tracked": "versioned",
            "untracked": "unversioned",
            "checkout_phrase": "the main checkout, which is always on `^/trunk`",
            "on_main": "in the trunk checkout",
            "own_checkout": "their own trunk checkout",
        })
    if deploy == "production":
        ctx.update({"deploy_where": "production", "deploy_check": "production check",
                    "deploy_phrase": "deploy to production and check the production data"})
    elif deploy == "test":
        ctx.update({"deploy_where": "the test server", "deploy_check": "test server check",
                    "deploy_phrase": "deploy to the test server and verify there"})
    else:
        ctx.update({"deploy_where": "nowhere (no deploy step)", "deploy_check": "verification",
                    "deploy_phrase": "verify the landed change"})
    ctx["answers"] = a
    return ctx


# ---------------------------------------------------------------------------------------------------------------
# Build


def _included(rel: str, ctx: dict) -> bool:
    for pattern, cond in PATH_CONDITIONS:
        if fnmatch.fnmatchcase(rel, pattern) and not _evaluate(cond, ctx, f"PATH_CONDITIONS[{pattern}]"):
            return False
    return True


def _renders(rel: str) -> bool:
    path = Path(rel)
    if any(rel == never or rel.startswith(never + "/") for never in NEVER_RENDER):
        return False
    return path.suffix in RENDER_SUFFIXES or path.name in RENDER_NAMES


def _output_name(rel: str) -> str:
    """The path a template file gets in the build.

    `template/_claude/` becomes `.claude/` (a real `.claude/` inside template/ would make Claude Code treat the
    unrendered skills, agents and settings as this toolkit's own), and a file named `gitignore` becomes
    `.gitignore`.
    """
    parts = rel.split("/")
    if parts[0] == "_claude":
        parts[0] = ".claude"
    if parts[-1] == "gitignore":
        parts[-1] = ".gitignore"
    return "/".join(parts)


def build(answers: dict, out_dir: Path, zip_path: Path | None = None, force: bool = False) -> list:
    ctx = context(answers)
    if out_dir.exists():
        if not force:
            raise BuildError(f"{out_dir} exists; use --force to replace it")
        shutil.rmtree(out_dir)
    written = []
    for path in sorted(TEMPLATE_DIR.rglob("*")):
        rel = _output_name(path.relative_to(TEMPLATE_DIR).as_posix())  # rules speak of the output layout
        if "__pycache__" in rel or rel.endswith(".pyc") or "/.pytest_cache" in rel or "/log/" in rel:
            continue
        if not _included(rel, ctx):
            continue
        target = out_dir / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if _renders(rel):
            text = path.read_text(encoding="utf-8")
            target.write_text(render(text, ctx, rel), encoding="utf-8", newline="\n")
        else:
            shutil.copyfile(path, target)
        written.append(target.relative_to(out_dir).as_posix())
    (out_dir / "harness-answers.json").write_text(json.dumps(ctx["answers"], indent=2) + "\n", encoding="utf-8")
    written.append("harness-answers.json")
    if zip_path:
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(out_dir.rglob("*")):
                archive.write(path, path.relative_to(out_dir).as_posix())
    return written


def example_answers(vcs: str) -> dict:
    answers = {}
    for question in load_questions():
        example = question.get("example", {}).get(vcs) if isinstance(question.get("example"), dict) else \
            question.get("example")
        if example is None:
            example = question.get("default")
            if isinstance(example, dict):
                example = example["values"].get(vcs, example.get("else"))
        if example is not None:
            answers[question["id"]] = example
    answers["vcs"] = vcs
    return validate(answers)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--answers", help="answers JSON file")
    parser.add_argument("--out", help="output directory (replaced with --force)")
    parser.add_argument("--zip", help="also write this zip file")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--questions", action="store_true", help="print the question schema and exit")
    parser.add_argument("--example", choices=["git", "svn"], help="print example answers and exit")
    parser.add_argument("--check", help="validate this answers file and print the derived context")
    args = parser.parse_args(argv)
    try:
        if args.questions:
            print(json.dumps(load_questions(), indent=2, ensure_ascii=False))
            return 0
        if args.example:
            print(json.dumps(example_answers(args.example), indent=2, ensure_ascii=False))
            return 0
        if args.check:
            with open(args.check, encoding="utf-8") as handle:
                ctx = context(json.load(handle))
            print(json.dumps({k: v for k, v in ctx.items() if k != "answers"}, indent=2, ensure_ascii=False))
            return 0
        if not args.answers or not args.out:
            parser.error("--answers and --out are required to build")
        with open(args.answers, encoding="utf-8") as handle:
            answers = json.load(handle)
        written = build(answers, Path(args.out), Path(args.zip) if args.zip else None, args.force)
        print(f"wrote {len(written)} files to {args.out}" + (f" and {args.zip}" if args.zip else ""))
        return 0
    except BuildError as exc:
        print(f"build: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
