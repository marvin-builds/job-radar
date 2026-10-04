# job-radar

Personal job-search radar. Polls job portals on a schedule, filters by keywords,
lets headless Claude Code score the rest against my profile, and sends the good
ones to Telegram. A button on each posting starts a deep job and employer report.

```
systemd timer (hourly Mon-Fri 07-21, every 2 h Sat/Sun 09-19)
  └─ poll: sources ─► SQLite dedupe ─► keyword filter ─► claude -p (haiku, no tools) ─► Telegram cards (max 5 per run, rest queued)
systemd service
  └─ listen: Telegram long polling
       🔎 Deep report ─► claude -p (sonnet, WebSearch + WebFetch only) ─► markdown file ─► Telegram
       👎 Skip / ✅ Applied ─► status (+ tracker row)
       pasted job link ─► same deep report
```

Python 3.12 stdlib only. Linux with systemd user units; Claude Code CLI logged in.

## Setup

1. Telegram bot: talk to `@BotFather`, `/newbot`, copy the token.
2. `sh install.sh` installs the units and creates `~/.config/job-radar/config.json`
   (from `config.example.json`) and `~/.config/job-radar/env` (mode 600).
3. Put `TELEGRAM_BOT_TOKEN=<token>` into `env`, send your bot any message (e.g. `/start`),
   then `python3 -m job_radar setup-chat` saves `TELEGRAM_CHAT_ID` and sends a test message.
   Adjust keywords, home city and paths in `config.json`.
4. `python3 -m job_radar doctor`, `python3 -m job_radar test-notify`,
   `python3 -m job_radar poll --dry-run`.
5. `sh install.sh --enable`. Optional: `loginctl enable-linger $USER` to run while logged out.

## Commands

| Command | What it does |
| --- | --- |
| `poll [--dry-run]` | one fetch/filter/triage/notify run (dry run prints, stores nothing; still calls Claude unless triage is disabled) |
| `listen` | handle Telegram buttons (the systemd service) |
| `report <id>` | deep report for a stored posting id |
| `usage` | AI usage (calls, tokens, list-price cost) for today, 7 and 30 days |
| `setup-chat` | find your private chat with the bot, save `TELEGRAM_CHAT_ID` |
| `test-notify` | send a test message |
| `doctor` | check secrets, config, `claude`, database |

## In the chat

Buttons on each job: 🔎 Deep report, 👎 Skip, ✅ Applied. Paste a job link for a deep report;
`/usage` shows AI usage; `/help` lists everything. Commands appear in the chat's `/` menu.

## Sources

| Source | Notes |
| --- | --- |
| `ba` | Bundesagentur für Arbeit Jobsuche API v6 (public app key). One query per keyword x area (an area may set its own `keywords`, e.g. broad ones for the home city); `{}` = nationwide, kept only if remote or the city matches `filter.locations`. `extra_params` passes API filters, e.g. `{"zeitarbeit": "false", "pav": "false"}` drops temp agencies and private recruiters. |
| `arbeitnow` | Free Arbeitnow job board API, DE/EU, many English and remote roles. Newest first; `max_pages` x 325 covers about 6 days, so hourly polls see everything new. Pages are fetched `page_delay_s` apart (the API rate-limits). |

`filter.nearby_skips_include` lets every local posting (radius search or a listed city) through to triage, because local titles are often generic ("Informatiker/in").

A new source is a module in `job_radar/sources/` with `parse(payload)` and
`fetch(cfg, http_get) -> list[Posting]`, registered in `sources/__init__.py`,
tested against a recorded fixture. Portals whose terms forbid scraping
(LinkedIn, StepStone, XING, Indeed) stay out; their email alerts are the way in.

## Safety

Job postings and web pages are untrusted input. Triage runs `claude -p --restricted --tools ""`;
the report gets only `WebSearch,WebFetch`, and the script writes the file itself.
Neither call can run commands or edit files. Secrets live only in `~/.config/job-radar/env`.

## Logs

`journalctl --user -u job-radar-poll`, `journalctl --user -u job-radar-listen -f`,
`systemctl --user list-timers`.

## Tests

`python3 -m unittest`
