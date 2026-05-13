# Deploy: systemd unit + hardening + alerts

Файлы для production-развёртывания бота на Linux (Ubuntu 22.04+).

## Структура
- [`../../vpn-bot.service`](../../vpn-bot.service) — основной unit (использует `.venv/bin/python`)
- [`hardening.conf`](hardening.conf) — drop-in: лимит памяти, sandboxing, OnFailure→Telegram
- [`alert@.service`](alert@.service) — template-юнит, шлёт алерт при падении
- [`systemd-telegram-notify.sh`](systemd-telegram-notify.sh) — скрипт-нотификатор (читает `/home/vpnuser/.env`)

## Установка с нуля

```bash
# 1. Клонируем репо
sudo -iu vpnuser
cd ~ && git clone https://github.com/StigMary/Bot-Amnezia-WG.git vpn_bot
cd vpn_bot

# 2. Виртуальное окружение
sudo apt-get install -y python3-venv python3-pip
python3 -m venv .venv
.venv/bin/pip install --upgrade pip wheel setuptools
.venv/bin/pip install -r requirements.txt

# 3. .env (BOT_TOKEN, ADMIN_ID и пр. — см. .env.example)
cp .env.example /home/vpnuser/.env
chmod 600 /home/vpnuser/.env
nano /home/vpnuser/.env

# 4. systemd unit + hardening + alerts (требуется sudo)
exit  # вернуться в root
cd /home/vpnuser/vpn_bot
sudo cp vpn-bot.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/vpn-bot.service.d
sudo cp deploy/systemd/hardening.conf /etc/systemd/system/vpn-bot.service.d/
sudo cp deploy/systemd/alert@.service /etc/systemd/system/
sudo cp deploy/systemd/systemd-telegram-notify.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/systemd-telegram-notify.sh

sudo systemctl daemon-reload
sudo systemctl enable --now vpn-bot.service
sudo systemctl status vpn-bot.service
```

## Тест alert
```bash
sudo systemctl start alert@vpn-bot.service.service
# В Telegram должно прийти "🚨 SYSTEMD ALERT" со статусом
```

## Проверка лимитов
```bash
systemctl show vpn-bot.service -p MemoryMax,MemoryHigh,StartLimitBurst
```
