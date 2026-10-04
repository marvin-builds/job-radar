# 02 – systemd units and install scripts

Status: done · Needs: 01 · Files: `systemd/`, `install.sh`, `disable.sh` · Branch: `systemd-instances`

## Goal

One listener for the shared bot, one poll timer per instance.

## Scope

- `systemd/job-radar-listen.service`: `ExecStart=/usr/bin/python3 -m job_radar listen` (keep
  `@REPO@`, PATH incl. `%h/.local/bin` for `claude`).
- Replace `job-radar-poll.service/.timer` with templates `job-radar-poll@.service` (runs
  `python3 -m job_radar --instance %i poll`, `TimeoutStartSec` large enough to wait for the
  Claude lock, e.g. 60 min) and `job-radar-poll@.timer` (same schedule as now,
  `RandomizedDelaySec=10min` so instances spread out, `Persistent=true`).
- `install.sh`: install units; create `~/.config/job-radar/env` (mode 600, `TELEGRAM_BOT_TOKEN=`)
  if missing; no per-instance config anymore (`invite` does that). `--enable`: enable the
  listener and the timer of every existing instance (`ls ~/.config/job-radar/instances`).
  Remove the old unit files `job-radar-poll.service/.timer` if present.
- `disable.sh [--uninstall]`: stop the listener and all `job-radar-poll@*.timer`; `--uninstall`
  removes the unit files. Never touch config or data.
- Check shell scripts with `sh -n` and, if available, `shellcheck`.

## Done when

On the laptop with a throwaway `XDG_CONFIG_HOME`: `sh install.sh` installs the units,
`systemctl --user cat job-radar-poll@test.timer` resolves, `systemd-analyze --user verify`
on the unit files shows no errors. Don't enable anything on the laptop.
