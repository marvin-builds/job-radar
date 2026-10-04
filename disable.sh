#!/bin/sh
# Stop job-radar. Usage: sh disable.sh [--uninstall]
# Stops and disables the poll timer and the Telegram listener. --uninstall also
# removes the unit files. Config, secrets and the database are always kept.
# Turn it back on with: sh install.sh --enable
set -eu
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now job-radar-poll.timer job-radar-listen.service 2>/dev/null || true
systemctl --user stop job-radar-poll.service 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$units"/job-radar-poll.service "$units"/job-radar-poll.timer "$units"/job-radar-listen.service
    systemctl --user daemon-reload
    echo "Disabled and unit files removed. Config and data are kept in ~/.config/job-radar and ~/.local/share/job-radar."
else
    echo "Disabled. No polls, no Telegram replies. Re-enable with: sh install.sh --enable"
fi
