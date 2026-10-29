"""
config.py — Конфигурация и глобальный логгер.
Импортируй `from config import cfg, logger` в любом модуле.
"""

import logging
import os
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Загружаем .env: сначала из корня проекта, затем из домашней директории пользователя
env_path = os.getenv("ENV_PATH", os.path.join(BASE_DIR, ".env"))
if not os.path.exists(env_path):
    user_env = os.path.expanduser("~/.env")
    if os.path.exists(user_env):
        env_path = user_env

load_dotenv(env_path)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise ValueError(f"КРИТИЧЕСКАЯ ОШИБКА: переменная '{key}' не найдена в .env!")
    return val


@dataclass(frozen=True)
class Config:
    # --- Секреты ---
    token: str = field(default_factory=lambda: _require("BOT_TOKEN"))
    admin_id: int = field(default_factory=lambda: int(_require("ADMIN_ID")))
    admin_api_key: str = field(default_factory=lambda: os.getenv("ADMIN_API_KEY", ""))

    # --- Опциональные: второй (RU) сервер ---
    ru_host: str = field(default_factory=lambda: os.getenv("RU_HOST", ""))
    ru_ssh_port: str = field(default_factory=lambda: os.getenv("RU_SSH_PORT", "22"))
    ru_user: str = field(default_factory=lambda: os.getenv("RU_USER", "root"))

    # --- Опциональные: выходной (SE) сервер ---
    se_host: str = field(default_factory=lambda: os.getenv("SE_HOST", ""))
    se_ssh_port: str = field(default_factory=lambda: os.getenv("SE_SSH_PORT", "22"))
    se_user: str = field(default_factory=lambda: os.getenv("SE_USER", "root"))

    # --- ID закрытой группы (для @group_member_only) ---
    group_chat_id: int = field(default_factory=lambda: int(os.getenv("GROUP_CHAT_ID", "0")))

    # --- Пути к файлам (по умолчанию внутри data/ проекта) ---
    data_dir: str = DATA_DIR
    db_file: str = field(default_factory=lambda: os.getenv("DB_FILE", os.path.join(DATA_DIR, "vpn_bot.db")))
    metrics_file: str = field(
        default_factory=lambda: os.getenv("METRICS_FILE", os.path.join(DATA_DIR, "metrics.csv"))
    )
    pid_file: str = field(default_factory=lambda: os.getenv("PID_FILE", os.path.join(DATA_DIR, "vpn_bot.pid")))
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", os.path.join(DATA_DIR, "bot.log")))

    # --- Расписание ---
    backup_time: str = "03:00"
    billing_check_time: str = "12:00"

    # --- Пороги мониторинга ---
    anomaly_cpu_threshold: int = 90
    anomaly_ram_threshold: int = 85

    # --- VPN / Биллинг ---
    vpn_subnet: str = "10.0.0.0/8"
    users_per_page: int = 7
    allowed_speeds: frozenset = field(default_factory=lambda: frozenset({"10", "30", "50", "100", "max", "punish"}))

    # --- Биллинг ---
    billing_warn_days: int = 3  # За сколько дней предупреждать
    billing_critical_days: int = 1  # "Завтра последний день"

    # --- Реквизиты оплаты (показываются клиентам) ---
    payment_details: str = field(
        default_factory=lambda: os.getenv("PAYMENT_DETAILS", "Укажите реквизиты в .env (PAYMENT_DETAILS)")
    )
    payment_amount: str = field(
        default_factory=lambda: os.getenv("PAYMENT_AMOUNT", "Укажите сумму в .env (PAYMENT_AMOUNT)")
    )

    @property
    def vpn_containers(self) -> list[dict[str, Any]]:
        """Список VPN-контейнеров. Настраивается через .env."""
        container1 = os.getenv("VPN_CONTAINER_1", "amnezia-awg2")
        container2 = os.getenv("VPN_CONTAINER_2", "")
        container3 = os.getenv("VPN_CONTAINER_3", "")
        specs = [
            {
                "name": container1,
                "iface": "awg0",
                "alias": os.getenv("VPN_ALIAS_1", "🇸🇪 SE Server (Main)"),
                "host": "local",
                "port": None,
            },
            {
                "name": container2,
                "iface": "awg0",
                "alias": os.getenv("VPN_ALIAS_2", ""),
                "host": "local",
                "port": None,
            },
            {
                "name": container3,
                "iface": "awg0",
                "alias": os.getenv("VPN_ALIAS_3", "🇷🇺 RU Server (Node)"),
                "host": self.ru_host,
                "port": self.ru_ssh_port,
            },
        ]
        seen = set()
        result = []
        for spec in specs:
            key = (spec["host"], (spec["name"] or "").strip())
            if not key[1] or key in seen:
                continue
            seen.add(key)
            result.append(spec)
        return result


# Синглтон конфига
cfg = Config()


# ─── Глобальный логгер ────────────────────────────────────────────────────────


def _setup_logger() -> logging.Logger:
    log = logging.getLogger("vpn_bot")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

        fh = RotatingFileHandler(
            cfg.log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)

        ch = logging.StreamHandler()
        ch.setFormatter(fmt)

        log.addHandler(fh)
        log.addHandler(ch)
    return log


logger = _setup_logger()
