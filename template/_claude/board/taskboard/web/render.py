"""
Markdown to HTML. Task files get a minimal subset: headings, `- ` lists (indented lines continue an item), fenced
code blocks, paragraphs, **bold**, `code` and links on the `*.md` paths link_path knows (plans). Everything is
HTML-escaped first; nothing else is interpreted. Plans get CommonMark with tables (markdown-it, raw HTML escaped).
"""

import re
from typing import Callable, Optional

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

_HEADING = re.compile(r"^(#{1,6}) (.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_PATH = re.compile(r"(?<![\w./-])([\w./-]+\.md)\b")
_KEPT_LINK = re.compile(r"^(?:https?:|mailto:|#)", re.IGNORECASE)

LinkPath = Callable[[str], Optional[str]]


def link_paths(html: str, link_path: LinkPath | None) -> str:
    """Every `*.md` path in already escaped text that link_path turns into a URL becomes a link."""
    if link_path is None:
        return html

    def link(m: re.Match) -> str:
        url = link_path(m.group(1))
        return f'<a href="{escape(url)}">{m.group(1)}</a>' if url else m.group(1)
    return _PATH.sub(link, html)


def render_inline(text: str, link_path: LinkPath | None = None) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{link_paths(str(escape(part[1:-1])), link_path)}</code>")
        else:
            out.append(link_paths(_BOLD.sub(r"<strong>\1</strong>", str(escape(part))), link_path))
    return "".join(out)


def render_document(text: str, link_href: LinkPath) -> Markup:
    """
    A plan as HTML: CommonMark with tables and strikethrough, raw HTML shown as text. link_href turns a relative
    link target into a board URL; when it gives None the link stays plain text. http(s), mailto and #anchors stay.
    """
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    tokens = md.parse(text)
    for token in tokens:
        for child in token.children or []:
            if child.type != "link_open":
                continue
            href = str(child.attrGet("href") or "")
            if _KEPT_LINK.match(href):
                continue
            url = link_href(href)
            if url:
                child.attrSet("href", url)
            else:
                child.attrs.pop("href", None)
    return Markup(md.renderer.render(tokens, md.options, {}))


def render_markdown(text: str, link_path: LinkPath | None = None) -> Markup:
    lines = text.replace("\r\n", "\n").split("\n")
    html: list[str] = []
    paragraph: list[str] = []
    items: list[list[str]] = []

    def flush() -> None:
        if paragraph:
            html.append("<p>" + render_inline(" ".join(s.strip() for s in paragraph), link_path) + "</p>")
            paragraph.clear()
        if items:
            html.append("<ul>" + "".join("<li>" + render_inline(" ".join(s.strip() for s in item), link_path)
                                         + "</li>" for item in items) + "</ul>")
            items.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            flush()
            block = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                block.append(lines[i])
                i += 1
            html.append("<pre><code>" + str(escape("\n".join(block))) + "</code></pre>")
        elif not line.strip():
            flush()
        elif _HEADING.match(line):
            flush()
            m = _HEADING.match(line)
            level = len(m.group(1))
            html.append(f"<h{level}>{render_inline(m.group(2), link_path)}</h{level}>")
        elif line.startswith("- ") or line.startswith("* "):
            if paragraph:
                flush()
            items.append([line[2:]])
        elif items and line[:1] in (" ", "\t"):
            items[-1].append(line)
        else:
            if items:
                flush()
            paragraph.append(line)
        i += 1
    flush()
    return Markup("\n".join(html))
