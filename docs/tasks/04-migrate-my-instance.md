# 04 – Move my own radar to the Pi

Status: open · Needs: 03 · Where: laptop + `ssh pi`

## Goal

My radar runs on the Pi as instance `marvin` with my existing profile and search settings.

## Steps

1. On the Pi: `python3 -m job_radar invite marvin`. Keep the link; don't open it yet.
2. Profile: concatenate the files listed in `profile_files` of the laptop's
   `~/.config/job-radar/config.json` (vault notes; read them first, they are personal and stay
   off the repo) into `instances/marvin/profile.md` on the Pi.
3. Search: from the laptop's `config.json`, put the whitelisted keys (see `plan.md`) into
   `instances/marvin/search.json` and validate with `config.validate_search`. Put the rest
   that still applies (e.g. `filter.nearby_skips_include`, `triage.threshold`,
   `sources.ba.published_since_days`) into `instances/marvin/config.json`. Drop the vault
   paths (`report.reports_dir`, `tracker_path`): reports now land in the instance's data dir
   and are sent to Telegram anyway.
4. Report language: my profile has no "Report language" line, so reports default to English.
   Ask me whether I want German.
5. Run `python3 -m job_radar --instance marvin poll --dry-run` (prints, sends nothing) and
   check that the cards look like my old radar's.
6. Now open the invite link from my Telegram account. Because `profile.md` already exists,
   `/start` shows the help instead of starting the interview.
7. `systemctl --user enable --now job-radar-poll@marvin.timer`.
8. Laptop: leave the old units disabled (`sh disable.sh --uninstall` there if I agree).

## Done when

Cards arrive on the Pi from my instance, and the old laptop radar is off.
