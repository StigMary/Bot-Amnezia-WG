#!/bin/bash
# Отправляет alert в Telegram при падении systemd unit.
# Usage: systemd-telegram-notify.sh <unit_name>
#
# Читает BOT_TOKEN и ADMIN_ID из /home/vpnuser/.env (тот же .env что у бота).

UNIT="${1:-unknown}"
ENV_FILE="/home/vpnuser/.env"

if [ ! -r "$ENV_FILE" ]; then
    logger -t systemd-telegram-notify "ERROR: cannot read $ENV_FILE"
    exit 1
fi

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_ID" ]; then
    logger -t systemd-telegram-notify "ERROR: BOT_TOKEN/ADMIN_ID not set"
    exit 1
fi

HOST=$(hostname)
STATUS=$(systemctl status "$UNIT" --no-pager -l 2>&1 | head -20 | sed 's/[<>&]//g')
TIME=$(date '+%Y-%m-%d %H:%M:%S %Z')

TEXT="🚨 <b>SYSTEMD ALERT</b>%0A<b>Host:</b> ${HOST}%0A<b>Unit:</b> ${UNIT}%0A<b>Time:</b> ${TIME}%0A%0A<pre>${STATUS}</pre>"

curl -s -m 10 -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d "chat_id=${ADMIN_ID}" \
    -d "parse_mode=HTML" \
    -d "text=${TEXT}" >/dev/null
