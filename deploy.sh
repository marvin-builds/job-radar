#!/bin/sh
# Deploy the newest main on this machine: pull, install the units, restart the
# listener, which sends everyone the new CHANGES.md sections.
# Usage: sh deploy.sh [--no-news] [--force]
#   --no-news  deploy code changes without a new CHANGES.md section (nobody will notice them)
#   --force    restart the listener even while an interview turn is running
set -eu
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
news=1
force=0
for arg in "$@"; do
    case "$arg" in
        --no-news) news=0 ;;
        --force) force=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

git fetch -q origin
if [ "$news" = 1 ] && git diff --quiet HEAD origin/main -- CHANGES.md \
        && ! git diff --quiet HEAD origin/main -- job_radar prompts systemd; then
    echo "Code people use changed, but CHANGES.md has no new section." >&2
    echo "Add one (see README, 'Telling people what is new'), or use --no-news if nobody will notice." >&2
    exit 1
fi
git merge -q --ff-only origin/main
sh install.sh >/dev/null
# Timers pick up a changed schedule only on restart; a running poll is not touched.
for timer in $(systemctl --user list-units --plain --no-legend 'job-radar-poll@*.timer' | awk '{print $1}'); do
    systemctl --user restart "$timer"
done

# A restart cuts off an interview turn that is waiting for Claude; wait up to 10 minutes.
if [ "$force" = 0 ]; then
    tries=0
    while ! python3 -m job_radar busy; do
        tries=$((tries + 1))
        if [ "$tries" -ge 30 ]; then
            echo "Still busy after 10 minutes; listener not restarted. Run deploy.sh again later, or with --force." >&2
            exit 1
        fi
        sleep 20
    done
fi
systemctl --user restart job-radar-listen
sleep 3
systemctl --user is-active job-radar-listen
git log --oneline -1
