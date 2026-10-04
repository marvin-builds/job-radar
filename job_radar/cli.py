from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from . import app, claude, config
from .store import PollLocked, Store
from .http import post_json
from .telegram import API, Bot


def _print_checks(checks: list[tuple[str, bool]]) -> bool:
    for label, ok in checks:
        print(f"{'ok ' if ok else 'MISSING'}  {label}")
    return all(ok for _, ok in checks)


def _has_claude(which) -> bool:
    """PATH, or ~/.local/bin, which the systemd units add (a plain SSH command lacks it)."""
    return bool(which("claude") or which("claude", path=str(Path.home() / ".local" / "bin")))


def doctor(cfg: dict, env: dict, which=shutil.which) -> bool:
    checks = [
        ("TELEGRAM_BOT_TOKEN set", bool(env.get("TELEGRAM_BOT_TOKEN"))),
        ("TELEGRAM_CHAT_ID set", bool(env.get("TELEGRAM_CHAT_ID"))),
        ("claude on PATH or in ~/.local/bin", _has_claude(which)),
        (f"reports dir {cfg['report']['reports_dir'] or '(unset)'}", bool(cfg["report"]["reports_dir"])),
    ]
    profile = Path(cfg["config_dir"]) / "profile.md" if cfg.get("config_dir") else None
    for f in cfg.get("profile_files", []):
        if Path(f) == profile and not profile.exists():
            print(f"todo     profile {f}: run /setup in Telegram")
        else:
            checks.append((f"profile file {f}", Path(f).exists()))
    if not cfg.get("profile_files"):
        checks.append(("profile_files configured", False))
    try:
        Store(Path(cfg["db_path"])).close()
        checks.append((f"database {cfg['db_path']}", True))
    except Exception:
        checks.append((f"database {cfg['db_path']}", False))
    return _print_checks(checks)


def doctor_shared(env: dict, which=shutil.which) -> bool:
    ok = _print_checks([
        (f"TELEGRAM_BOT_TOKEN set in {config.config_dir() / 'env'}", bool(env.get("TELEGRAM_BOT_TOKEN"))),
        ("claude on PATH or in ~/.local/bin", _has_claude(which)),
    ])
    names = config.instances()
    print(f"instances: {len(names)}")
    for name in names:
        bound = "chat bound" if config.instance_env(name).get("TELEGRAM_CHAT_ID") else "no chat (invite not used yet)"
        print(f"  {name}: {bound}")
    print(f"{config.open_invites()} open invite(s)")
    return ok


def _token(env: dict) -> str:
    if not env.get("TELEGRAM_BOT_TOKEN"):
        sys.exit(f"Put TELEGRAM_BOT_TOKEN=<token from BotFather> into {config.config_dir() / 'env'} first")
    return env["TELEGRAM_BOT_TOKEN"]


def invite(name: str, env: dict, post=post_json) -> int:
    token = _token(env)
    username = post(API.format(token=token, method="getMe"), {}, timeout=30)["result"]["username"]
    code = config.create_invite(name)
    print(f"Send {name} this link (works once):\nhttps://t.me/{username}?start={code}\n")
    print(f"Then start the hourly poll:\nsystemctl --user enable --now job-radar-poll@{name}.timer")
    return 0


def _instance(args) -> str:
    name = args.instance or os.environ.get("JOB_RADAR_INSTANCE")
    if not name:
        sys.exit(f"'{args.cmd}' needs --instance NAME (or JOB_RADAR_INSTANCE); instances: "
                 f"{', '.join(config.instances()) or 'none'}")
    if name not in config.instances():
        sys.exit(f"no instance {name!r}; create it with: job-radar invite {name}")
    return name


def _bot(env: dict, name: str) -> Bot:
    if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_CHAT_ID"):
        sys.exit(f"Set TELEGRAM_BOT_TOKEN in {config.config_dir() / 'env'} "
                 f"and TELEGRAM_CHAT_ID in {config.config_dir(name) / 'env'}")
    return Bot(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])


def main(argv: list[str] | None = None, post=post_json, which=shutil.which) -> int:
    parser = argparse.ArgumentParser(prog="job-radar")
    parser.add_argument("--instance", help="the person's instance (default: env JOB_RADAR_INSTANCE)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_poll = sub.add_parser("poll", help="fetch, filter, triage and notify once")
    p_poll.add_argument("--dry-run", action="store_true", help="print instead of sending; nothing is stored")
    sub.add_parser("listen", help="react to Telegram messages of all instances (long-running)")
    p_report = sub.add_parser("report", help="write a deep report for a stored posting id")
    p_report.add_argument("id", type=int)
    sub.add_parser("test-notify", help="send a test message")
    sub.add_parser("doctor", help="check config, secrets and tools (shared, or one instance)")
    sub.add_parser("usage", help="show AI usage for today, 7 and 30 days")
    p_invite = sub.add_parser("invite", help="create an instance and print its one-time Telegram link")
    p_invite.add_argument("name")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    claude.LOCK_PATH = config.data_dir() / "claude.lock"
    shared_env = config.load_env(config.config_dir() / "env")

    if args.cmd == "invite":
        return invite(args.name, shared_env, post)
    if args.cmd == "listen":
        token = _token(shared_env)
        registry = app.Registry(token)
        try:
            app.listen(registry, Bot(token, ""))
        finally:
            registry.close()
        return 0
    if args.cmd == "doctor" and not (args.instance or os.environ.get("JOB_RADAR_INSTANCE")):
        return 0 if doctor_shared(shared_env, which) else 1

    name = _instance(args)
    cfg, env = config.load_instance(name), config.instance_env(name)
    if args.cmd == "doctor":
        return 0 if doctor(cfg, env, which) else 1
    if args.cmd == "usage":
        print(re.sub(r"</?[bi]>", "", app.usage_text(store := Store(Path(cfg["db_path"])), probe=lambda: claude.probe_limits(
            on_usage=lambda u: store.record_usage("probe", u)))))
        return 0
    if args.cmd == "poll" and args.dry_run:
        store = Store(Path(":memory:"))
        print(app.poll(cfg, store, bot=None, dry_run=True))
        return 0
    if args.cmd == "poll" and not env.get("TELEGRAM_CHAT_ID"):
        print(f"skipped: no chat yet for {name} (invite not used)")
        return 0
    bot, store = _bot(env, name), Store(Path(cfg["db_path"]))
    if args.cmd == "poll":
        try:
            print(app.locked_poll(cfg, store, bot))
        except PollLocked as e:
            print(f"skipped: {e}")
    elif args.cmd == "report":
        p = store.get(args.id)
        if p is None:
            sys.exit(f"no posting {args.id}")
        app.run_report(cfg, store, bot, p)
    elif args.cmd == "test-notify":
        bot.send("✅ job-radar test message")
    return 0
