# job-radar

Job-search radar for one person per Linux user. Polls job portals on a schedule, filters by keywords,
lets headless Claude Code score the rest against that person's profile, and sends the good
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
   Put the person's profile into `profile.md` and their search settings (keywords, areas,
   include/exclude words, locations) into `search.json`, both next to `config.json`.
   `config.json` holds operator settings (models, limits, paths) and overrides both.
4. `python3 -m job_radar doctor`, `python3 -m job_radar test-notify`,
   `python3 -m job_radar poll --dry-run`.
5. `sh install.sh --enable`. Optional: `loginctl enable-linger $USER` to run while logged out.

## Configuration files

All in `~/.config/job-radar/`, merged in this order (later wins):

| File | Holds | Written by |
| --- | --- | --- |
| built-in defaults | generic values, no search terms | `job_radar/config.py` |
| `search.json` | `sources.ba` keywords, areas, extra_params; `filter` include, exclude_title, locations, contract_types | the person's settings; any other key makes the file ignored |
| `config.json` | everything else (models, limits, paths), and overrides | the operator |
| `profile.md` | free-text profile for triage and reports (default for `profile_files`) | the person's settings |

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
`/quellen` searches the web for job boards of the person's field and offers the ones with a
working feed (it also runs once after `/setup`); `/feedback <text>` (or `/feedback`, then the
text) opens a GitHub issue labelled `feedback`; `/usage` shows AI usage; `/help` lists everything. Commands appear in the chat's `/` menu.

## Sources

| Source | Notes |
| --- | --- |
| `ba` | Bundesagentur für Arbeit Jobsuche API v6 (public app key). One query per keyword x area (an area may set its own `keywords`, e.g. broad ones for the home city); `{}` = nationwide, kept only if remote or the city matches `filter.locations`. `extra_params` passes API filters, e.g. `{"zeitarbeit": "false", "pav": "false"}` drops temp agencies and private recruiters. |
| `arbeitnow` | Free Arbeitnow job board API, DE/EU, many English and remote roles. Newest first; `max_pages` x 325 covers about 6 days, so hourly polls see everything new. Pages are fetched `page_delay_s` apart (the API rate-limits). |
| `feeds` | RSS or Atom feeds of field job boards, `{"name", "url"}` each, added per person with `/quellen` (saved in `search.json`). Entries usually have no location field; the filter then looks for `filter.locations` in title and text. |

`filter.nearby_skips_include` lets every local posting (radius search or a listed city) through to triage, because local titles are often generic ("Informatiker/in").

A new source is a module in `job_radar/sources/` with `parse(payload)` and
`fetch(cfg, http_get) -> list[Posting]`, registered in `sources/__init__.py`,
tested against a recorded fixture. Portals whose terms forbid scraping
(LinkedIn, StepStone, XING, Indeed) stay out; their email alerts are the way in.

## Safety

Job postings and web pages are untrusted input. Triage runs `claude -p --restricted --tools ""`;
the report gets only `WebSearch,WebFetch`, and the script writes the file itself.
Neither call can run commands or edit files. Secrets live only in `~/.config/job-radar/env`.

`/feedback` needs `"feedback": {"repo": "owner/name"}` in `config.json` and `GITHUB_TOKEN` in the
shared `env`: a fine-grained token for that one repo with only "Issues: read and write". The
issue names the instance, never the person's Telegram name or chat id. Without both, the bot
says feedback is not set up and the text goes to the log.

## Telling people what is new

When the listener starts (after `git pull` and a restart), each person gets one message with the
`CHANGES.md` sections they have not seen yet; someone who joins later starts at the current
section. When a merge changes something people notice, add a section on top: `## <date>`
(a second one that day: `## <date>b`), then `- ` bullets in German with "du", written for the
people using the bot. Internal changes get no section, and then nobody gets a message.

## Turning it off

`sh disable.sh` stops and disables the timer and the listener; `sh install.sh --enable`
turns them back on. `sh disable.sh --uninstall` also removes the unit files. Config,
secrets and the database are always kept. To pause only the hourly polls but keep the
bot answering: `systemctl --user stop job-radar-poll.timer`.

## Logs

`journalctl --user -u job-radar-poll`, `journalctl --user -u job-radar-listen -f`,
`systemctl --user list-timers`.

## Tests

`python3 -m unittest`
