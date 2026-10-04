"""Local config (~/.config/job-radar/config.json) and secrets (~/.config/job-radar/env).

Neither is committed. See config.example.json for all keys.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "job-radar"


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "job-radar"


DEFAULTS = {
    "db_path": str(data_dir() / "radar.db"),
    "sources": {
        "ba": {"enabled": True, "keywords": ["Kotlin", "Java Backend"], "areas": [{}],
               "published_since_days": 7, "page_size": 50},
        "arbeitnow": {"enabled": True, "max_pages": 2},
    },
    "filter": {
        "include": ["kotlin", "java", "backend", "spring", "platform engineer", "integration"],
        "exclude_title": ["junior", "werkstudent", "praktikum", "ausbildung", "intern", "trainee"],
        "locations": [],
        "contract_types": ["permanent", "freelance", ""],
    },
    "triage": {"enabled": True, "model": "haiku", "threshold": 6, "max_per_run": 40},
    "report": {"model": "sonnet", "reports_dir": "", "timeout_s": 900},
    "tracker_path": "",
    "profile_files": [],
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _expand(cfg: dict) -> dict:
    cfg["db_path"] = os.path.expanduser(cfg["db_path"])
    cfg["report"]["reports_dir"] = os.path.expanduser(cfg["report"]["reports_dir"])
    cfg["tracker_path"] = os.path.expanduser(cfg["tracker_path"])
    cfg["profile_files"] = [os.path.expanduser(p) for p in cfg["profile_files"]]
    return cfg


def load(path: Path | None = None) -> dict:
    path = Path(path or config_dir() / "config.json")
    user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _expand(_merge(DEFAULTS, user))


def load_env(path: Path | None = None) -> dict:
    path = Path(path or config_dir() / "env")
    if not path.exists():
        return {}
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def set_env_value(path: Path, key: str, value: str) -> None:
    """Set KEY=value in the env file, keeping other lines and the file mode."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}"); done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
