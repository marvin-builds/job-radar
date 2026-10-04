"""Tell each person what is new after a deploy, from CHANGES.md."""
from __future__ import annotations

import html
import logging
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "CHANGES.md"
STATE_KEY = "changes_seen"
log = logging.getLogger("job-radar")


def parse(text: str) -> list[tuple[str, list[str]]]:
    """`## <id>` headings with `- ` bullets, newest first as in the file."""
    sections = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif line.startswith("- ") and sections:
            sections[-1][1].append(line[2:].strip())
    return sections


def load(path: Path = PATH) -> list[tuple[str, list[str]]]:
    return parse(path.read_text(encoding="utf-8"))


def _unseen(store, sections):
    seen = store.get_state(STATE_KEY)
    ids = [sid for sid, _ in sections]
    if seen not in ids:  # no record yet: someone who was here before this feature
        return sections[:1]
    return sections[:ids.index(seen)]


def announce(store, bot, sections) -> None:
    """Send the sections this person has not seen, as one message; retried next start if it fails."""
    new = _unseen(store, sections)
    bullets = [b for _, items in new for b in items]
    if not bullets:
        return
    text = "🆕 <b>Neu im Job-Radar</b>\n\n" + "\n".join(f"• {html.escape(b, quote=False)}" for b in bullets)
    try:
        bot.send(text)
    except Exception as e:
        log.warning("could not send changes: %s", e)
        return
    store.set_state(STATE_KEY, sections[0][0])


def mark_seen(store, sections) -> None:
    """A new person starts with the current version, without a backlog."""
    if sections:
        store.set_state(STATE_KEY, sections[0][0])
