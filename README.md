# 🚀 KJZNNETx VPN Telegram Bot & WebApp 2.0

Современная многосерверная система управления VPN-сетью (**AmneziaWG**, **Xray Reality / Dokodemo**, **WireGuard**) с интерактивной панелью **Telegram Mini Apps (WebApp)**, умным биллингом и точным аппаратным шейпером скорости (QoS).

---

## 📸 Интерфейс и возможности (Showcase)

<div align="center">

| 🤖 Telegram Бот (`/start`) | 📱 Главный Дашборд WebApp |
|:---:|:---:|
| <img src="docs/screenshots/01_telegram_bot.png" width="380" alt="Telegram Bot" /> | <img src="docs/screenshots/02_dashboard.png" width="380" alt="Dashboard" /> |

| 👥 Список абонентов (Ноды и статусы) | ⚙️ Управление службами и узлом |
|:---:|:---:|
| <img src="docs/screenshots/03_subscribers.png" width="380" alt="Subscribers" /> | <img src="docs/screenshots/04_server_control.png" width="380" alt="Server Control" /> |

| 📊 Мониторинг системы 24ч (CPU / RAM) | 🌐 Мониторинг сети 24ч (RX / TX Трафик) |
|:---:|:---:|
| <img src="docs/screenshots/05_chart_system.png" width="380" alt="CPU and RAM Chart" /> | <img src="docs/screenshots/06_chart_network.png" width="380" alt="Network Bandwidth Chart" /> |

| 👤 Карточка абонента и статистика | ⚡ Аппаратный шейпинг QoS (Лимиты / Карцер) |
|:---:|:---:|
| <img src="docs/screenshots/07_user_card.png" width="380" alt="User Details Card" /> | <img src="docs/screenshots/08_user_speeds.png" width="380" alt="Speed Limiter QoS" /> |

| 💳 Продление подписки и биллинг | 📦 Массовые операции над абонентами |
|:---:|:---:|
| <img src="docs/screenshots/09_subscription_renewal.png" width="380" alt="Subscription Renewal" /> | <img src="docs/screenshots/10_batch_operations.png" width="380" alt="Batch Operations" /> |

| ✉️ Сообщение выбранным абонентам | 💬 Список обращений (Тикеты) |
|:---:|:---:|
| <img src="docs/screenshots/11_batch_messaging.png" width="380" alt="Direct Message to Selected" /> | <img src="docs/screenshots/12_tickets_list.png" width="380" alt="Tickets List" /> |

| 🧑‍💻 Чат с клиентом в тикете | 📢 Массовая системная рассылка |
|:---:|:---:|
| <img src="docs/screenshots/13_ticket_chat.png" width="380" alt="Ticket Chat" /> | <img src="docs/screenshots/14_broadcast.png" width="380" alt="Broadcast" /> |

| ⚙️ Пороги аномалий и экспорт бэкапов | |
|:---:|:---:|
| <img src="docs/screenshots/15_settings_backups.png" width="380" alt="Settings and Backups" /> | |

</div>

---

## 🌟 Основные возможности

* 📱 **Интерактивный WebApp Дашборд:** Графика нагрузки серверов (CPU, RAM, диск, сетевая активность в реальном времени), детальная статистика по клиентам, управление ключами и тарифами в 1 клик.
* 🌐 **Многосерверная архитектура:** Поддержка неограниченного числа локальных и удалённых нод через защищённые SSH ControlMaster соединения с автопереиспользованием сокетов.
* ⚡ **Аппаратный шейпер скорости (QoS):** Управление полосой пропускания через Linux Traffic Control (`tc`):
  * **Загрузка (Download / Egress):** Классы `HTB` с точным соответствием `rate == ceil` (исключает овербукинг и неконтролируемые всплески).
  * **Отдача (Upload / Ingress):** `Ingress policing` со строгим отбрасыванием пакетов (`action drop`) сверх установленного лимита (10 / 30 / 50 / 100 Мбит/с).
* 🔄 **Автоматическая синхронизация имён:** Чтение `/opt/amnezia/awg/clientsTable` и автоматическая подгрузка имён клиентов, созданных в приложении Amnezia.
* 💳 **Биллинг и уведомления:** Учёт сроков подписки, автонапоминания абонентам об оплате и возможность мягкого ограничения скорости при неуплате.
* 🛡️ **Безопасность:** Аутентификация администратора через криптографическую валидацию Telegram `initData` (HMAC-SHA256). Все чувствительные данные изолированы в `.env` и никогда не попадают в систему контроля версий.

---

## 📋 Системные требования

* **ОС:** Ubuntu 22.04 / 24.04 LTS или Debian 11 / 12.
* **Python:** 3.10, 3.11 или 3.12.
* **Docker:** Установленный Docker Engine.
* **VPN-контейнер:** AmneziaWG (образ `amnezia-awg2` или `spcfox/amnezia-wg-easy`, развернутый через приложение Amnezia).
* **Сетевые утилиты:** `iproute2`, `qrencode`, `curl`.

---

## 🚀 Установка

### 1. Клонирование репозитория
```bash
git clone https://github.com/StigMary/Bot-Amnezia-WG.git /opt/vpn_bot
cd /opt/vpn_bot
```

### 2. Запуск автоматического установщика
```bash
chmod +x install.sh update.sh
./install.sh
```
*Скрипт проверит Docker, создаст виртуальное окружение `.venv`, установит библиотеки, подготовит структуру директорий и запустит интерактивный мастер настройки.*

### 3. Интерактивная настройка (Wizard)
Если вы хотите повторно настроить бота или сконфигурировать токены:
```bash
python3 manage.py setup
```
Мастер спросит токен Telegram-бота от [@BotFather](https://t.me/BotFather), ваш Telegram ID и автоматически определит локальный Docker-контейнер AmneziaWG.

### 4. Запуск служб
```bash
sudo systemctl restart vpn-bot vpn-webapp
```

---

## 🖥️ Управление серверами (Dynamic Nodes)

Система поддерживает **любое количество VPN-серверов** (1, 2, 3, 5+).

### Как добавить новый сервер:
Выполните интерактивную команду:
```bash
python3 manage.py add-node
```
Мастер запросит:
1. Имя сервера (например: `🇳🇱 Нидерланды` или `🇩🇪 Германия Exit-Node`).
2. IP-адрес или домен.
3. SSH-порт и пользователя (с беспарольным sudo).
4. Протестирует SSH-связь, автоматически добавит сервер в `.env` и перезапустит бота. Новый сервер сразу появится на дашборде WebApp!

### Список серверов и их онлайн-статус:
```bash
python3 manage.py list-nodes
```

### Проверка здоровья системы (Self-Test):
```bash
python3 manage.py test
```

---

## ⚙️ Конфигурация (.env)

| Переменная | Обязательная | Описание |
| :--- | :---: | :--- |
| `BOT_TOKEN` | **Да** | API-токен бота от [@BotFather](https://t.me/BotFather). |
| `ADMIN_ID` | **Да** | Числовой Telegram ID администратора (узнать в [@userinfobot](https://t.me/userinfobot)). |
| `ADMIN_API_KEY` | Нет | Секретный токен для внешнего API-доступа к FastAPI (для выгрузки бэкапов и автоматизации). |
| `GROUP_CHAT_ID` | Нет | ID закрытого канала/чата (если включена проверка подписки абонента, иначе `0`). |
| `WEBHOOK_URL` | Нет | Публичный HTTPS-адрес панели (Cloudflare Tunnel или ваш домен) для кнопки WebApp. |
| `NODE_1` | Нет | Описание сервера #1 в формате `ИМЯ\|local(или IP)\|PORT\|USER\|CONTAINER\|IFACE`. |
| `NODE_2`..`NODE_20` | Нет | Дополнительные удаленные серверы, подключенные по SSH. |
| `PAYMENT_DETAILS` | Нет | Текст реквизитов для оплаты (показывается абонентам в боте). |
| `PAYMENT_AMOUNT` | Нет | Стоимость подписки (например, `150 руб./мес.`). |

---

## 🌐 Настройка WebApp в Telegram

1. **Публикация порта 8080 через Cloudflare Quick Tunnel:**
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8080
   ```
   *(Скопируйте полученный адрес вида `https://xxxx.trycloudflare.com` в переменную `WEBHOOK_URL` в `.env`)*.
2. **Регистрация кнопки в BotFather:**
   * Откройте [@BotFather](https://t.me/BotFather) ➔ `/mybots` ➔ выберите вашего бота.
   * Перейдите в **Bot Settings** ➔ **Menu Button** ➔ **Configure menu button**.
   * Отправьте ссылку на ваш WebApp: `https://xxxx.trycloudflare.com`.
   * Задайте название кнопки, например: `⚡ Дашборд`.

---

## 🔄 Обновление

Для обновления бота и веб-панели до последней версии достаточно выполнить:
```bash
cd /opt/vpn_bot
./update.sh
```
Скрипт сам выполнит `git pull`, доустановит зависимости и перезапустит службы.

---

## 🔒 Безопасность и архитектура

1. **Файловые права:** Конфигурация `.env` создается с правами `600`, а каталог базы данных `data/` — с правами `700`.
2. **Изоляция WebApp:** Сервер `uvicorn` слушает только локальный интерфейс `127.0.0.1:8080` и доступен исключительно через защищенный HTTPS-прокси (Cloudflare Tunnel / Nginx).
3. **SSH ControlMaster:** Для удалённых нод используется мультиплексирование SSH-соединений с проверкой `StrictHostKeyChecking=yes` по файлу `known_hosts`.
4. **Харденинг служб:** Systemd-юниты запускаются с директивами `NoNewPrivileges=true` и `ProtectSystem=full`.

Подробная архитектурная схема и описание взаимодействия сервисов доступны в файле [docs/project_documentation.md](docs/project_documentation.md).

---

## 📄 Лицензия

Проект распространяется под лицензией [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE).
