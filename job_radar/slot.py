"""The interview slot: one interview at a time across all instances, so the small machine
answers one person quickly instead of interleaving everyone's turns on the Claude lock.

The slot is a small JSON file next to claude.lock ({"owner": <instance>, "at": <last activity>}),
changed under an flock. An owner who stays silent for TIMEOUT_S loses it."""
from __future__ import annotations

import contextlib
import fcntl
import json
import time
from pathlib import Path

PATH: Path | None = None  # set by the CLI; None (tests, one-off commands) means no limit
TIMEOUT_S = 30 * 60


@contextlib.contextmanager
def _locked():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PATH.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _holder() -> str | None:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        fresh = time.time() - float(data["at"]) < TIMEOUT_S
        return str(data["owner"]) if fresh else None
    except (OSError, ValueError, KeyError, TypeError):  # missing or broken: free
        return None


def take(owner: str) -> bool:
    """Take or refresh the slot for owner; False while someone else holds it."""
    if PATH is None:
        return True
    with _locked():
        if _holder() not in (None, owner):
            return False
        PATH.write_text(json.dumps({"owner": owner, "at": time.time()}), encoding="utf-8")
        return True


def holds(owner: str) -> bool:
    if PATH is None:
        return True
    with _locked():
        return _holder() == owner


def release(owner: str) -> None:
    if PATH is None:
        return
    with _locked():
        if _holder() == owner:
            PATH.unlink(missing_ok=True)
