"""Tell each person what is new after a deploy, from CHANGES.md."""
from __future__ import annotations

import html
import logging
import re
import subprocess
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


USER_FACING = ("job_radar/", "prompts/", "systemd/")
NO_NEWS = re.compile(r"^No-news:[ \t]*\S", re.MULTILINE)


def unannounced(base: str, head: str, repo: Path = PATH.parent) -> list[str]:
    """Main-line commits in base..head ("<sha> <subject>") that change code people use but
    neither touch CHANGES.md nor say why not with a `No-news: <reason>` line.

    Each merge is judged on everything it brought in, so branch commits never count alone."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout

    missing = []
    for sha in git("rev-list", "--first-parent", f"{base}..{head}").split():
        paths = git("diff", "--name-only", f"{sha}^1", sha).split()
        if "CHANGES.md" in paths or not any(p.startswith(USER_FACING) for p in paths):
            continue
        if not NO_NEWS.search(git("log", "-1", "--format=%B", sha)):
            missing.append(git("log", "-1", "--format=%h %s", sha).strip())
    return missing
