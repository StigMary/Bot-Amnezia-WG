# 🛡 VPN Management Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

*Read this in [English](README_EN.md)*

Telegram-бот для управления VPN-сервером на базе **AmneziaVPN / WireGuard**.  
Позволяет администратору управлять клиентами, подписками и устройствами прямо из Telegram.

---

## ✨ Возможности

### 👑 Для администратора
- 📋 Просмотр списка всех клиентов с IP, алиасами и датами подписки
- 💳 Продление подписки клиентам (в т.ч. ручная установка даты на 28-е число)
- 🔗 Тихая привязка конфигов к Telegram-аккаунтам (без уведомлений клиентам)
- 📤 Выдача второго устройства с **авто-парсингом IP** из `.conf` файла
- 🚦 Управление скоростью (QoS): лимитирование / снятие ограничений
- 📊 Утренний дайджест: только важные события (просрочка, скоро истекает)
- 🎟 Helpdesk: получение тикетов от клиентов с кнопкой ответа и закрытия

### 👤 Для клиентов
- 🏠 Личный кабинет: статус подписки, список устройств
- 💳 Оплата: реквизиты + отправка фото чека администратору
- ➕ Запрос второго устройства (лимит: 2 конфигурации на аккаунт)
- ✏️ Переименование своих устройств (не затрагивает пометки администратора)
- 🛠 Инструкция по подключению со свежими ссылками на приложения
- 🎧 Поддержка: открытый тикет с возможностью писать сообщения и фото

---

## 🗂 Структура проекта

```
vpn_bot/
├── bot.py                  # Точка входа, инициализация бота
├── config.py               # Загрузка настроек из .env
├── database.py             # SQLite: схема, миграции, CRUD
├── tasks.py                # Планировщик (утренний дайджест, виджет)
├── vpn_engine.py           # Работа с WireGuard: парсинг, QoS
├── handlers/
│   ├── admin.py            # Все команды администратора
│   ├── client.py           # Клиентское меню и тикет-система
│   └── decorators.py       # Middleware: rate_limit, admin_only и др.
├── deploy/                 # Документация и скрипты production-деплоя
├── data/                   # ← НЕ коммитить (БД, логи, пиды)
├── .env                    # ← НЕ коммитить (секреты)
├── .env.example            # Шаблон переменных окружения
├── requirements.txt        # Зависимости Python
└── vpn-bot.service         # Systemd unit-файл для автозапуска
```

---

## 🚀 Установка и деплой

Полная инструкция по **Production-развёртыванию** (настройка `systemd`, `venv`, изоляция прав пользователя, скрипты авто-обновления) находится в папке [deploy/README.md](deploy/README.md).

### Краткий запуск для разработки
```bash
git clone https://github.com/ВАШ_АККАУНТ/vpn_bot.git
cd vpn_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Заполните .env файл
python bot.py
```

---

## ⚙️ Переменные окружения (`.env`)

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен бота от @BotFather |
| `ADMIN_ID` | Telegram ID администратора |
| `GROUP_ID` | ID группы для верификации клиентов |
| `PAYMENT_DETAILS` | Номер телефона / реквизиты оплаты |
| `PAYMENT_AMOUNT` | Стоимость подписки (например `200₽`) |
| `DB_FILE` | Путь к файлу базы данных SQLite |
| `SERVER_NAME` | Название сервера (для дайджеста) |

---

## 🔒 Безопасность

- SQL-инъекции закрыты через **whitelist полей** (`ALLOWED_FIELDS`) в `database.py`
- Все пользовательские алиасы проходят через `sanitize_alias()` (защита от XSS)
- Rate limiting на клиентских командах
- Разделение прав: middleware `admin_only` / `group_member_only`
- Секреты вынесены в `.env` и исключены из репозитория

---

## 📝 Лицензия

Проект распространяется под лицензией [GNU AGPLv3](LICENSE).

---

## ☕ Поддержать проект

Если этот проект оказался полезным — буду рад любой поддержке! 🙏

| Способ | Ссылка |
|---|---|
| 🚀 **Boosty** | [boosty.to/kjznnetx/donate](https://boosty.to/kjznnetx/donate) |
| 💎 **TON** | `UQCGEByMVxefI4hpLvoCOuvaS9EbDPC0d-wPzMCuBql9DFJW` |

[![Boosty](https://img.shields.io/badge/Boosty-%D0%BF%D0%BE%D0%B4%D0%B4%D0%B5%D1%80%D0%B6%D0%B0%D1%82%D1%8C-orange?style=for-the-badge)](https://boosty.to/kjznnetx/donate)
[![TON](https://img.shields.io/badge/TON-%D0%BE%D1%82%D0%BF%D1%80%D0%B0%D0%B2%D0%B8%D1%82%D1%8C-0098EA?style=for-the-badge&logo=telegram)](https://app.tonkeeper.com/transfer/UQCGEByMVxefI4hpLvoCOuvaS9EbDPC0d-wPzMCuBql9DFJW)
