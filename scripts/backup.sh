#!/usr/bin/env bash
# ==============================================================================
# scripts/backup.sh — Автоматический бэкап базы данных и конфигураций
# ==============================================================================
set -euo pipefail

BACKUP_DIR="${HOME}/backups"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

DATE=$(date +%Y-%m-%d_%H%M%S)
DATA_DIR="/home/vpnuser/vpn_bot/data"

# 1. Горячий бэкап SQLite базы
if [ -f "${DATA_DIR}/vpn_bot.db" ]; then
    sqlite3 "${DATA_DIR}/vpn_bot.db" ".backup ${BACKUP_DIR}/vpn_bot-${DATE}.db"
    chmod 600 "${BACKUP_DIR}/vpn_bot-${DATE}.db"
fi

# 2. Бэкап файлов конфигураций пользователей
if [ -d "${DATA_DIR}/configs" ]; then
    tar -czf "${BACKUP_DIR}/configs-${DATE}.tar.gz" -C "${DATA_DIR}" configs
    chmod 600 "${BACKUP_DIR}/configs-${DATE}.tar.gz"
fi

# 3. Ротация: удаление бэкапов старше 14 дней
find "$BACKUP_DIR" -type f -mtime +14 -delete 2>/dev/null || true

echo "Бэкап успешно создан в ${BACKUP_DIR} за ${DATE}"
