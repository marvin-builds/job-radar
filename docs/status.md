# Status (2026-10-05)

One short branch per task, merged into `main` when the task is done. Open tasks are
[GitHub issues](https://github.com/marvinmai/job-radar/issues?q=is%3Aopen+label%3Atask); the
task files of 01–08 are in the git history (`docs/tasks/`, removed in the move to issues).
`python3 -m unittest`: 362 tests pass, including `tests/test_integration.py` (two people at once, real threads).

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
- [x] #6 `/feedback` opens a GitHub issue (label `feedback`, names only the instance). Needs
  `feedback.repo` in the instance config and `GITHUB_TOKEN` in the shared env.
- [x] #9 after a deploy the listener sends each person the unseen `CHANGES.md` sections.
- [x] Weekday polls start at 09:00 (was 07:00). Deploys go through `deploy.sh`: it refuses
  each merge that changes code people use without a `CHANGES.md` section or a `No-news:
  <reason>` line (`job_radar check-news`), waits while an interview turn runs (`job_radar busy`) and restarts the listener, which sends the news.

- [x] 2026-10-05, worked through alone:
  - **Invites and chat:** #12 invite from a bound chat moves it; #3 `/abbrechen`; #14 readable profile and report summary; #13 `/suchen`; #16 one interview at a time; #10 `/einladen` (admin in `operator.json`, the listener starts the timer).
  - **Poll times and sources:** #11 `/zeiten`, default 3× a day (9/13/18); #7 boards without a feed (`sources.pages`).
  - **Logging and deploys:** #18 update and Telegram error logging; #19 `busy` waits for searches and taps, the listener drains on stop.
  - **Network:** #21 a lost connection attempt no longer stalls the bot for 60 s.
  - **Security:** #15 audit. Split report, empty cwd, daily limits, sandboxed units (2.2 OK), managed settings, SSH keys only, unattended-upgrades, file modes.
  - **Docs:** #2 README as an operator guide.
  - **Family feedback:** #22 `/quellen` lists before searching; #23 SQLite shared across threads (lock); #24 integration test; #25 `/quellen` names all sources; #26 bookmarks (`/merkliste`), follow-up questions, ratings learn from marks.
  - **Sources:** #8 BA postings are rated on their full text.

## Broken right now

Nothing known.

## Environment

The bot runs on a Raspberry Pi 3B+ with two instances (the admin's and `family1`), sandboxed
units, linger on and timers hourly 07-22. Machine details (user, deploy key, paths) are kept
out of the repo. Update with `sh deploy.sh` (see README).

## Open tasks

See the [open issues](https://github.com/marvinmai/job-radar/issues?q=is%3Aopen):
- #1 Pi end-to-end test: tapping the card buttons is left
- #4 move the admin's radar to the Pi: close when the first cards have arrived
- #17 measure poll usage: a week of numbers, check about 2026-10-12
- #5 final check, after a few days of running
