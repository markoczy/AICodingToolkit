"""
The task state machine: which moves are allowed, what each target state requires, and what a move sets.

This is the executable form of the state table in AI/Tasks/README.md and the move rules in
AI/Methodology.md; keep the three in step.
"""

from .model import STATES
from .vcs import DEFAULT_REVIEW, ReviewField

C, R, RD, IMPL, CR, UR, DEP, DONE, DROP = STATES

ALLOWED = {
    C: [R, DROP],
    R: [C, RD, UR, DROP],
    RD: [R, IMPL, DROP],
    IMPL: [R, RD, CR, UR, DROP],
    CR: [IMPL, UR, DEP, DONE, DROP],
    UR: [R, RD, IMPL, CR, DEP, DONE, DROP],
    DEP: [IMPL, DONE],
    DONE: [],
    DROP: [C, R],
}

# Per-type overrides of the move rules; a type without an entry follows ALLOWED and the preconditions unchanged.
# The rules come from `type_rules` in board.json (validated by config.py, same shape as here); this default is the
# fallback when board.json has no `type_rules` key. An entry has these keys, all optional:
#   moves:         {src: [targets]} replaces ALLOWED[src] for the type
#   refuse:        {src: [dst]} moves the type's path leaves out; refused with a message that names the type
#   no_branch:     the type moves 02 -> 03 without a branch (nobody branches; the agent reports to the supervisor)
#   tokens_from:   [src] states from which a move into 07-Done needs --tokens (agents ran, no review carries
#                  the count)
DEFAULT_TYPE_RULES = {
    "research": {
        "moves": {IMPL: [R, RD, UR, DONE, DROP], UR: [R, RD, IMPL, DONE, DROP]},
        "refuse": {IMPL: [CR, DEP], UR: [CR, DEP]},
        "no_branch": True,
        "tokens_from": [IMPL, UR],
    },
}
TYPE_RULE_KEYS = {"moves", "refuse", "no_branch", "tokens_from"}

# Moves that go back in the flow; they always need a reason.
BACKWARD = {(R, C), (RD, R), (IMPL, R), (IMPL, RD), (CR, IMPL), (DEP, IMPL), (UR, R), (UR, RD), (UR, IMPL),
            (DROP, C), (DROP, R)}

# waiting_on after a move into a state (None = nobody); an open question for the user always wins.
WAITING_ON = {
    C: None,
    R: "supervisor",
    RD: None,
    IMPL: "executor",
    CR: "reviewer",
    UR: "user",
    DEP: "supervisor",
    DONE: None,
    DROP: None,
}


def resolve_state(name: str) -> str:
    """A state from its full name, its number (`02`, `2`) or its name without the number (`ready`)."""
    key = name.strip().lower()
    for state in STATES:
        number, label = state.split("-", 1)
        if key in (state.lower(), number, str(int(number)), label.lower()):
            return state
    raise ValueError(f"Unknown state {name!r}; one of {', '.join(STATES)}")


def type_rules(task_type: str | None, rules: dict | None = None) -> dict:
    """The override entry of a task type in `rules` (the config's type_rules; the default when None)."""
    return (DEFAULT_TYPE_RULES if rules is None else rules).get(task_type or "", {})


def allowed_moves(task_type: str | None, state: str, rules: dict | None = None) -> list[str]:
    """The states a task of this type can move to from `state`."""
    return type_rules(task_type, rules).get("moves", {}).get(state, ALLOWED[state])


def move_problems(task: dict, src: str, dst: str, reason: str | None, tokens: str | None,
                  type_rules_config: dict | None = None, review: ReviewField | None = None) -> list[str]:
    """
    Why task (a parse_task() entry, with the header values the move will set) can't move src -> dst.
    type_rules_config is the board config's type_rules (None: DEFAULT_TYPE_RULES); review is the header field of
    the code review (vcs.review_field; None: `pr`, git's pull request number).
    """
    problems: list[str] = []
    header = task["header"]
    review = review or DEFAULT_REVIEW
    if dst == src:
        return [f"the task is already in {dst}"]
    task_type = header.get("type")
    rules = type_rules(task_type, type_rules_config)
    if dst in rules.get("refuse", {}).get(src, ()):
        return [f"{src} -> {dst} is not on the path of a {task_type} task (from {src}: "
                f"{', '.join(allowed_moves(task_type, src, type_rules_config))})"]
    allowed_list = allowed_moves(task_type, src, type_rules_config)
    if dst not in allowed_list:
        allowed = ", ".join(allowed_list) or "nothing (final state)"
        return [f"{src} -> {dst} is not an allowed move (from {src}: {allowed})"]
    if ((src, dst) in BACKWARD or dst == DROP) and not reason:
        problems.append(f"{src} -> {dst} needs a reason (--reason)")
    open_q = [q for q in task["questions"] if q["status"] == "open"]
    open_user = [q["id"] for q in open_q if q["for"] == "user"]
    sections = task["sections"]

    if dst in (RD, IMPL):
        if open_q:
            problems.append(f"open question(s) {', '.join(q['id'] for q in open_q)}: answer or drop them first")
        for name in ("Problem", "Approach", "Acceptance"):
            if not sections.get(name, "").strip():
                problems.append(f"the {name} section is empty")
        if header.get("blocked_by"):
            problems.append(f"blocked by {', '.join(header['blocked_by'])}")
    if dst == IMPL and not header.get("branch") and not rules.get("no_branch"):
        problems.append("no branch (--branch)")
    if dst == CR and not header.get("branch"):
        problems.append("no branch (--branch)")
    if dst == CR and not header.get(review.name):
        problems.append(review.missing)
    if dst == UR and not open_user:
        problems.append("05-UserReview means waiting for the user: ask the user a question first (`ask`)")
    if dst in (DEP, DONE) and src in (CR, UR) and header.get(review.name) and not reason:
        problems.append(f"say how the change landed (--reason, e.g. '{review.landing_example}')")
    if dst == DONE:
        if open_q:
            problems.append(f"open question(s) {', '.join(q['id'] for q in open_q)}")
        if header.get(review.name) and not tokens:
            problems.append("a task with a reviewed branch needs its agent token counts at Done (--tokens, e.g. "
                            "'executor 120k, reviewer 45k (estimate ~150k)'; 'n/a' if unknown)")
        if src in rules.get("tokens_from", ()) and not tokens:
            problems.append(f"a {task_type} task needs its agent token counts at Done (--tokens, e.g. "
                            "'analyst 60k'; 'n/a' if unknown); a --reason 'result: <answer>' is recommended")
    return problems


def waiting_after_move(task: dict, dst: str) -> str | None:
    if any(q["status"] == "open" and q["for"] == "user" for q in task["questions"]):
        return "user"
    return WAITING_ON[dst]
