# Status (2026-10-05)

One short branch per task, merged into `main` when the task is done. Open tasks are
[GitHub issues](https://github.com/marvinmai/job-radar/issues?q=is%3Aopen+label%3Atask); the
task files of 01–08 are in the git history (`docs/tasks/`, removed in the move to issues). Remote: private repo `git@github.com:marvinmai/job-radar.git`.
`python3 -m unittest`: 180 tests pass.

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
- [x] Task 01: CLI per instance (`--instance` / `JOB_RADAR_INSTANCE`, `invite`, shared and
  per-instance `doctor`, `poll` skips an instance without chat); `setup-chat` and `--config`
  removed. Review fixes: each chat runs on its own worker thread (`app.ChatWorkers`), so a
  Claude call waiting for the lock no longer freezes the bot for everyone; one broken instance
  config no longer drops the others; a failed portal fetch after setup is not reported as
  "no matches"; the interview must deliver search terms and include words; `/setup` drops an
  old proposal.
- [x] Task 02: systemd templates `job-radar-poll@<name>.service/.timer` (60 min timeout for
  the Claude lock, 10 min random delay) and one `job-radar-listen.service`; `install.sh
  --enable` starts every instance's timer and drops the old single-instance units;
  `disable.sh` stops all instance timers. Checked with `systemd-analyze --user verify`;
  `systemctl --user cat` of an instance timer checked in the Pi test (#1).

- [x] Task 08: domain job sources. Feed source (`sources.feeds`, RSS 2.0 and Atom per
  instance in `search.json`); posting without a location field matches a location word in
  title or text; `claude.find_sources` (WebSearch, WebFetch) suggests feeds that the code
  checks before offering; `/quellen` lists, adds and removes feeds and also runs after the
  setup is confirmed; triage rates title-only postings. On the Pi `family1` took 3 greenjobs
  feeds, and a poll sent 2 cards from them. Follow-ups: #7, #8.

## Broken right now

- `README.md` still documents `setup-chat` and the single-instance layout (#2).

## Environment

- **Laptop**: my old radar is stopped (timer and listener inactive). Its config
  `~/.config/job-radar/config.json` holds my search settings and the vault profile paths;
  `~/.config/job-radar/env` holds the bot token. Source for #4.
- **Pi** (`ssh pi`, user `marvin`): git, GNU time and Claude Code installed, Claude logged in
  with my account. `~/job-radar` tracks `origin` = GitHub through a read-only deploy key
  (`~/.ssh/job_radar_deploy`, used for `github.com` via `~/.ssh/config`; on GitHub as
  "pi (read-only)"). Update with `git pull`; the Pi cannot push. Units installed, linger on;
  listener and the poll timers of `test` and `family1` run. Logs: `journalctl --user-unit
  job-radar-listen` / `job-radar-poll@<name>`. Over SSH, `PATH` needs `$HOME/.local/bin` for
  `claude`. Locale warnings over SSH are harmless (`export LC_ALL=C.UTF-8`).

## Open tasks

See the [open issues](https://github.com/marvinmai/job-radar/issues?q=is%3Aopen+label%3Atask):
#1 Pi end-to-end test, #4 move my radar to the Pi, #2 README, #5 final check,
#3 cancel an interview, #6 `/feedback`.
