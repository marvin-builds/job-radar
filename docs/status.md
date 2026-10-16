# Status (2026-10-07)

One short branch per task, merged into `main` when the task is done. Open tasks are
[GitHub issues](https://github.com/marvin-builds/job-radar/issues?q=is%3Aopen+label%3Atask); the
task files of 01–08 are in the git history (`docs/tasks/`, removed in the move to issues).
`python3 -m unittest`: 681 tests pass, including `tests/test_integration.py` (two people at once, real threads).

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
- [x] 2026-10-06, family feedback worked through alone:
  - #30 every report has ⭐ Vormerken; `/recherche` asks for a link to a posting found elsewhere.
  - #29 after `/setup` the board search runs first; offered boards end with 🔎 Jetzt suchen (`q:go`, shares the `/suchen` cooldown), else the first poll runs at once.
  - #31 the poll also finds employers whose careers page asks for unsolicited applications (source `initiativ`, web search at most weekly, no keyword filter, triaged, never offered twice).
  - #28 triage rates a posting of the wrong kind of position (regular job for an internship seeker) at most 3; checked on family1's shown postings with Haiku.
  - #27 `/zeitraum` sets `sources.ba.published_since_days` (1-100) in `search.json`; default 28 now in `DEFAULTS`, removed from `config.example.json` and from the instances' `config.json` on the Pi. Employers taking unsolicited applications: #31.
- [x] 2026-10-07, family feedback worked through alone:
  - #33 `/merkliste` sends each bookmark as its own message (`e:a`/`e:x`, edited after a tap; old `m:` buttons still work); cards and entries say how long a posting is online. Telegram calls wait on 429.
  - #34 a posting sent as a link takes its publication date from the report (`published:` in the frontmatter) or from a source that found the same URL.
  - #32 Initiativbewerbungen switch in `/quellen` (`sources.initiativ.enabled` in `search.json`, `config.json` wins). Cards show the employer's website (triage names it, checked at send time over `http.get_public_text`) and link the kununu and Glassdoor search: both sites block bots (AWS WAF captcha, 403), also for Claude's WebFetch, so scores cannot be read.
- [x] 2026-10-07, cost plan phase 0 (measure, extends #17): `usage` records failed and timed-out calls
  (`ok`, `error`), cache writes and reads apart, postings per call, poll id and trigger; a `polls` row per
  poll (fetched to notified, omitted, failed batches, board page cache hits); `/quellen` board checks count
  as `pages_check`. `usage-report` and `replay-filter [--dedup]` read a database read-only, no AI.
- [x] 2026-10-07, cost plan phase 1 (stop paying twice): triage rates the newest pending postings first,
  halves a failed batch down to the posting that breaks it (both first halves failing: an outage, waits),
  gives up on a posting after 3 calls that left it out or failed on it alone (`unscored`; a failed call with
  others in it, or an outage, counts none) and expires pending postings older than the search (`expired`). An
  indexed `fingerprint` (title, employer, city; none without employer or city) gives a copy rated in the last
  60 days, after the last profile change, that rating as `duplicate`, never sent, only when that rating was
  good enough to send. `r:` and a link sent again resend the report file; `resume_reports` resumes each report
  once within a day, counted toward the limit. On the dev database `replay-filter --dedup` saves 1 of 272
  ratings (0.4 %; 5 if copies also took a low rating).
- [x] 2026-10-07, cost plan phase 3 (cheaper spawns): triage, board-page extraction and the limit probe send
  their fixed instructions as `--system-prompt` instead of Claude Code's default (still the subscription
  login on the Pi; probe 3360 -> 363 input tokens) and skip the 1-hour prompt cache, which they never read;
  web-tool calls keep both. A batch of 10 costs $0.0100 instead of $0.0235; on 40 dev postings old and new
  agree as closely as the old setup with itself (85 % vs 82 % within ±1). `triage.batch_size` is
  configurable; it stays at 10, since 20 moved scores beyond that noise (78-80 %). `--json-schema` works with
  Haiku but takes 3 turns and costs more, so replies are still parsed.
- [x] 2026-10-07, cost plan phase 2 (BA fields): BA internships and apprenticeships get the contract types
  `internship` and `apprenticeship` (German on the card; a `contract_types` list naming neither lets both
  through, as before); the details add start, main occupation and a `Zeitarbeit` tag and line; the BA
  partner's site becomes the website when its host names the employer. `exclude_title` words ending in `*`
  match inside words; `nearby_skips_include` needs the radius search or the location field, not a city in a
  feed's title (family1: 19 fewer postings, all rated 0-2). `extra_params` take only the API's codes
  (`angebotsart`, `arbeitszeit`, `befristung`, `zeitarbeit`, `pav`), and the interview knows `angebotsart`
  and `*`. Every filter change loses 0 good or bookmarked postings in `replay-filter` on the dev and both
  live databases. No `exclude_staffing`: the search results carry no staffing flag, and `zeitarbeit=false`
  drops the same postings before any detail request.
- [x] 2026-10-07, cost plan phase 5 (deep report): both report calls get the posting's date and tags
  (`describe(..., known=True)`) and its text cut at 6,000 characters; only the report call without tools also
  gets triage's employer line and website (`triaged=True`), which triage wrote after reading the profile. The
  research fetches at most 6 pages, opens the posting only when its text is missing, cut or under 1,000
  characters (`[only a summary]`) or its date is missing, and no longer fetches kununu or Glassdoor (they block
  bots; the card links stay). A link the person sends is read once in code: its JSON-LD `JobPosting`
  (`jsonld.py`, only a page with exactly one) gives title, employer, place, date (#34) and text before the
  research starts.

- [x] 2026-10-07, cost plan phase 4 (board pages in code): the reduced page drops closed `nav` blocks and the
  page's own `header`/`footer` (only when unique outside content, before/after every other link, closed, and
  never when nothing would be left). The model's first read keeps the postings' address shapes (host and
  folder, digits open; none when more than 3 or more than half the postings, so any link change counts); later
  polls send it only the lines of new links of those shapes (up to two lines before and after), drop gone ones
  without a call and read the whole page weekly. JSON-LD postings (`JobPosting` or an `ItemList`, at least two,
  half linked, hardly more same-shaped links) stand in between those reads once the model found nearly
  nothing they lack (cache flag `jsonld`) and no new shaped link is missing from them. A feed the page names
  (RSS/Atom autodiscovery) that lists nearly all the model's postings replaces the page in the poll (the
  model reads the page weekly and the feed is checked again against its postings; postings keep source `page` and
  their link as id) and in `/quellen`, where `check_page` reads like a poll and fills the cache. One `pages-cache.json` in the shared data directory for all instances (`pages.CACHE_PATH`, merged
  under a lock, atomic replace, written only on a new read, boards unread for 30 days dropped). `page_hits`
  now counts every board read without the model.
- [x] 2026-10-07, cost plan phase 6 (interview buttons): a first `/setup` asks seven closed questions with
  buttons before the model (`interview.QUESTIONS`: kind of position, hours, commute, remote, fixed-term, temp
  agencies, report language; callback `b:<key>:<code>`, answers in state `interview_answers`). The model gets
  them as facts and asks only open questions; code writes their settings into the proposal
  (`interview.apply`: `radius_km` of every town area, the nationwide `{}` area, `angebotsart`, `befristung`
  2;3, `zeitarbeit`), except for answers the model lists in `changed` because the person changed them in the
  chat. A re-setup goes straight to the model. Thinking stays on: with it off, a finishing turn twice gave no
  clean JSON.
- [x] 2026-10-07, cost plan phase 7 (employers in code): each poll reads `/karriere`, `/jobs` and
  `/stellenangebote` on the sites of employers whose postings triage rated at least 4 (`careers.py`, at most
  20 requests 1 s apart through `get_public_text`); a page that carries the employer's name and invites
  the application itself (Initiativbewerbung, not "Eigeninitiative") becomes an Initiativbewerbung card.
  State `initiativ_sites` keeps each host's check (rechecked after 90 days if nothing was found, 7 if the
  site was down; a bad URL or failing site is logged and never stops the poll). With at least 3 employers
  found this way since the last Sonnet search that ran (`initiativ_ok_at`), that search waits 14 days
  instead of 7. A job feed a careers page names is offered once in `/quellen` when it lists jobs (➕ or Nein).
- [x] 2026-10-07, cost plan phase 8 (Pi and operations): every Claude call writes the account's plan limits to
  `limits.json` next to `claude.lock` (temp file, then rename), and `/usage` takes the newest of the instance's
  own and the shared ones, so it probes only when no instance saw limits in the last 15 minutes. A poll writes
  a triage batch's ratings in one transaction (`Store.set_scores`) and its statuses in one per kind
  (`Store.set_statuses`): about 155 commits for 30 postings before, under 15 now. The databases run in WAL mode
  with `synchronous=NORMAL` (fewer SD card syncs, readers never block the writer); a file another process is
  reading keeps its old journal until the next start. The hourly timer stays: an empty start costs no AI.

## Broken right now

Nothing known.

## Environment

The bot runs on a Raspberry Pi 3B+ with two instances (the admin's and `family1`), sandboxed
units, linger on and timers hourly 07-22. Machine details (user, deploy key, paths) are kept
out of the repo. Update with `sh deploy.sh` (see README).

## Open tasks

See the [open issues](https://github.com/marvin-builds/job-radar/issues?q=is%3Aopen):
- #1 Pi end-to-end test: tapping the card buttons is left
- #4 move the admin's radar to the Pi: close when the first cards have arrived
- #17 measure poll usage: a week of numbers, check about 2026-10-12
- #5 final check, after a few days of running
