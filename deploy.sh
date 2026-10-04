#!/bin/sh
# Deploy the newest main on this machine: pull, install the units, restart the
# listener, which sends everyone the new CHANGES.md sections.
# Refuses when a merge changes what people use without a CHANGES.md section or a
# "No-news: <reason>" line (python3 -m job_radar check-news).
# Usage: sh deploy.sh [--force] [--skip-news-check]
#   --force            restart the listener even while an interview turn is running
#   --skip-news-check  only for a merge already pushed without its line (history stays as it is)
set -eu
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
force=0
check=1
for arg in "$@"; do
    case "$arg" in
        --force) force=1 ;;
        --skip-news-check) check=0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

git fetch -q origin
if [ "$check" = 1 ]; then
    python3 -m job_radar check-news HEAD origin/main >&2
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
