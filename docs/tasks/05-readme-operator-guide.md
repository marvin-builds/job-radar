# 05 – README: operator guide

Status: open · Needs: 02 · Files: `README.md`

## Goal

The README describes the family setup, not my old single-user setup.

## Scope

- Intro: one shared bot, instances, invite links, German interview.
- Setup on a Raspberry Pi (64-bit Raspberry Pi OS / Debian 13): packages (`git`), Claude Code
  native installer + login, bot via @BotFather (once), `install.sh`, token into env,
  `invite <name>`, `install.sh --enable`, `loginctl enable-linger`.
- Adding a person: `invite <name>` → send them the link → they chat; enable their timer.
- Changing someone's search: they send `/setup`; or edit `instances/<name>/search.json` /
  `config.json` (layer table, whitelist).
- Removing a person: disable `job-radar-poll@<name>.timer`, delete their instance dirs.
- Update the command table (`--instance`, `invite`, no `setup-chat`), the chat section
  (German buttons, `/setup`, `/profile`), logs (`job-radar-poll@<name>`), the Pi numbers from
  `status.md` (RAM per Claude call, lock).
- Keep the Safety section; add: unknown chats are ignored, invites are one-time.
- No personal data (names of family members, my vault paths).
