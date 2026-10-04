"""Turn feedback from the chat into a GitHub issue."""
from __future__ import annotations

from datetime import datetime

from . import config
from .http import post_json

API = "https://api.github.com/repos/{repo}/issues"
TITLE_MAX = 60


class NotConfigured(Exception):
    pass


def send(cfg: dict, text: str, env: dict | None = None, post=post_json, now=datetime.now) -> str:
    """Open an issue labelled "feedback"; returns its URL. Names the instance, not the person."""
    repo = cfg.get("feedback", {}).get("repo", "")
    token = (config.load_env() if env is None else env).get("GITHUB_TOKEN", "")
    if not repo or not token:
        raise NotConfigured("set feedback.repo in config.json and GITHUB_TOKEN in env")
    instance = cfg.get("instance") or "?"
    first = text.strip().splitlines()[0]
    if len(first) > TITLE_MAX:
        first = first[:TITLE_MAX - 1] + "…"
    body = f"{text.strip()}\n\n---\nInstanz: {instance} · {now():%Y-%m-%d %H:%M} · gesendet mit /feedback"
    issue = post(API.format(repo=repo), {"title": f"Feedback von {instance}: {first}", "body": body,
                                         "labels": ["feedback"]},
                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return issue["html_url"]
