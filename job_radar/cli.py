from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import app, config
from .store import Store
from .telegram import Bot


def doctor(cfg: dict, env: dict, which=shutil.which) -> bool:
    checks = [
        ("TELEGRAM_BOT_TOKEN set", bool(env.get("TELEGRAM_BOT_TOKEN"))),
        ("TELEGRAM_CHAT_ID set", bool(env.get("TELEGRAM_CHAT_ID"))),
        ("claude on PATH", bool(which("claude"))),
        (f"reports dir {cfg['report']['reports_dir'] or '(unset)'}", bool(cfg["report"]["reports_dir"])),
    ]
    checks += [(f"profile file {f}", Path(f).exists()) for f in cfg.get("profile_files", [])]
    if not cfg.get("profile_files"):
        checks.append(("profile_files configured", False))
    try:
        Store(Path(cfg["db_path"])).close()
        checks.append((f"database {cfg['db_path']}", True))
    except Exception:
        checks.append((f"database {cfg['db_path']}", False))
    for label, ok in checks:
        print(f"{'ok ' if ok else 'MISSING'}  {label}")
    return all(ok for _, ok in checks)


def _bot(env: dict) -> Bot:
    if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_CHAT_ID"):
        sys.exit(f"Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in {config.config_dir() / 'env'}")
    return Bot(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="job-radar")
    parser.add_argument("--config", type=Path, help="config file (default ~/.config/job-radar/config.json)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_poll = sub.add_parser("poll", help="fetch, filter, triage and notify once")
    p_poll.add_argument("--dry-run", action="store_true", help="print instead of sending; nothing is stored")
    sub.add_parser("listen", help="react to Telegram buttons (long-running)")
    p_report = sub.add_parser("report", help="write a deep report for a stored posting id")
    p_report.add_argument("id", type=int)
    sub.add_parser("test-notify", help="send a test message")
    sub.add_parser("doctor", help="check config, secrets and tools")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg, env = config.load(args.config), config.load_env()

    if args.cmd == "doctor":
        return 0 if doctor(cfg, env) else 1
    if args.cmd == "poll" and args.dry_run:
        store = Store(Path(":memory:"))
        print(app.poll(cfg, store, bot=None, dry_run=True))
        return 0
    bot, store = _bot(env), Store(Path(cfg["db_path"]))
    if args.cmd == "poll":
        print(app.poll(cfg, store, bot))
    elif args.cmd == "listen":
        app.listen(cfg, store, bot)
    elif args.cmd == "report":
        p = store.get(args.id)
        if p is None:
            sys.exit(f"no posting {args.id}")
        app.run_report(cfg, store, bot, p)
    elif args.cmd == "test-notify":
        bot.send("✅ job-radar test message")
    return 0
