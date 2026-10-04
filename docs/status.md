# Status (2026-10-04)

Branch `generalize-onboarding`, all work so far, merged into `main` at the end of task 01. Remote: private repo
`git@github.com:marvinmai/job-radar.git` (`main` and this branch pushed).
`python3 -m unittest`: 124 tests pass.

## Done

- [x] Phase 0, Pi feasibility. Pi 3B+, Debian 13 aarch64, Python 3.13, Claude Code 2.1.289:
  tests pass; dry-run poll with Haiku triage 15 s; Sonnet deep report 79 s, $0.25 list price;
  peak 270 MB per Claude process, no swap; interview ~10 s per question, ~4 min in total.
- [x] Generic defaults, no IT-specific search terms; 3-layer config with `search.json` whitelist.
- [x] Prompts work for any field; report language follows the profile; triage notes in German.
- [x] Interview: `interview.turn`, `prompts/interview.md`, Telegram flow (`/start` without
  profile, `/setup`, `/profile`, confirm/change buttons, match count after confirm,
  one-time "/setup" nudge from poll).
- [x] Interview prompt tuned on real data: short BA keywords without "/in", no
  `arbeitszeit` filter. Persona "Anna, nurse in Kassel" got 45 matching postings.
- [x] All fixed bot texts in German with "du".
- [x] Shared Claude lock (`claude.LOCK_PATH`, waits up to 30 min).
- [x] Instances and invites in `config.py`; `Registry`, `dispatch`, shared `listen` in `app.py`.

## Broken right now

- `python3 -m job_radar listen` crashes: `cli.py` still calls the old
  `app.listen(cfg, store, bot)`. Fixed by task 01.
- `cli.py` still uses the single-instance layout (`--config`, `setup-chat`), and the systemd
  units and `install.sh` are single-instance. Tasks 01 and 02.

## Environment

- **Laptop**: my old radar is stopped (timer and listener inactive). Its config
  `~/.config/job-radar/config.json` holds my search settings and the vault profile paths;
  `~/.config/job-radar/env` holds the bot token. Source for task 04.
- **Pi** (`ssh pi`, user `marvin`): git, GNU time and Claude Code installed, Claude logged in
  with my account. `~/job-radar` tracks `origin` = GitHub through a read-only deploy key
  (`~/.ssh/job_radar_deploy`, used for `github.com` via `~/.ssh/config`; on GitHub as
  "pi (read-only)"). Update with `git pull`; the Pi cannot push. No job-radar units
  installed yet. `~/.config/job-radar/` holds the **test persona** in the old layout
  (config.json, profile.md, search.json); delete it in task 03. Locale warnings over SSH are
  harmless (`export LC_ALL=C.UTF-8`).

## Open tasks

| # | Task | Needs |
| --- | --- | --- |
| 01 | [CLI for instances](tasks/01-cli-instances.md) | – |
| 02 | [systemd units and install scripts](tasks/02-systemd-and-install.md) | 01 |
| 03 | [Deploy on the Pi, end-to-end test](tasks/03-pi-deploy-and-e2e.md) | 01, 02 |
| 04 | [Move my own radar to the Pi](tasks/04-migrate-my-instance.md) | 03 |
| 05 | [README: operator guide](tasks/05-readme-operator-guide.md) | 02 (can run parallel to 03) |
| 06 | [Final check](tasks/06-final-check.md) | 01–05 merged |
