#!/bin/bash
# ==============================================================================
# VPN Bot & WebApp — Скрипт быстрого обновления
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${BLUE}🔄 Получение обновлений из Git...${NC}"
git pull

echo -e "\n${BLUE}📦 Проверка зависимостей...${NC}"
if [ -f "$SCRIPT_DIR/.venv/bin/pip" ]; then
    "$SCRIPT_DIR/.venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"
fi

echo -e "\n${BLUE}🚀 Перезапуск служб...${NC}"
SERVICE_BOT="vpn-bot"
SERVICE_WEBAPP="vpn-webapp"

# Если службы названы msk_bot (как на RU-сервере), учитываем это
if systemctl list-units --full -all | grep -Fq "msk_bot.service"; then
    SERVICE_BOT="msk_bot"
fi

sudo systemctl restart "$SERVICE_BOT" "$SERVICE_WEBAPP"

echo -e "\n${GREEN}✓ Обновление успешно установлено!${NC}"
sudo systemctl status "$SERVICE_BOT" --no-pager -l | head -n 12
echo "---"
sudo systemctl status "$SERVICE_WEBAPP" --no-pager -l | head -n 12
