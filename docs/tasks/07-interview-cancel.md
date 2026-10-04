# 07 – Cancel an interview

Status: open · Needs: – · Files: `job_radar/app.py`, `prompts/interview.md` · Branch: `interview-cancel`

## Goal

A person can stop the interview, and the bot only claims what really happened.

## Found in task 03

In the Pi test I wrote "Abbruch dieses interviews". Claude answered "ich breche das Interview
hier ab. Es wurde nichts gespeichert", but the state stayed `interview=active`: the next
message would have continued the interview. There is no command for it.

## Scope

- A `/abbrechen` command (in `COMMANDS` and the help text) that clears the interview
  state and transcript, and says so; an existing profile stays.
- The prompt must not promise actions the code does not take; if the person wants to stop,
  point them to `/abbrechen`.
- Test first in `tests/test_app.py`.
