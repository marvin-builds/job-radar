#!/bin/sh
# Install job-radar as systemd user units. Usage: sh install.sh [--enable]
# One listener serves the shared bot; each instance gets its own poll timer
# (job-radar-poll@<name>.timer). --enable starts the listener and the timer
# of every instance in ~/.config/job-radar/instances.
set -eu
repo=$(cd "$(dirname "$0")" && pwd)
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/job-radar"

mkdir -p "$units" "$conf"
# Drop the units of the old single-instance layout.
if [ -f "$units/job-radar-poll.timer" ]; then
    systemctl --user disable --now job-radar-poll.timer 2>/dev/null || true
    rm -f "$units/job-radar-poll.service" "$units/job-radar-poll.timer"
fi
for f in "$repo"/systemd/*; do
    sed "s|@REPO@|$repo|g" "$f" > "$units/$(basename "$f")"
done
[ -f "$conf/env" ] || printf 'TELEGRAM_BOT_TOKEN=\n' > "$conf/env"
chmod 600 "$conf/env"
systemctl --user daemon-reload

if [ "${1:-}" = "--enable" ]; then
    systemctl --user enable --now job-radar-listen.service
    for d in "$conf"/instances/*/; do
        [ -d "$d" ] || continue
        systemctl --user enable --now "job-radar-poll@$(basename "$d").timer"
    done
    echo "Enabled. Check: systemctl --user list-timers; journalctl --user -u job-radar-listen -f"
else
    echo "Units installed. Put TELEGRAM_BOT_TOKEN into $conf/env, create an instance with"
    echo "'python3 -m job_radar --instance <name> invite', check with 'python3 -m job_radar doctor',"
    echo "then 'sh install.sh --enable'. Run it again after each new instance to start its timer."
fi
