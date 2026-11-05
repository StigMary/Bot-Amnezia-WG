"""
db/schema.py — Инициализация таблиц базы данных и безопасная автоматическая миграция схемы.
"""
import contextlib
from datetime import datetime

from config import logger
from db.connection import get_conn


def init_db() -> None:
    """Создаёт таблицы и выполняет безопасную миграцию схемы."""
    with get_conn() as conn:
        # Таблица пользователей
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                ip_address   TEXT PRIMARY KEY,
                alias        TEXT    DEFAULT 'Новый пользователь',
                protocol     TEXT,
                speed_limit  TEXT    DEFAULT 'max',
                tg_user_id   INTEGER DEFAULT NULL,
                paid_until   TEXT    DEFAULT NULL,
                client_alias TEXT    DEFAULT NULL
            )
        """)

        # Таблица аудита
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  TEXT    NOT NULL,
                action     TEXT    NOT NULL,
                target_ip  TEXT,
                details    TEXT,
                admin_id   INTEGER
            )
        """)

        # Версия схемы
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4)")

    _migrate()
    logger.info("БД инициализирована.")


def _migrate() -> None:
    """Безопасная миграция: добавляет новые колонки и служебные таблицы.

    Повторный вызов безопасен (существующие колонки пропускаются).
    """
    migrations = [
        "ALTER TABLE users ADD COLUMN tg_user_id INTEGER DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN paid_until  TEXT    DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN client_alias TEXT   DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN config_text  TEXT   DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN prev_speed_limit TEXT DEFAULT NULL",
    ]
    with get_conn() as conn:
        for sql in migrations:
            with contextlib.suppress(Exception):  # колонка уже существует — OK
                conn.execute(sql)
                
        # Создаем таблицу settings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        # Создаем таблицу server_stats
        conn.execute("""
            CREATE TABLE IF NOT EXISTS server_stats (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    INTEGER NOT NULL,
                server_name  TEXT NOT NULL,
                cpu          REAL,
                ram          REAL,
                rx_speed     REAL,
                tx_speed     REAL
            )
        """)
        
        # Создаем таблицу тикетов
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER,
                alias      TEXT,
                messages   TEXT,
                status     TEXT DEFAULT 'open',
                created_at TEXT
            )
        """)

        # Таблица зала ожидания
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_tg_users (
                tg_user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username   TEXT,
                created_at TEXT
            )
        """)

        # Таблица для отметок об отправленных биллинг-уведомлениях
        conn.execute("""
            CREATE TABLE IF NOT EXISTS billing_notified (
                tg_user_id  INTEGER NOT NULL,
                notice_type TEXT    NOT NULL,
                date        TEXT    NOT NULL,
                PRIMARY KEY (tg_user_id, notice_type, date)
            )
        """)

        # Очистка старых записей метрик (старше 48 часов)
        cutoff = int(datetime.now().timestamp()) - 48 * 3600
        conn.execute("DELETE FROM server_stats WHERE timestamp < ?", (cutoff,))

    logger.info("Миграция схемы БД выполнена.")
