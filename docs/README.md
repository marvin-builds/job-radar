# Project docs: job-radar for the family

job-radar is being turned from my personal radar into a service that several people in my
household use through one shared Telegram bot, all running on a Raspberry Pi.

| File | What it holds |
| --- | --- |
| [plan.md](plan.md) | Goal, decisions taken, target architecture |
| [status.md](status.md) | What is done and verified, what is broken right now, where things stand on the Pi |
| [GitHub issues](https://github.com/marvinmai/job-radar/issues) | Open tasks (label `task`), ideas, and feedback sent from the bot (label `feedback`) |

## Working on a task in a new session

1. Read `plan.md` (decisions are settled; don't reopen them without asking) and `status.md`.
2. Pick the lowest-numbered open issue with the label `task` whose prerequisites ("Needs:")
   are closed: `gh issue list --label task`.
3. Work on a short branch named after the issue, created from an up-to-date `main`. One
   issue per branch, then merge.
4. Write failing tests first for logic (see "Testing" in `plan.md`), commit one concern at a
   time, never push without asking.
5. When the task is done: run `python3 -m unittest`, run `/code-review` on the branch and fix
   what it finds, tick the boxes in the issue, update `status.md`, and note anything the next
   task needs to know there (commit that too). If people using the bot will notice the change
   (new or changed commands, texts, cards, schedule), add a section to `CHANGES.md` (see the
   README); they get it after the next deploy. In doubt, add one. Close the issue after the merge (or with
   `Closes #<n>` in the merge commit).
6. Then ask me whether to merge into `main` (`git merge --no-ff <branch>`) and push. After
   the merge, `main` must be consistent: tests green, nothing half-wired.
   Deploy with `ssh pi 'cd ~/job-radar && sh deploy.sh'`, never with a bare `git pull`: only
   the listener restart sends the `CHANGES.md` news, and the script refuses user-facing changes
   without a section (`--no-news` only for changes nobody notices).
7. Tasks without code (deployment on the Pi) need no branch. Fixes they turn up go on a short
   branch of their own; findings that are not fixed right away become new issues.
