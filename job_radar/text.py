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
