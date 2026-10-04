# Project docs: job-radar for the family

job-radar is being turned from my personal radar into a service that several people in my
household use through one shared Telegram bot, all running on a Raspberry Pi.

| File | What it holds |
| --- | --- |
| [plan.md](plan.md) | Goal, decisions taken, target architecture |
| [status.md](status.md) | What is done and verified, what is broken right now, where things stand on the Pi |
| [tasks/](tasks/) | One file per open task, each sized for one session |

## Working on a task in a new session

1. Read `plan.md` (decisions are settled; don't reopen them without asking) and `status.md`.
2. Pick the lowest-numbered open task in `tasks/` whose prerequisites are done.
3. Work on branch `generalize-onboarding`. Write failing tests first for logic (see
   "Testing" in `plan.md`), commit one concern at a time, never push without asking.
4. When the task is done: tick it in `status.md`, set the task file's status to `done`, and
   note anything the next task needs to know.
