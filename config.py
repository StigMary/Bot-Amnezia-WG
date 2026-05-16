"""
config.py — Конфигурация и глобальный логгер.
Импортируй `from config import cfg, logger` в любом модуле.
"""
import os
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from typing import List, Dict, Any
from dotenv import load_dotenv

# Ищем .env: сначала рядом со скриптом, потом стандартный путь продакшна
_env_candidates = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    "/home/vpnuser/.env",
]
for _env_path in _env_candidates:
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
        break
else:
    load_dotenv()  # fallback: поискать в CWD


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

    # --- Опциональные: второй (RF) сервер ---
    rf_host: str = field(default_factory=lambda: os.getenv("RF_HOST", ""))
    rf_ssh_port: str = field(default_factory=lambda: os.getenv("RF_SSH_PORT", "22"))

    # --- ID закрытой группы (для @group_member_only) ---
    group_chat_id: int = field(
        default_factory=lambda: int(os.getenv("GROUP_CHAT_ID", "0"))
    )

    # --- Пути к файлам (можно переопределить через .env) ---
    # Дефолт: ./data/ рядом со скриптом (работает локально и на сервере)
    db_file: str = field(default_factory=lambda: os.getenv(
        "DB_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vpn_bot.db")
    ))
    metrics_file: str = field(default_factory=lambda: os.getenv(
        "METRICS_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "metrics.csv")
    ))
    pid_file: str = field(default_factory=lambda: os.getenv(
        "PID_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vpn_bot.pid")
    ))
    log_file: str = field(default_factory=lambda: os.getenv(
        "LOG_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot.log")
    ))

    # --- Расписание ---
    backup_time: str = "03:00"
    billing_check_time: str = "12:00"

    # --- Пороги мониторинга ---
    anomaly_cpu_threshold: int = 90
    anomaly_ram_threshold: int = 85

    # --- VPN / Биллинг ---
    vpn_subnet: str = "10.0.0.0/8"
    users_per_page: int = 7
    allowed_speeds: frozenset = field(
        default_factory=lambda: frozenset({"10", "30", "50", "100", "max", "punish"})
    )

    # --- Биллинг ---
    billing_warn_days: int = 3      # За сколько дней предупреждать
    billing_critical_days: int = 1  # "Завтра последний день"

    # --- Реквизиты оплаты (показываются клиентам) ---
    payment_details: str = field(
        default_factory=lambda: os.getenv("PAYMENT_DETAILS", "Укажите реквизиты в .env (PAYMENT_DETAILS)")
    )
    payment_amount: str = field(
        default_factory=lambda: os.getenv("PAYMENT_AMOUNT", "Укажите сумму в .env (PAYMENT_AMOUNT)")
    )

    @property
    def vpn_containers(self) -> List[Dict[str, Any]]:
        """Список VPN-контейнеров. Настраивается через .env."""
        container1 = os.getenv("VPN_CONTAINER_1", "amnezia-awg2")
        container2 = os.getenv("VPN_CONTAINER_2", "amnezia-awg")
        container3 = os.getenv("VPN_CONTAINER_3", "amnezia-awg2")
        return [
            {"name": container1, "iface": "awg0", "alias": os.getenv("VPN_ALIAS_1", "Server 1"),  "host": "local", "port": None},
            {"name": container2, "iface": "awg0", "alias": os.getenv("VPN_ALIAS_2", "Server 2"),  "host": "local", "port": None},
            {"name": container3, "iface": "awg0", "alias": os.getenv("VPN_ALIAS_3", "Remote"),    "host": self.rf_host, "port": self.rf_ssh_port},
        ]


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
