#!/bin/sh
# Stop job-radar. Usage: sh disable.sh [--uninstall]
# Stops and disables the Telegram listener and the poll timers of all
# instances. --uninstall also removes the unit files. Config, secrets and the
# databases are always kept. Turn it back on with: sh install.sh --enable
set -eu
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now job-radar-listen.service 2>/dev/null || true
# Loaded timers, plus those of instances whose timer is not loaded right now.
conf="${XDG_CONFIG_HOME:-$HOME/.config}/job-radar"
timers=$(systemctl --user list-units --all --plain --no-legend 'job-radar-poll@*.timer' 2>/dev/null | awk '{print $1}')
for d in "$conf"/instances/*/; do
    [ -d "$d" ] && timers="$timers job-radar-poll@$(basename "$d").timer"
done
for t in $timers; do
    systemctl --user disable --now "$t" 2>/dev/null || true
done
systemctl --user stop 'job-radar-poll@*.service' 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$units"/job-radar-listen.service "$units"/job-radar-poll@.service "$units"/job-radar-poll@.timer
    systemctl --user daemon-reload
    echo "Disabled and unit files removed. Config and data are kept in ~/.config/job-radar and ~/.local/share/job-radar."
else
    echo "Disabled. No polls, no Telegram replies. Re-enable with: sh install.sh --enable"
fi
