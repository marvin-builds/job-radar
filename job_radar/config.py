"""Local config and secrets in ~/.config/job-radar/, none of it committed.

Settings merge in three layers: DEFAULTS < search.json (search settings written by the
Telegram interview, whitelisted keys only) < config.json (operator overrides, see
config.example.json). Secrets live in env.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("job-radar")


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "job-radar"


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "job-radar"


DEFAULTS = {
    "db_path": str(data_dir() / "radar.db"),
    "sources": {
        "ba": {"enabled": True, "keywords": [], "areas": [{}],
               "published_since_days": 7, "page_size": 50},
        "arbeitnow": {"enabled": True, "max_pages": 2},
    },
    "filter": {
        "include": [],
        "exclude_title": [],
        "locations": [],
        "contract_types": ["permanent", "freelance", ""],
    },
    "notify": {"max_per_run": 5},
    "triage": {"enabled": True, "model": "haiku", "threshold": 6, "max_per_run": 40},
    "interview": {"model": "sonnet"},
    "report": {"model": "sonnet", "reports_dir": str(data_dir() / "reports"), "timeout_s": 900},
    "tracker_path": "",
    "profile_files": [],
}

# What the interview may set in search.json; anything else is rejected.
_WORDS = "words"
_SEARCH_SCHEMA = {
    "sources": {"ba": {"keywords": _WORDS,
                       "areas": [{"where": str, "radius_km": int, "keywords": _WORDS}],
                       "extra_params": {"arbeitszeit": str, "befristung": str, "zeitarbeit": str,
                                        "pav": str, "angebotsart": str}}},
    "filter": {"include": _WORDS, "exclude_title": _WORDS, "locations": _WORDS, "contract_types": _WORDS},
}


def _check(value, schema, where: str) -> None:
    if schema is _WORDS:
        if not (isinstance(value, list) and all(isinstance(w, str) for w in value)):
            raise ValueError(f"{where}: expected a list of strings")
    elif isinstance(schema, list):
        if not isinstance(value, list):
            raise ValueError(f"{where}: expected a list")
        for i, item in enumerate(value):
            _check(item, schema[0], f"{where}[{i}]")
    elif isinstance(schema, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{where}: expected an object")
        for k, v in value.items():
            if k not in schema:
                raise ValueError(f"{where}.{k}: not allowed in search settings")
            _check(v, schema[k], f"{where}.{k}")
    elif not isinstance(value, schema) or isinstance(value, bool):
        raise ValueError(f"{where}: expected {schema.__name__}")


def validate_search(data: dict) -> dict:
    _check(data, _SEARCH_SCHEMA, "search")
    return data


def write_search(path: Path, data: dict) -> None:
    validate_search(data)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load(path: Path | None = None) -> dict:
    path = Path(path or config_dir() / "config.json")
    try:
        search = validate_search(_read(path.with_name("search.json")))
    except ValueError as e:
        log.warning("ignoring search.json: %s", e)
        search = {}
    cfg = _merge(_merge(DEFAULTS, search), _read(path))
    cfg["profile_files"] = cfg["profile_files"] or [str(path.with_name("profile.md"))]
    cfg["config_dir"] = str(path.parent)
    return _expand(cfg)


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
