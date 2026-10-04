# Plan

## Goal

Make job-radar usable by people other than me (any field, not only IT) whom I set up myself.
They only use Telegram. First users: my family, sharing one Raspberry Pi 3B+ and my Claude
login. My own radar moves to the same Pi.

## Decisions

- **Generalize in place.** This repo is the generic project. Personal data lives only in
  local config, never in the repo.
- **Germany, Linux/systemd, Claude Code CLI, Telegram stay** as they are.
- **Onboarding is an AI interview inside Telegram** (`/start` or `/setup`), conversation only,
  no CV upload. The interview writes a free-text profile and *derives* the search settings.
  The person confirms a plain-language summary (✅ Passt so / ✏️ Ändern). I can still edit
  the files by hand.
- **German only, always "du".** That covers the interview and all fixed bot texts.
- **One Raspberry Pi, one Linux user, one Claude login (mine)** for the whole household.
  Claude calls of all people take turns through a file lock (~270 MB each on a 1 GB Pi).
- **One shared Telegram bot (the existing one) with invite links.** `invite <name>` creates
  an instance and prints `https://t.me/<bot>?start=<code>`. An unknown chat is ignored unless
  it opens the bot with a valid one-time code.
- **My own instance**: my vault profile files are copied (concatenated) into its `profile.md`,
  and my current search settings into its `search.json` / `config.json`. No interview for me.

## Architecture (target)

```
~/.config/job-radar/
  env                      TELEGRAM_BOT_TOKEN (shared)
  invites.json             one-time codes -> instance name (mode 600)
  instances/<name>/
    config.json            operator settings (models, limits); overrides everything
    search.json            search settings from the interview (whitelisted keys only)
    profile.md             profile for triage and reports
    env                    TELEGRAM_CHAT_ID of this person
~/.local/share/job-radar/
  claude.lock              shared lock: one Claude process at a time
  instances/<name>/radar.db, reports/

systemd --user
  job-radar-listen.service       one listener for the shared bot, routes chats to instances
  job-radar-poll@<name>.timer    per instance, hourly poll (runs `poll --instance <name>`)
```

Settings merge per instance: built-in defaults < `search.json` < `config.json`.
`search.json` may only contain `sources.ba.{keywords, areas, extra_params}` and
`filter.{include, exclude_title, locations, contract_types}`. Any other key makes the
file ignored, so the AI can never set paths, models or tools (`config.validate_search`).

Code map:
- `job_radar/config.py`: layers, whitelist, instance paths, invites
- `job_radar/interview.py` + `prompts/interview.md`: one interview turn = one tool-less `claude -p`
- `job_radar/app.py`: poll, Telegram handling (`handle_update`), interview flow, `Registry`,
  `dispatch`, `listen`
- `job_radar/claude.py`: `claude -p` runner, `LOCK_PATH` (set by the CLI)
- `job_radar/cli.py`: commands (still single-instance, see status)

## Testing

- Test first for logic: config/whitelist, interview parsing and flow, routing, locks, CLI
  argument handling. The repo has fakes for Claude (`FakeRunner`), Telegram (`FakeApi`,
  `FakeBot`) and an on-disk `Store` in a temp dir. Patch `XDG_CONFIG_HOME`/`XDG_DATA_HOME`
  for anything touching instance paths (see `InstanceTest`, `RouterTest`).
- No unit tests for prompt wording, README or shell scripts. Check prompts with a persona run
  (see issue #1, Pi end-to-end test).
- `python3 -m unittest` must pass before every commit.
