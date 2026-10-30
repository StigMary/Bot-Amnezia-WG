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

    # --- Проект и бренд ---
    project_name: str = field(
        default_factory=lambda: os.getenv("PROJECT_NAME", "KznNET")
    )
    _payment_bank: str = field(
        default_factory=lambda: os.getenv("PAYMENT_BANK", "Т-Банк")
    )

    # --- Реквизиты оплаты (показываются клиентам) ---
    _payment_details: str = field(
        default_factory=lambda: os.getenv("PAYMENT_DETAILS", "Укажите реквизиты в .env (PAYMENT_DETAILS)")
    )
    _payment_amount: str = field(
        default_factory=lambda: os.getenv("PAYMENT_AMOUNT", "Укажите сумму в .env (PAYMENT_AMOUNT)")
    )

    @property
    def payment_bank(self) -> str:
        try:
            from database import get_setting
            val = get_setting("payment_bank")
            if val:
                return val
        except Exception:
            pass
        return self._payment_bank

    @property
    def payment_details(self) -> str:
        try:
            from database import get_setting
            val = get_setting("payment_details")
            if val:
                return val
        except Exception:
            pass
        return self._payment_details

    @property
    def payment_amount(self) -> str:
        try:
            from database import get_setting
            val = get_setting("payment_amount")
            if val:
                return val
        except Exception:
            pass
        return self._payment_amount

    @property
    def admin_ids(self) -> list[int]:
        """Список всех ID администраторов из ADMIN_IDS или ADMIN_ID."""
        raw = os.getenv("ADMIN_IDS", "").strip()
        if raw:
            ids = []
            for part in raw.split(","):
                part = part.strip()
                if part.isdigit():
                    ids.append(int(part))
            if ids:
                return ids
        return [self.admin_id] if self.admin_id else []

    @property
    def vpn_containers(self) -> list[dict[str, Any]]:
        """Список всех подключенных VPN-нод/серверов.
        
        Поддерживает:
        1. Декларативные ноды: NODE_1="ИМЯ|local(или IP)|PORT|USER|CONTAINER|IFACE"
        2. Либо переменные: NODE_1_NAME, NODE_1_HOST, NODE_1_PORT, ...
        3. Легаси-переменные: VPN_CONTAINER_1..3, RU_HOST, SE_HOST (полная обратная совместимость).
        """
        nodes = []
        seen = set()

        # 1. Поиск динамических нод NODE_1, NODE_2, ... до 20
        for i in range(1, 21):
            prefix = f"NODE_{i}"
            val = os.getenv(prefix)
            name_val = os.getenv(f"{prefix}_NAME")
            if not val and not name_val:
                continue

            if val and "|" in val:
                parts = [p.strip() for p in val.split("|")]
                alias = parts[0] if len(parts) > 0 and parts[0] else f"Node {i}"
                host = parts[1] if len(parts) > 1 and parts[1] else "local"
                port = parts[2] if len(parts) > 2 and parts[2] not in ("-", "", "none", "local") else "22"
                user = parts[3] if len(parts) > 3 and parts[3] not in ("-", "", "none") else "root"
                container = parts[4] if len(parts) > 4 and parts[4] else "amnezia-awg2"
                iface = parts[5] if len(parts) > 5 and parts[5] else "awg0"
            else:
                alias = os.getenv(f"{prefix}_NAME", val or f"Node {i}")
                host = os.getenv(f"{prefix}_HOST", "local")
                port = os.getenv(f"{prefix}_PORT", "22")
                user = os.getenv(f"{prefix}_USER", "root")
                container = os.getenv(f"{prefix}_CONTAINER", "amnezia-awg2")
                iface = os.getenv(f"{prefix}_IFACE", "awg0")

            if host == "local":
                port = None

            key = (host, container)
            if key in seen:
                continue
            seen.add(key)

            nodes.append({
                "id": f"node_{i}",
                "name": container,
                "container": container,
                "iface": iface,
                "alias": alias,
                "host": host,
                "port": port,
                "user": user,
            })

        if nodes:
            return nodes

        # 2. Обратная совместимость с легаси .env
        container1 = os.getenv("VPN_CONTAINER_1", "amnezia-awg2")
        container2 = os.getenv("VPN_CONTAINER_2", "")
        container3 = os.getenv("VPN_CONTAINER_3", "")
        specs = [
            {
                "id": "node_1",
                "name": container1,
                "container": container1,
                "iface": "awg0",
                "alias": os.getenv("VPN_ALIAS_1", "🇸🇪 SE Server (Main)"),
                "host": "local",
                "port": None,
                "user": self.se_user or "root",
            },
            {
                "id": "node_2",
                "name": container2,
                "container": container2,
                "iface": "awg0",
                "alias": os.getenv("VPN_ALIAS_2", ""),
                "host": "local",
                "port": None,
                "user": "root",
            },
            {
                "id": "node_3",
                "name": container3,
                "container": container3,
                "iface": "awg0",
                "alias": os.getenv("VPN_ALIAS_3", "🇷🇺 RU Server (Node)"),
                "host": self.ru_host,
                "port": self.ru_ssh_port,
                "user": self.ru_user or "root",
            },
        ]
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
