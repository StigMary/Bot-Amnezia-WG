#!/bin/bash
# ==============================================================================
# VPN Bot & WebApp — Автоматический установщик для Linux (Debian/Ubuntu)
# ==============================================================================

set -e

# Цвета для красивого вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}=====================================================${NC}"
echo -e "${BLUE}    🚀 Установка KJZNNETx VPN Bot & WebApp           ${NC}"
echo -e "${BLUE}=====================================================${NC}"

# 1. Проверка рабочей директории и пользователя
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"

echo -e "${YELLOW}📂 Рабочая директория:${NC} $SCRIPT_DIR"
echo -e "${YELLOW}👤 Пользователь:${NC} $CURRENT_USER"

# 2. Обновление пакетов и установка зависимостей
echo -e "\n${BLUE}📦 [1/5] Проверка и установка системных пакетов...${NC}"
sudo apt update -y
sudo apt install -y python3 python3-venv python3-pip git curl qrencode iproute2

# 3. Создание venv и установка Python библиотек
echo -e "\n${BLUE}🐍 [2/5] Настройка виртуального окружения Python...${NC}"
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    python3 -m venv "$SCRIPT_DIR/.venv"
    echo -e "${GREEN}✓ Виртуальное окружение создано${NC}"
fi

"$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip
"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
echo -e "${GREEN}✓ Все Python-библиотеки успешно установлены${NC}"

# 4. Каталоги и конфигурация
echo -e "\n${BLUE}⚙️ [3/5] Подготовка директорий и конфигурации...${NC}"
mkdir -p "$SCRIPT_DIR/data/configs"
chmod 700 "$SCRIPT_DIR/data"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    chmod 600 "$SCRIPT_DIR/.env"
    echo -e "${YELLOW}⚠️ Создан файл .env из шаблона. ОБЯЗАТЕЛЬНО укажите в нем BOT_TOKEN и ADMIN_ID!${NC}"
else
    echo -e "${GREEN}✓ Файл .env уже существует${NC}"
fi

# 5. Создание systemd служб
echo -e "\n${BLUE}🔧 [4/5] Настройка системных служб Systemd...${NC}"

# Служба бота
sudo bash -c "cat <<EOF > /etc/systemd/system/vpn-bot.service
[Unit]
Description=KJZNNETx VPN Telegram Bot
After=network.target docker.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/.venv/bin/python3 $SCRIPT_DIR/bot.py
Restart=always
RestartSec=5
MemoryMax=400M

[Install]
WantedBy=multi-user.target
EOF"

# Служба WebApp
sudo bash -c "cat <<EOF > /etc/systemd/system/vpn-webapp.service
[Unit]
Description=KJZNNETx VPN WebApp (FastAPI)
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR/webapp
Environment=\"PYTHONPATH=$SCRIPT_DIR\"
ExecStart=$SCRIPT_DIR/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF"

sudo systemctl daemon-reload
sudo systemctl enable vpn-bot vpn-webapp

echo -e "${GREEN}✓ Службы vpn-bot и vpn-webapp зарегистрированы и включены в автозагрузку${NC}"

# 6. Финал
echo -e "\n${BLUE}🏁 [5/5] Завершение установки...${NC}"
echo -e "${GREEN}=====================================================${NC}"
echo -e "${GREEN}  🎉 Установка успешно завершена!                   ${NC}"
echo -e "${GREEN}=====================================================${NC}"
echo -e "Чтобы запустить бота прямо сейчас, выполните:"
echo -e "${YELLOW}  sudo systemctl restart vpn-bot vpn-webapp${NC}"
echo -e "Для просмотра логов:"
echo -e "${YELLOW}  journalctl -u vpn-bot -f${NC}"
echo -e "${YELLOW}  journalctl -u vpn-webapp -f${NC}"
