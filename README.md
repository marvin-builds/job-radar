# job-radar

A Telegram job-search bot for a few people (e.g. a family), run on one small Linux machine
such as a Raspberry Pi. One shared bot serves several **instances**, one per person: each has
its own profile, search settings, poll times and database. A person joins with a one-time
**invite link**, and the bot then sets up their search in a short German interview.

Each instance polls job portals and boards the person picked, filters by keywords, lets
headless Claude Code rate the rest against the person's profile and sends the good ones as
cards. A button on a card starts a deep report on the job and the employer.

```
systemd timer per instance (hourly 07-22, polls only at the person's times; default 9, 13, 18)
  └─ poll: sources ─► SQLite dedupe ─► keyword filter ─► claude -p (haiku, no tools) ─► cards (max 5 per run, rest queued)
systemd service (one for all instances)
  └─ listen: Telegram long polling, one worker per chat
       interview (/setup) ─► claude -p (sonnet, no tools) ─► profile.md + search.json
       🔎 report ─► research: claude -p (WebSearch, WebFetch; no profile) ─► report: claude -p (no tools) ─► markdown file
       /quellen ─► claude -p (WebSearch, WebFetch; search terms and region only) ─► boards offered
```

Python 3 stdlib only. Linux with systemd user units; Claude Code logged in. Every Claude call
on the machine takes one shared lock, so they run one at a time: on a Raspberry Pi 3B+ one
call needs up to ~270 MB.

## Setup on a Raspberry Pi

Tested on a Pi 3B+ with 64-bit Raspberry Pi OS / Debian 13 (Python 3.13).

1. Packages: `sudo apt install git python3`.
2. Claude Code with the native installer (`curl -fsSL https://claude.ai/install.sh | bash`,
   see the Claude Code docs), then run `claude` once and log in. `~/.local/bin` must be on
   `PATH` for your shell; the units set it themselves.
3. The bot, once: talk to `@BotFather`, `/newbot`, copy the token.
4. `git clone <this repo> ~/job-radar && cd ~/job-radar && sh install.sh`. This installs the
   units and creates `~/.config/job-radar/env` (mode 600).
5. Put `TELEGRAM_BOT_TOKEN=<token>` into `~/.config/job-radar/env`.
6. Create the first person: `python3 -m job_radar invite <name>` prints a one-time link. Use a
   neutral name (`family1`, …), not the person's real name: it shows up in logs and in
   feedback issues.
7. `python3 -m job_radar doctor` checks the shared setup; `sh install.sh --enable` starts the
   listener and the poll timers; `loginctl enable-linger $USER` keeps them running while
   nobody is logged in.
8. Optional:
   - **Admin:** make your own instance the admin, which allows invites from the chat (see
     below). Put `{"admin": "<your instance>"}` into `~/.config/job-radar/operator.json`.
   - **Feedback:** to let `/feedback` open GitHub issues, put
     `"feedback": {"repo": "owner/name"}` into the instance's `config.json` and `GITHUB_TOKEN`
     into the shared `env`. Use a fine-grained token for that one repo with only "Issues:
     read and write".

Then harden the machine as described under [Safety](#safety).

## People

**Adding a person.**
- **From the chat:** the admin sends `/einladen`. The bot creates the next free instance
  (`family2`, …) and replies with its link.
- **On the machine:** run `python3 -m job_radar invite <name>`.
- **After that:** send the link to the person. It works once and expires after 14 days. When
  they open it, their chat is bound to the instance, the listener starts their poll timer,
  and the interview begins. `/einladungen` (admin) lists links nobody has used yet.

A chat that is already bound and opens a new invite moves to the new instance; the old
instance keeps its data but gets no messages.

**Changing someone's search.**
- **In the chat:** the person sends `/setup` (interview again), `/quellen` (job boards) or
  `/zeiten` (poll times).
- **On the machine:** edit the instance's files in `~/.config/job-radar/instances/<name>/`.
  They merge in this order, later wins:

| File | Holds | Written by |
| --- | --- | --- |
| built-in defaults | generic values, no search terms (`job_radar/config.py`) | the code |
| `search.json` | `sources.ba` keywords, areas, extra_params; `sources.feeds.feeds` and `sources.pages.pages` (`{name, url}`, http(s) only); `filter` include, exclude_title, locations, contract_types; `schedule` (`weekdays`/`weekend` hours, 7-22) | the person's interview, `/quellen`, `/zeiten`; checked against a whitelist, any other key makes the file ignored |
| `config.json` | everything else (models, limits, paths) and overrides; starts as a copy of `config.example.json` | the operator |
| `profile.md` | the profile that triage and reports rate against | the interview |
| `env` | `TELEGRAM_CHAT_ID` of the bound chat (mode 600) | the invite |

**Removing a person.** `systemctl --user disable --now job-radar-poll@<name>.timer`, then
delete `~/.config/job-radar/instances/<name>` and `~/.local/share/job-radar/instances/<name>`.

## In the chat

| Command or button | What happens |
| --- | --- |
| 🔎 Recherche / 👎 Überspringen / ✅ Beworben | on each card: deep report (file plus summary after a few minutes), hide, mark as applied |
| a job link | deep report on a posting the person found |
| `/setup` | the interview (again); `/abbrechen` stops it, an existing profile stays |
| `/profile` | the profile, formatted |
| `/suchen` | poll now (at most every 10 minutes) |
| `/zeiten` | when to poll: presets (3× a day, 1×, workdays only, hourly) or `/zeiten 8 12 17` |
| `/quellen` | lists the person's boards (tap to remove) and searches the web for more of their field: feeds, or plain list pages that a cheap model reads (marked "ohne Feed") |
| `/usage` | AI usage today, 7 and 30 days, and the plan limits of the Claude account |
| `/feedback` | sends a GitHub issue labelled `feedback` that names only the instance |
| `/help` | all of this, in German |
| `/einladen`, `/einladungen` | admin only, in the admin's menu only |

Only one person can be in the interview at a time; anyone else is asked to try again in a
few minutes. A silent interview gives up its turn after 30 minutes.

**Limits per person and day:** 15 reports, 3 board searches and 100 interview turns, so that
one chat cannot use up the Claude account.

## Commands

All take `--instance <name>` (or `JOB_RADAR_INSTANCE`) where they act on one person.

| Command | What it does |
| --- | --- |
| `invite <name>` | create an instance and print its one-time link |
| `doctor` | without `--instance`: token, `claude`, instances, open invites; with it: that instance's config, profile, database |
| `poll [--dry-run]` | one run for the instance; outside its poll times it ends at once (dry run prints, stores nothing, still calls Claude for triage) |
| `listen` | the bot for all instances (the systemd service) |
| `report <id>` | deep report for a stored posting id |
| `usage` | AI usage of the instance |
| `test-notify` | send a test message to the instance's chat |
| `busy` | what a listener restart would cut off now; exit 1 if `deploy.sh` should wait |
| `check-news <base> <head>` | merges that change what people use without news (see below) |

## Sources

| Source | Notes |
| --- | --- |
| `ba` | Bundesagentur für Arbeit Jobsuche API v6 (public app key). One query per keyword x area (an area may set its own `keywords`, e.g. broad ones for the home city); `{}` = nationwide, kept only if remote or the city matches `filter.locations`. `extra_params` passes API filters, e.g. `{"zeitarbeit": "false", "pav": "false"}` drops temp agencies and private recruiters. |
| `arbeitnow` | Free Arbeitnow job board API, DE/EU, many English and remote roles. Newest first; `max_pages` x 325 covers about 6 days. Pages are fetched `page_delay_s` apart (the API rate-limits). |
| `feeds` | RSS or Atom feeds of field job boards, added per person with `/quellen`. Entries usually have no location field; the filter then looks for `filter.locations` in title and text. |
| `pages` | Boards without a feed: the list page is reduced to text with numbered links and the triage model (Haiku) names the postings by link number. It runs again only when the page's links changed (`pages-cache.json` next to the database); about $0.05 list price and 40 s on a Pi for a page of 50 postings. |

`filter.nearby_skips_include` lets every local posting (radius search or a listed city)
through to triage, because local titles are often generic ("Informatiker/in").

A new source is a module in `job_radar/sources/` with `parse(payload)` and
`fetch(cfg, http_get) -> list[Posting]`, registered in `sources/__init__.py`, tested against
a recorded fixture. Portals whose terms forbid scraping (LinkedIn, StepStone, XING, Indeed)
stay out; their email alerts are the way in.

## Safety

The machine runs Claude Code with your account behind a bot that several people use and
that reads untrusted text (postings, web pages, chat messages). Defences in the code:

- **Who can talk to the bot:** an unknown chat only gets "you need an invite link". Invites
  are one-time, 96 random bits, valid 14 days, and each chat reaches only its own instance.
- **No shell or file tools for Claude:**
  - triage, interview and page reading run `claude -p --restricted --tools ""`
  - the report research and the board search get only `WebSearch,WebFetch`
  - every call runs in an empty directory
  - the script writes all files itself, under names Claude cannot choose
- **No profile next to the web:** the report research never sees the profile, so a page
  cannot make it send the profile anywhere. The report itself is then written without
  tools. The board search gets only search terms and region.
- **Outputs:** everything Claude returns is HTML-escaped before it reaches Telegram; card
  links are http(s) only; board pages can only name links that are on the page.
- **Limits:** chat messages to the interview are capped at 2000 characters; the per-day
  limits above apply.
- **Sandboxed services:** both services run sandboxed (`systemd/*.service`):
  - private umask; a read-only system and home except the bot's config and data, Claude's
    login and the user units
  - no access to `~/.ssh`, `~/.gnupg` or the shell history
  - no new privileges, namespaces or capabilities

  `systemd-analyze --user security job-radar-listen.service` rates it 2.2 ("OK").
- **Secrets:** they live only in `env` files (mode 600). Logs carry no message text, chat
  ids or tokens; the one exception is the text of a `/feedback` that could not be sent.

On the machine, once:

- **Managed settings:** `/etc/claude-code/managed-settings.json` applies to every Claude
  Code run on the machine, even with `--restricted`, as a second layer:

  ```json
  {
    "permissions": {
      "deny": ["Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep",
               "WebFetch(domain:localhost)", "WebFetch(domain:127.0.0.1)", "WebFetch(domain:<router IP>)"],
      "disableBypassPermissionsMode": "disable"
    },
    "disableAllHooks": true,
    "enableAllProjectMcpServers": false
  }
  ```

- **SSH:** keys only and no root login, e.g. `/etc/ssh/sshd_config.d/10-hardening.conf` with
  `PasswordAuthentication no`, `KbdInteractiveAuthentication no` and `PermitRootLogin no`.
  Test a fresh login before closing your session.
- **Updates:** `sudo apt install unattended-upgrades` for Debian security updates.
- **Claude Code:** the bot's calls never update it (`DISABLE_AUTOUPDATER=1`). Update it on
  purpose with `claude update` after reading the release notes, then check that `doctor` and
  one `poll --dry-run` still work.

## Deploying an update

On the machine that runs the bot: `sh deploy.sh`.
1. It pulls `main`, installs the units and restarts the poll timers.
2. It waits while a restart would cut someone off (`python3 -m job_radar busy`): an
   interview turn, a `/quellen` search, a `/suchen` or the first search after setup, or a
   chat used in the last minute. It waits at most 10 minutes, and `--force` skips the wait.
   Running reports are only listed; they resume after the restart.
3. It restarts the listener. On stop, the listener takes no new updates and lets the queued
   ones finish (up to 60 s).

Before pulling it checks every commit on `main`'s main line since the running version
(`python3 -m job_radar check-news HEAD origin/main`). A commit that changes `job_radar/`,
`prompts/` or `systemd/` must bring a `CHANGES.md` section, or carry a line `No-news:
<reason>` in its merge commit message for changes nobody using the bot will notice. Decide
that when merging:

```
git merge --no-ff <branch> -m "Merge branch '<branch>'" -m "No-news: internal refactor"
```

`--skip-news-check` is only for a merge that was already pushed without its line.

## Telling people what is new

When the listener starts (after `sh deploy.sh`), each person gets one message with the
`CHANGES.md` sections they have not seen yet; someone who joins later starts at the current
section. When a merge changes something people notice, add a section on top: `## <date>`
(a second one that day: `## <date>b`), then `- ` bullets in German with "du", written for the
people using the bot. Internal changes get no section, and then nobody gets a message.

## Turning it off

`sh disable.sh` stops and disables the listener and every poll timer; `sh install.sh
--enable` turns them back on. `sh disable.sh --uninstall` also removes the unit files.
Config, secrets and databases are always kept. To pause one person's polls but keep the bot
answering: `systemctl --user stop job-radar-poll@<name>.timer`.

## Logs

- `journalctl --user-unit job-radar-listen -f`: the bot. Each update is logged with
  instance, command or button, age and handling time; Telegram errors carry Telegram's
  reason.
- `journalctl --user-unit job-radar-poll@<name>`: the polls. Each logs `fetched`, `new`,
  `matched`, `notified`, `triage_calls` and `scored`.
- `systemctl --user list-timers`.

Use `--user-unit`; `journalctl --user -u` finds nothing for these units on the Pi. Over SSH,
`export LC_ALL=C.UTF-8` silences locale warnings, and `~/.local/bin` must be on `PATH` for
`claude`.

## Tests

`python3 -m unittest`
