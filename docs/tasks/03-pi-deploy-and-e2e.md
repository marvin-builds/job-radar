# 03 – Deploy on the Pi, end-to-end test

Status: open · Needs: 01, 02 · Where: `ssh pi` · Branch: none; fixes on `pi-e2e-fixes`

## Goal

The family setup runs on the Pi and a full interview works through the real bot.

## Steps

1. The Pi runs `main`: `cd ~/job-radar && git switch main && git pull` (read-only deploy
   key, see `status.md`; the first time, `git switch -c main --track origin/main` after
   `git fetch`). Run `python3 -m unittest` there.
2. Delete the old-layout test persona: `rm ~/.config/job-radar/{config.json,profile.md,search.json}`
   and `~/.local/share/job-radar/radar.db` if present. Check what is there before deleting.
3. Bot token: copy `TELEGRAM_BOT_TOKEN` from the laptop's `~/.config/job-radar/env` into the
   Pi's `~/.config/job-radar/env` (mode 600). Keep the laptop radar stopped: only one program
   may read a bot's updates.
4. `sh install.sh`, `python3 -m job_radar invite test`, `sh install.sh --enable`,
   `loginctl enable-linger marvin` (needs sudo; ask before).
5. Ask me to open the invite link and do the interview as a persona. Watch
   `journalctl --user -u job-radar-listen -f`. Check: German with "du", summary + buttons,
   files written to `instances/test/`, match count message, `/profile`, `/help`.
6. Trigger a poll: `systemctl --user start job-radar-poll@test.service`, check that cards
   arrive and buttons work (Recherche, Überspringen, Beworben).
7. Check the lock: start two polls (or a poll plus a report) at once, verify in the journal
   that the Claude calls run one after the other; watch RAM with `vmstat`.
8. Remove the test instance afterwards: disable `job-radar-poll@test.timer`, delete
   `instances/test` in config and data (ask before deleting).

## Done when

Steps 1–8 pass; findings (prompt wording, timing) are fixed or written down as new tasks.
