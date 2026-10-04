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
3. Work on the branch the task file names. Create it from an up-to-date `main` unless it
   already exists (task 01 continues `generalize-onboarding`). Branches stay short: one task,
   then merge.
4. Write failing tests first for logic (see "Testing" in `plan.md`), commit one concern at a
   time, never push without asking.
5. When the task is done: run `python3 -m unittest`, run `/code-review` on the branch and fix
   what it finds, tick the task in `status.md`, set the task file's status to `done`, and note
   anything the next task needs to know (commit that too).
6. Then ask me whether to merge into `main` (`git merge --no-ff <branch>`) and push. After
   the merge, `main` must be consistent: tests green, nothing half-wired.
7. Tasks without code (deployment on the Pi) need no branch. Fixes they turn up go on a short
   branch of their own, named in the task.
