# Changelog

Все значимые изменения в этом проекте документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
проект придерживается [Semantic Versioning](https://semver.org/lang/ru/).

## [Unreleased]

### Planned
- CI/CD pipeline (GitHub Actions: lint + smoke tests + auto-deploy).
- Автоматический бэкап БД на отдельный сервер (offsite).
- Метрики Prometheus + Grafana дашборд.
- Health-check HTTP endpoint для внешнего мониторинга.

---

## [3.1.0] — 2026-05-14

### Added
- **venv-окружение** — миграция с системного Python на `/home/vpnuser/vpn_bot/.venv` с пиннингом версий зависимостей в [`requirements.txt`](requirements.txt).
- **Systemd hardening** ([`deploy/systemd/hardening.conf`](deploy/systemd/hardening.conf:1)):
  - лимиты памяти (`MemoryMax=200M`, `MemoryHigh=150M`);
  - sandboxing (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`, `RestrictSUIDSGID`, `LockPersonality`, `PrivateTmp`);
  - анти-краш-цикл (`StartLimitBurst=5`, `StartLimitIntervalSec=300`).
- **Telegram-алерты при падении сервиса** ([`alert@.service`](deploy/systemd/alert@.service:1) + [`systemd-telegram-notify.sh`](deploy/systemd/systemd-telegram-notify.sh:1)) — автоматическое уведомление администратора через `OnFailure=alert@%n.service`.
- Документация: [`deploy/systemd/README.md`](deploy/systemd/README.md:1) с пошаговой инструкцией установки.
- Лицензия: [`LICENSE`](LICENSE:1) (GNU AGPL-3.0).

### Changed
- [`vpn-bot.service`](vpn-bot.service:1): `ExecStart` теперь использует Python из venv (`.venv/bin/python`).

### Fixed
- **`handlers/admin.py`** — устранён `400 Bad Request: can't parse entities` в команде `/journal`:
  - добавлена функция `_md_escape()` для экранирования спецсимволов MarkdownV2;
  - добавлен `try/except` fallback на plain-text при ошибке парсинга разметки.

### Security
- Бот работает с непривилегированным пользователем `vpnuser`, доступ к ФС ограничен `ReadWritePaths`.
- Заблокирован публичный доступ к UDP/443 на хосте (только локальный loopback) — на стороне инфраструктуры.

---

## [3.0.0] — 2025-XX-XX

### Added
- Базовая функциональность Telegram-бота для управления AmneziaWG / WireGuard.
- Админ-панель: создание/удаление клиентов, выдача QR-кодов и `.conf`-файлов.
- Журналирование действий, мониторинг ресурсов (psutil), графики (matplotlib).
- Планировщик задач (schedule), бэкап БД.

---

[Unreleased]: https://github.com/StigMary/Bot-Amnezia-WG/compare/v3.1.0...HEAD
[3.1.0]: https://github.com/StigMary/Bot-Amnezia-WG/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/StigMary/Bot-Amnezia-WG/releases/tag/v3.0.0
