#!/bin/sh
# Install job-radar as systemd user units. Usage: sh install.sh [--enable]
set -eu
repo=$(cd "$(dirname "$0")" && pwd)
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/job-radar"

mkdir -p "$units" "$conf"
for f in "$repo"/systemd/*; do
    sed "s|@REPO@|$repo|g" "$f" > "$units/$(basename "$f")"
done
[ -f "$conf/config.json" ] || cp "$repo/config.example.json" "$conf/config.json"
if [ ! -f "$conf/env" ]; then
    printf 'TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n' > "$conf/env"
fi
chmod 600 "$conf/env"
systemctl --user daemon-reload

if [ "${1:-}" = "--enable" ]; then
    systemctl --user enable --now job-radar-poll.timer job-radar-listen.service
    echo "Enabled. Check: systemctl --user list-timers; journalctl --user -u job-radar-listen -f"
else
    echo "Units installed. Put TELEGRAM_BOT_TOKEN into $conf/env, message your bot,"
    echo "run 'python3 -m job_radar setup-chat', adjust $conf/config.json,"
    echo "check with 'python3 -m job_radar doctor', then 'sh install.sh --enable'."
fi
