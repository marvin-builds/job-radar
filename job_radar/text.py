from __future__ import annotations

import html
import re


def html_to_text(raw: str) -> str:
    """Unescape (possibly double-escaped) HTML and strip tags."""
    text = html.unescape(html.unescape(raw or ""))
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


_LINK = re.compile(r"\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)")  # one level of (...) in URLs
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])|(?<!\w)_(?=\S)([^_]+?)(?<=\S)_(?!\w)")
_CODE = re.compile(r"`([^`]+)`")


def _inline(line: str) -> str:
    """Escape one line and turn its markdown marks into Telegram HTML; tags never leave the line."""
    links = []

    def keep_link(m):
        label, url = m.group(1), m.group(2)
        if not re.match(r"https?://", url):
            return label
        links.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=False)}</a>')
        return f"\x00{len(links) - 1}\x00"

    out = html.escape(_LINK.sub(keep_link, line), quote=False)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", out)
    return re.sub("\x00(\\d+)\x00", lambda m: links[int(m.group(1))], out)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def markdown_to_html(md: str) -> str:
    """Markdown (profiles, summaries) as Telegram HTML: headings bold, bullets •, tables as lines."""
    out, header = [], None
    for raw in md.strip().splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = _cells(stripped)
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue  # the line under the header
            if header is None:
                header = cells
                continue
            if len(header) == 2:  # a key/value table: "Stunden: 20-30"
                out.append("• " + _inline(": ".join(c for c in cells if c)))
                continue
            pairs = [f"{h}: {v}" if h else v for h, v in zip(header, cells) if v]
            out.append("• " + _inline(", ".join(pairs)))
            continue
        header = None
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            out.append("")
            continue
        heading = re.match(r"#{1,6}\s+(.*)", stripped)
        bullet = re.match(r"(\s*)[-*+]\s+(.*)", line)
        if heading:
            out.append(f"<b>{_inline(heading.group(1).strip().strip('#').strip())}</b>")
        elif bullet:
            indent = len(bullet.group(1).expandtabs(2)) // 2
            out.append("  " * indent + ("◦ " if indent else "• ") + _inline(bullet.group(2)))
        else:
            out.append(_inline(line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
