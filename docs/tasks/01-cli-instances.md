# 01 – CLI for instances

Status: open · Needs: – · Files: `job_radar/cli.py`, `tests/test_cli.py` · Branch: `generalize-onboarding` (existing; merge into `main` when done)

## Goal

`python3 -m job_radar` works with the instance layout from `plan.md`.

## Scope

- Global `--instance NAME`, default from env `JOB_RADAR_INSTANCE`. Remove `--config`.
- Per-instance commands use `config.load_instance(name)` and `config.instance_env(name)`:
  `poll [--dry-run]`, `report <id>`, `test-notify`, `usage`, `doctor`.
  - `poll` for an instance without `TELEGRAM_CHAT_ID` (invite not redeemed): print
    "skipped: no chat yet" and exit 0, so the timer doesn't fail. `poll --dry-run` works without a chat
    (it sends nothing), so an instance can be checked before its invite is used.
  - `doctor` without `--instance`: shared checks (token, `claude` on PATH, `instances()` with
    chat bound yes/no, open invites). With `--instance`: the current checks for that instance.
    A missing `profile.md` shows "run /setup in Telegram" rather than reading as a broken config.
- `listen` (no instance): `Bot(token, "")` as receiver, `app.Registry(token)`,
  `app.listen(registry, receiver)`. Token from `config.load_env(config.config_dir() / "env")`.
- New `invite NAME`: `config.create_invite(name)`, get the bot's username via Telegram
  `getMe`, print `https://t.me/<username>?start=<code>`, then the hint
  `systemctl --user enable --now job-radar-poll@NAME.timer`.
- Remove `setup-chat` and `find_chats` (invites replace them) and their test.
- In `main`, set `claude.LOCK_PATH = config.data_dir() / "claude.lock"` before any command runs.
- `PollLock` stays per instance (it sits next to the instance's db).

## Done when

- Tests (first) for: instance resolution (flag vs env, missing → clear error for
  per-instance commands), poll skip without chat, invite prints the link (fake `post`
  returning `getMe`), lock path set by `main`.
- `python3 -m unittest` passes; `python3 -m job_radar --help` lists the commands.
- `XDG_CONFIG_HOME=/tmp/x XDG_DATA_HOME=/tmp/y python3 -m job_radar invite test` with a test
  token prints a link.
- Then the first merge: `/code-review` on `generalize-onboarding` (it holds all work so far),
  fix findings, ask me about squashing (never rewrite history unasked), then ask to merge
  into `main` and push.
