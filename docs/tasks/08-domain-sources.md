# 08 – Domain-specific job sources

Status: in progress (code and tests done, Pi check open) · Needs: – · Files: `job_radar/sources/feeds.py`, `job_radar/app.py`,
`job_radar/claude.py`, `job_radar/config.py`, `job_radar/filter.py`, `prompts/sources.md` ·
Branch: `domain-sources`

## Goal

Each person's radar also reads job boards of their own field, which the Bundesagentur and
Arbeitnow barely cover. Claude finds them, the person decides which ones to add.

## Found in task 03

The first real instance (part-time work alongside studies in ecology and botany near
Stuttgart) got no card. The triage was right; the supply was the problem. The BA API
returned, for 28 days within 30 km of Stuttgart: Kartierung 0, Botanik 0, Naturschutz 4,
Ökologie 5, Biologie 11. Arbeitnow is mostly IT and English, so 650 postings gave nothing.
Field boards such as greenjobs.de list such jobs and offer an Atom feed (132 entries a
week, location in the title: "… (Köln) - Landwirtschaftskammer NRW").

## Scope

1. **Feed source** `sources.feeds`: RSS 2.0 and Atom feeds listed per instance in
   `search.json` as `{"name", "url"}`. One broken feed does not stop the others.
2. **Filter**: a posting without a location field (feeds) matches when a location word
   appears in its title or text.
3. **Finding sources**: `claude.find_sources` searches the web (WebSearch, WebFetch) for
   job boards that fit the profile and region and returns feed URLs. The code keeps only
   feeds that load and parse, and drops ones already configured.
4. **`/quellen`**: lists the person's feeds (remove button each) and searches for new ones;
   each suggestion comes with "Aufnehmen" / "Nein" buttons. It also runs on its own after
   the setup is confirmed, after the first poll.
5. A new `/setup` keeps the feeds the person added.

## Later (not in this task)

- Boards without a feed: let a cheap model extract postings from the list page.
- Fetch the BA posting text, so triage sees working hours (now title and occupation only).

## Done when

- Tests first for each part; `python3 -m unittest` passes.
- On the Pi, `/quellen` for a real instance suggests at least one working feed, and a poll
  reads it.
