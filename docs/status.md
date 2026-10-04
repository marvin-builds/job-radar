# Status (2026-10-04)

One short branch per task, merged into `main` when the task is done (01: `generalize-onboarding`,
02: `systemd-instances`). Remote: private repo `git@github.com:marvinmai/job-radar.git`.
`python3 -m unittest`: 143 tests pass.

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
  `systemctl --user cat` of an instance timer moves to task 03 (needs installed units).

## Broken right now

- `README.md` still documents `setup-chat` and the single-instance layout. Task 05.

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
| ~~01~~ | ~~[CLI for instances](tasks/01-cli-instances.md)~~ done | – |
| ~~02~~ | ~~[systemd units and install scripts](tasks/02-systemd-and-install.md)~~ done | – |
| 03 | [Deploy on the Pi, end-to-end test](tasks/03-pi-deploy-and-e2e.md) | 01, 02 |
| 04 | [Move my own radar to the Pi](tasks/04-migrate-my-instance.md) | 03 |
| 05 | [README: operator guide](tasks/05-readme-operator-guide.md) | 02 (can run parallel to 03) |
| 06 | [Final check](tasks/06-final-check.md) | 01–05 merged |
| 07 | [Cancel an interview](tasks/07-interview-cancel.md) | – |
| 08 | [Domain-specific job sources](tasks/08-domain-sources.md) | – |
