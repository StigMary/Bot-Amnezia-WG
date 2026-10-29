# Документация проекта KJZNNETx VPN (Bot + WebApp)

Этот документ описывает архитектуру, технические детали, местоположение файлов и полный функционал системы управления VPN-пользователями.

## 1. Инфраструктура и Хостинг

Система развернута на главном сервере (SE Node).

*   **Директория проекта:** `/home/vpnuser/vpn_bot/`
*   **Конфигурация:** Все секреты (токен бота, ID администратора, реквизиты оплаты) хранятся в `/home/vpnuser/.env` (права 600)

> ⚠️ IP-адрес сервера, SSH-порт и имя пользователя не указаны в документации из соображений безопасности.
> Они хранятся в защищённом месте отдельно.

### Системные сервисы (systemd)
Управление сервером осуществляется через systemd-сервисы:
1.  `vpn-bot.service` — Telegram-бот (фоновые задачи, клиенты, inline-админка). Включён hardening: `MemoryMax=200M`, `NoNewPrivileges=true`, `ProtectSystem=strict`.
2.  `vpn-webapp.service` — FastAPI бэкенд для WebApp (uvicorn на `127.0.0.1:8080`).
3.  `cloudflared.service` — Cloudflare Tunnel для безопасного доступа к WebApp извне.

Команды управления:
```bash
sudo systemctl restart vpn-bot.service vpn-webapp.service
sudo systemctl restart cloudflared.service
journalctl -u vpn-bot.service -n 50 -f      # Логи бота
journalctl -u vpn-webapp.service -n 50 -f    # Логи WebApp
journalctl -u cloudflared.service -n 20      # Логи Tunnel
```

### Безопасность сервера
*   SSH на нестандартном порту, только ключевая аутентификация (`PasswordAuthentication no`)
*   `PermitRootLogin no`
*   UFW с политикой `DROP` по умолчанию
*   fail2ban для защиты от brute-force
*   systemd hardening (`ProtectSystem=strict`, `ProtectHome=read-only`, `MemoryMax`)
*   Cloudflare Tunnel (WebApp не выставлен напрямую в интернет)

---

## 2. Структура файлов

Все файлы проекта находятся в папке `/home/vpnuser/vpn_bot/`. Виртуальное окружение Python находится в `.venv/`.

### Бэкенд и База Данных (Корень проекта)
*   `bot.py` — Точка входа для Telegram-бота (инициализация `telebot`, graceful shutdown, single-instance lock, экспоненциальный backoff).
*   `config.py` — Конфигурационный файл (dataclass Config + dotenv + RotatingFileHandler).
*   `database.py` — Модуль для работы с SQLite (WAL mode, контекстный менеджер, миграции, CRUD, whitelist полей для защиты от SQL-инъекций).
*   `data/vpn_bot.db` — БД SQLite (WAL mode, busy_timeout=30000).
*   `data/bot.log` — Лог-файл (RotatingFileHandler, 5MB × 3 файла).
*   `data/metrics.csv` — Метрики для графиков.
*   `vpn_engine.py` — Интеграция с VPN-серверами (SSH ControlMaster, docker exec, tc/htb QoS, ThreadPoolExecutor, кэш 30с).
*   `tasks.py` — Планировщик (сохранение виджетов, авто-карцер, утренние дайджесты, сбор метрик).
*   `requirements.txt` — Зависимости Python.

### Обработчики Telegram-бота (`handlers/`)
*   `handlers/admin.py` — Текстовая панель администратора (`/start`, `/search`, серверы, бэкапы, журнал).
*   `handlers/client.py` — Клиентская часть (личный кабинет, тикеты, генерация ключей).
*   `handlers/decorators.py` — Декораторы проверки прав (`@admin_only`, `@group_member_only`, `@rate_limit`).

### Telegram Mini App (`webapp/`)
Полноценная CRM для администратора.
*   `webapp/main.py` — Асинхронный сервер на FastAPI (API, валидация initData, aiogram для рассылки, rate-limit на опасные действия).
*   `webapp/static/index.html` — SPA-интерфейс (HTML + Vanilla JS).
*   `webapp/static/index.css` — Стилизация (Glassmorphism, градиенты, тёмная тема).
*   `webapp/static/chart.js` — Библиотека графиков.

### Служебные директории
*   `data/` — БД, логи, метрики, PID-файл.
*   `backups/` — Автоматические ежедневные бэкапы БД (cron, хранение 30 дней).
*   `legacy/` — Архив старых/тестовых файлов (не используется в production).
*   `.venv/` — Виртуальное окружение Python.

---

## 3. Схема базы данных

### users
| Поле | Тип | По умолчанию | PK |
|------|------|-------------|-----|
| ip_address | TEXT | - | ✅ |
| alias | TEXT | 'Новый пользователь' | - |
| protocol | TEXT | - | - |
| created_at | TIMESTAMP | CURRENT_TIMESTAMP | - |
| speed_limit | TEXT | 'max' | - |
| tg_user_id | INTEGER | NULL | - |
| paid_until | TEXT | NULL | - |
| client_alias | TEXT | NULL | - |

### audit_log
| Поле | Тип | По умолчанию | PK |
|------|------|-------------|-----|
| id | INTEGER | AUTO | ✅ |
| timestamp | TEXT | - | - |
| action | TEXT | - | - |
| target_ip | TEXT | - | - |
| details | TEXT | - | - |
| admin_id | INTEGER | - | - |

### tickets
| Поле | Тип | По умолчанию | PK |
|------|------|-------------|-----|
| id | INTEGER | AUTO | ✅ |
| tg_user_id | INTEGER | - | - |
| alias | TEXT | - | - |
| messages | TEXT (JSON) | - | - |
| status | TEXT | 'open' | - |
| created_at | TEXT | - | - |

### server_stats
| Поле | Тип | По умолчанию | PK |
|------|------|-------------|-----|
| id | INTEGER | AUTO | ✅ |
| timestamp | INTEGER | - | - |
| server_name | TEXT | - | - |
| cpu | REAL | - | - |
| ram | REAL | - | - |
| rx_speed | REAL | - | - |
| tx_speed | REAL | - | - |

### settings
| Поле | Тип | По умолчанию | PK |
|------|------|-------------|-----|
| key | TEXT | - | ✅ |
| value | TEXT | - | - |

### schema_version
| Поле | Тип | По умолчанию | PK |
|------|------|-------------|-----|
| version | INTEGER | - | ✅ |

---

## 4. Детальный функционал системы

Система разделена на три логических блока: Клиентский Бот, Админский Бот (пульт) и WebApp CRM.

### A. Клиентский Бот (Для абонентов)
1. **Регистрация / Привязка:** Пользователь привязывает свой Telegram-аккаунт к IP-адресу в VPN (или админ делает это вручную через `/bind`).
2. **Личный кабинет:** Команда `/start` показывает пользователю его выделенный IP, сервер (SE/RU), статус оплаты (до какого числа) и объем потраченного трафика.
3. **Поддержка (Тикеты):** Пользователь может отправить текстовое сообщение боту, и оно автоматически сформирует тикет. Ответ администратора придет пользователю сообщением от лица бота.

### B. Telegram Админка (Пульт администратора в боте)
Взаимодействие происходит напрямую в чате с ботом.
1. **Быстрый дашборд (`/start`):** Выводит онлайн-статус пользователей, загрузку CPU/RAM главного сервера и статус RU-ноды. Содержит кнопку запуска WebApp.
2. **Мониторинг Серверов ("📡 Серверы"):**
   * *Статус* — текстовые progress-бары использования ресурсов серверов.
   * *Нагрузка* — генерация суточных графиков (через matplotlib).
   * *Пинг* — измерение пинга с RU-сервера до Google, Telegram и т.д.
   * *Рестарт SE / RU* — принудительная перезагрузка Docker-контейнеров `amnezia-awg` с предварительным подтверждением.
3. **Обслуживание ("🛠 Сервис"):**
   * *Бэкап* — скачивание актуальной базы данных файлом в чат.
   * *Журнал действий* — просмотр логов администратора.
   * *Дайджест* — принудительный вызов утреннего отчета.
4. **Быстрый поиск ("🔍 Поиск" / `/search`):** Найти абонента по IP и узнать, какому Alias он принадлежит.

### C. WebApp (CRM Система)
Основной инструмент управления бизнесом. Открывается внутри Telegram как встроенное мини-приложение.

#### 1. Дашборд (Главная)
* Кольцевые диаграммы с онлайн/офлайн статусом и долей просроченных подписок.
* Круговые диаграммы утилизации серверов (Трафик, подключенные устройства по странам: SE vs RU).
* Зал ожидания (Waiting Room) — IP-адреса, подключенные к VPN, но не привязанные к TG-аккаунтам.

#### 2. Абоненты
* Карточный список всех клиентов с "умным" поиском.
* Быстрые фильтры: "Все", "Онлайн", "Истекают" (<3 дней), "Должники" (просрочено).
* Управление через **Карточку абонента**:
  * Изменение Alias.
  * Установка лимита скорости (10 / 30 / 50 / 100 Мбит/с, Максимум или Карцер/Punish).
  * Управление подпиской: +1/+3/+6 месяцев, дата рассчитывается автоматически.
  * Отвязка TG-аккаунта.
* **Массовая рассылка**: Асинхронная рассылка всем привязанным пользователям.
* **Экспорт**: CSV-выгрузка и скачивание бэкапа БД.

#### 3. Поддержка (Тикеты)
* Единая переписка с каждым пользователем, группировка в один чат.
* Разделение на "Открытые" и "Закрытые".
* Ответ клиенту (через Telegram от имени бота), закрытие и удаление тикетов.
* При новом ответе клиента закрытый тикет автоматически откроется снова.

#### 4. Журнал Аудита
* Логирование всех действий админа с метками времени.

#### 5. Мониторинг серверов
* CPU, RAM, Disk, Uptime серверов SE и RU.
* Ping до внешних сервисов.
* Перезапуск контейнеров и серверов (с cooldown 30 секунд).
* История нагрузки (графики).

#### 6. Настройки
* Пороги мониторинга (CPU, RAM).
* Параметры биллинга (дни предупреждения).

---

## 5. API Эндпоинты WebApp (FastAPI)

Все эндпоинты требуют заголовок `X-Telegram-Init-Data` с криптографической подписью.

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/api/users` | Список всех абонентов с VPN-статистикой |
| GET | `/api/user/{ip}` | Детали абонента по IP |
| POST | `/api/user/{ip}/speed` | Установка лимита скорости |
| POST | `/api/user/{ip}/rename` | Изменение Alias |
| POST | `/api/user/{ip}/extend` | Продление подписки |
| POST | `/api/user/{ip}/bind` | Привязка TG-аккаунта |
| POST | `/api/user/{ip}/unbind` | Отвязка TG-аккаунта |
| DELETE | `/api/user/{ip}/peer` | Удаление пира из WireGuard |
| GET | `/api/servers` | Метрики серверов SE и RU |
| GET | `/api/ping` | Пинг с RU-ноды |
| GET | `/api/server/{side}/history` | История нагрузки сервера |
| POST | `/api/server/{side}/{action}` | Действие с сервером (restart, reboot, ping) |
| GET | `/api/waiting_room` | Неприватные IP |
| GET | `/api/audit` | Журнал аудита |
| GET | `/api/tickets` | Список тикетов |
| POST | `/api/ticket/{id}/reply` | Ответ на тикет |
| POST | `/api/ticket/{id}/close` | Закрытие тикета |
| POST | `/api/ticket/{id}/delete` | Удаление тикета |
| POST | `/api/broadcast` | Массовая рассылка |
| GET | `/api/export/csv` | Экспорт CSV |
| GET | `/api/export/backup` | Скачивание бэкапа БД |
| GET | `/api/settings` | Получение настроек |
| POST | `/api/settings` | Сохранение настроек |

---

## 6. Защита и Безопасность

* **Авторизация WebApp:** Backend (FastAPI) криптографически проверяет подпись Telegram initData через токен бота. Зайти в WebApp из обычного браузера невозможно.
* **SQL-инъекции:** Whitelist полей (`ALLOWED_FIELDS`) в `update_user_field()`.
* **IP-валидация:** `validate_vpn_ip()` проверяет принадлежность к VPN-подсети.
* **Rate Limits:** Декораторы в боте + cooldown 30с на опасные действия (reboot/restart) в WebApp.
* **Бэкапы:** Автоматические ежедневные (cron 03:30, хранение 30 дней) + ручные через бот/WebApp.
* **Git:** Проект под контролем версий.

---

## 7. Деплой и обновление

### Установка зависимостей
```bash
cd /home/vpnuser/vpn_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Обновление кода
```bash
cd /home/vpnuser/vpn_bot
# Внести изменения в код
git add -A && git commit -m "описание изменений"
sudo systemctl restart vpn-bot.service vpn-webapp.service
```

### Просмотр логов после обновления
```bash
journalctl -u vpn-bot.service -n 50 -f
journalctl -u vpn-webapp.service -n 20 -f
```
