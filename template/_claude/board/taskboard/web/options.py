"""
The options of a question as the board offers them as buttons. A question lists its options in the text as
`(a) ... (b) ... (c) ...` (letters in order from a, at least two); the option marked `(recommended ...)` is the
recommended one. A question may continue on indented lines; the options may span them.
"""

import re
from typing import NamedTuple

from ..model import QUESTION

_MARKER = re.compile(r"(?<!\S)\(([a-z])\)(?!\S)")
_RECOMMENDED = re.compile(r"\(\s*recommended\b", re.IGNORECASE)
_QUESTION_START = re.compile(r"^- \*\*(Q\d+)\*\*")


class Option(NamedTuple):
    letter: str
    text: str
    recommended: bool


def question_texts(section: str) -> dict[str, str]:
    """The full text of every question in a Questions section by id: the rest of its line and its indented
    continuation lines, as one line of text."""
    texts: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in section.split("\n"):
        start = _QUESTION_START.match(line)
        if start:
            m = QUESTION.match(line)
            current = texts.setdefault(start.group(1), [])
            if m:
                current.append(line[m.end():])
        elif current is not None and line.strip() and line[:1] in (" ", "\t"):
            current.append(line)
        else:
            current = None
    return {qid: " ".join(" ".join(parts).split()) for qid, parts in texts.items()}


def _without_recommended(text: str) -> tuple[str, bool]:
    """text without its `(recommended ...)` group (balanced parentheses) and whether it had one."""
    m = _RECOMMENDED.search(text)
    if not m:
        return text, False
    depth = 0
    end = len(text)
    for i in range(m.start(), len(text)):
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        if depth == 0:
            end = i + 1
            break
    return text[:m.start()] + " " + text[end:], True


def parse_options(text: str) -> tuple[str, list[Option]]:
    """
    (the question text before its options, the options) of a question's text. No options (an empty list, the
    whole text first) when the text has no `(a)` followed by `(b)`, or when an option has no text.
    """
    text = " ".join(text.split())
    markers = [(m.start(), m.end(), m.group(1)) for m in _MARKER.finditer(text)]
    # The last `(a)` that is followed by a `(b)`: an earlier `(a)` is a reference in the question's own text.
    for first in reversed(range(len(markers))):
        start, end, letter = markers[first]
        if letter != "a":
            continue
        chain = [(start, end, letter)]
        for marker in markers[first + 1:]:
            if marker[2] == chr(ord(chain[-1][2]) + 1):
                chain.append(marker)
        if len(chain) < 2:
            continue
        options = []
        for i, (_, option_start, option_letter) in enumerate(chain):
            option_end = chain[i + 1][0] if i + 1 < len(chain) else len(text)
            body, recommended = _without_recommended(text[option_start:option_end])
            body = " ".join(body.split()).rstrip(" ;,")
            if not body:
                return text, []
            options.append(Option(option_letter, body, recommended))
        return text[:start].strip(), options
    return text, []
