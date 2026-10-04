from __future__ import annotations

import json
import urllib.request

USER_AGENT = "job-radar/0.1 (personal job search)"


def get_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def post_json(url: str, payload: dict, timeout: int = 60):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)
