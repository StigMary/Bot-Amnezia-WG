#!/bin/bash
# deploy.sh — Безопасный деплой бота на продакшн-сервер
# Запускать на СЕРВЕРЕ от vpnuser:
#   bash deploy.sh
# Или запускать удалённо с локальной машины (Windows):
#   ssh vpnuser@103.110.66.170 -p 49223 "bash /home/vpnuser/vpn_bot/deploy.sh"

set -e

BOT_DIR="/home/vpnuser/vpn_bot"
SERVICE="vpn-bot"
BRANCH="${1:-main}"  # По умолчанию main, можно передать как аргумент

echo "=== Деплой VPN Bot — ветка: $BRANCH ==="

# 1. Остановить сервис
echo "[1/6] Останавливаю бота..."
sudo systemctl stop "$SERVICE" 2>/dev/null || true

# 2. Обновить код
echo "[2/6] Обновляю код с GitHub..."
cd "$BOT_DIR"
git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

# 3. Создать/обновить venv
echo "[3/6] Обновляю зависимости..."
if [ ! -d "$BOT_DIR/venv" ]; then
    python3 -m venv "$BOT_DIR/venv"
fi
"$BOT_DIR/venv/bin/pip" install -q --upgrade pip
"$BOT_DIR/venv/bin/pip" install -q -r "$BOT_DIR/requirements.txt"

# 4. Создать папку данных
echo "[4/6] Проверяю папку data/..."
mkdir -p "$BOT_DIR/data"

# 5. Установить/обновить systemd-сервис
echo "[5/6] Обновляю systemd-сервис..."
sudo cp "$BOT_DIR/vpn-bot.service" /etc/systemd/system/"$SERVICE".service
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"

# 6. Запустить
echo "[6/6] Запускаю бота..."
sudo systemctl start "$SERVICE"
sleep 3
sudo systemctl status "$SERVICE" --no-pager

echo ""
echo "=== Деплой завершён! ==="
echo "Логи: sudo journalctl -u $SERVICE -f"
