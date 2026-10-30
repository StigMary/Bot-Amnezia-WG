"""
database.py — Вся работа с SQLite.
Инициализация таблиц, миграция схемы, CRUD-функции, аудит.
"""

import contextlib
import sqlite3
from datetime import datetime
from typing import Any

from config import cfg, logger

# ─── Контекстный менеджер соединения ─────────────────────────────────────────

_WAL_INITIALIZED = False


@contextlib.contextmanager
def get_conn():
    """Потокобезопасное соединение с автокоммитом / роллбэком.

    Включает WAL-режим (улучшает конкурентное чтение/запись) и
    busy_timeout (даёт другим коннектам подождать вместо «database is locked»).
    """
    global _WAL_INITIALIZED
    conn = sqlite3.connect(cfg.db_file, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        if not _WAL_INITIALIZED:
            with contextlib.suppress(Exception):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            _WAL_INITIALIZED = True
        # busy_timeout — на случай параллельных писем
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Инициализация и миграция ─────────────────────────────────────────────────


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

        # Таблица тикетов (удалена старая)

    _migrate()
    logger.info("БД инициализирована.")


def _migrate() -> None:
    """
    Безопасная миграция: добавляет новые колонки в существующую БД
    командой ALTER TABLE. Повторный вызов безопасен (ошибки игнорируются).
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
        
        # Создаем таблицу server_stats здесь, чтобы при накатывании старой БД она тоже создалась
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
        
        # Создаем таблицу тикетов, если ее еще нет
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

        # Таблица зала ожидания (пользователи Telegram, ожидающие привязки к VPN)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_tg_users (
                tg_user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username   TEXT,
                created_at TEXT
            )
        """)
        

        # Таблица для отметок об уже отправленных биллинг-уведомлениях
        conn.execute("""
            CREATE TABLE IF NOT EXISTS billing_notified (
                tg_user_id  INTEGER NOT NULL,
                notice_type TEXT    NOT NULL,
                date        TEXT    NOT NULL,
                PRIMARY KEY (tg_user_id, notice_type, date)
            )
        """)
        # Очистка старых записей (старше 48 часов) при каждой миграции или рестарте
        cutoff = int(datetime.now().timestamp()) - 48 * 3600
        conn.execute("DELETE FROM server_stats WHERE timestamp < ?", (cutoff,))

    logger.info("Миграция схемы БД выполнена.")

# ─── Тикеты (Поддержка) ──────────────────────────────────────────────────────

def create_ticket(tg_user_id: int, alias: str, text: str) -> int:
    import json
    with get_conn() as conn:
        # Check for ANY ticket from this user
        row = conn.execute("SELECT id, messages FROM tickets WHERE tg_user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (tg_user_id,)).fetchone()
        
        msg_obj = {"sender": "user", "text": text, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        
        if row:
            # Append to existing and REOPEN ticket
            try:
                msgs = json.loads(row["messages"])
            except Exception:
                msgs = [{"sender": "user", "text": row["messages"]}]
            msgs.append(msg_obj)
            conn.execute("UPDATE tickets SET messages = ?, status = 'open' WHERE id = ?", (json.dumps(msgs, ensure_ascii=False), row["id"]))
            return row["id"]
        else:
            # Create new
            msgs = [msg_obj]
            cursor = conn.execute(
                "INSERT INTO tickets (tg_user_id, alias, messages, created_at) VALUES (?, ?, ?, ?)",
                (tg_user_id, alias, json.dumps(msgs, ensure_ascii=False), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return cursor.lastrowid

def get_tickets(status: str = None) -> list[dict[str, Any]]:
    query = "SELECT t.*, u.ip_address as ip FROM tickets t LEFT JOIN users u ON t.tg_user_id = u.tg_user_id"
    params = []
    if status:
        query += " WHERE t.status = ?"
        params.append(status)
    query += " ORDER BY t.id DESC"
    
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]

def get_ticket(ticket_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT t.*, u.ip_address as ip FROM tickets t LEFT JOIN users u ON t.tg_user_id = u.tg_user_id WHERE t.id = ?", (ticket_id,)).fetchone()
    return dict(row) if row else None

def reply_ticket(ticket_id: int, reply_text: str) -> None:
    import json
    with get_conn() as conn:
        row = conn.execute("SELECT messages FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            return
            
        try:
            msgs = json.loads(row["messages"])
        except Exception:
            msgs = [{"sender": "user", "text": row["messages"]}]
            
        msgs.append({"sender": "admin", "text": reply_text, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        conn.execute("UPDATE tickets SET messages = ? WHERE id = ?", (json.dumps(msgs, ensure_ascii=False), ticket_id))

def close_ticket(ticket_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))

def delete_ticket(ticket_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))

# ─── Настройки (Settings) ────────────────────────────────────────────────────

def get_setting(key: str, default: Any = None) -> Any:
    """Возвращает настройку из БД, либо default если не найдено."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    return default

def set_setting(key: str, value: str) -> None:
    """Сохраняет или обновляет настройку в БД."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


# ─── Метрики серверов ────────────────────────────────────────────────────────

def log_server_stat(server_name: str, cpu: float, ram: float, rx_speed: float, tx_speed: float) -> None:
    """Записывает текущие метрики сервера в БД."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO server_stats (timestamp, server_name, cpu, ram, rx_speed, tx_speed) VALUES (?, ?, ?, ?, ?, ?)",
                (int(datetime.now().timestamp()), server_name, cpu, ram, rx_speed, tx_speed),
            )
    except Exception as e:
        logger.error(f"Ошибка записи в server_stats: {e}")

def get_server_history(server_name: str, hours: int = 24) -> list[dict[str, Any]]:
    """Возвращает историю метрик сервера за последние N часов."""
    cutoff = int(datetime.now().timestamp()) - hours * 3600
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT timestamp, cpu, ram, rx_speed, tx_speed FROM server_stats WHERE server_name = ? AND timestamp >= ? ORDER BY timestamp ASC",
            (server_name, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]

# ─── Аудит ───────────────────────────────────────────────────────────────────

def log_audit(
    action: str,
    target_ip: str | None = None,
    details: str | None = None,
    admin_id: int | None = None,
) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, action, target_ip, details, admin_id) VALUES (?, ?, ?, ?, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, target_ip, details, admin_id),
            )
    except Exception as e:
        logger.error(f"Ошибка записи в audit_log: {e}")


def get_audit_log(limit: int = 15) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT timestamp, action, target_ip, details FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ─── Пользователи: чтение ────────────────────────────────────────────────────


def get_all_users() -> dict[str, dict[str, Any]]:
    """Возвращает всех пользователей: {ip: {alias, speed_limit, protocol, tg_user_id, paid_until}}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ip_address, alias, speed_limit, protocol, tg_user_id, paid_until, client_alias FROM users"
        ).fetchall()
    return {
        r["ip_address"]: {
            "alias": r["alias"],
            "speed_limit": r["speed_limit"],
            "protocol": r["protocol"],
            "tg_user_id": r["tg_user_id"],
            "paid_until": r["paid_until"],
            "client_alias": r["client_alias"],
        }
        for r in rows
    }


def get_user_by_tg_id(tg_user_id: int) -> sqlite3.Row | None:
    """Ищет профиль пользователя по его Telegram ID."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)).fetchone()


def get_user_by_ip(ip: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE ip_address = ?", (ip,)).fetchone()

get_user = get_user_by_ip


def get_billing_users() -> list[sqlite3.Row]:
    """Все пользователи с привязанным TG ID и датой оплаты (для check_billing)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT ip_address, tg_user_id, paid_until, speed_limit, protocol "
            "FROM users WHERE tg_user_id IS NOT NULL AND paid_until IS NOT NULL"
        ).fetchall()


# ─── Пользователи: запись ────────────────────────────────────────────────────

ALLOWED_FIELDS = frozenset({"alias", "speed_limit", "tg_user_id", "paid_until", "protocol", "client_alias"})


def update_user_field(ip: str, field: str, value: Any) -> bool:
    """Обновляет поле пользователя (whitelist полей защищает от SQL-инъекции).

    Использует атомарный UPSERT (INSERT ... ON CONFLICT DO UPDATE),
    чтобы исключить race condition между UPDATE и INSERT.
    """
    if field not in ALLOWED_FIELDS:
        logger.error(f"Запрещённое поле для обновления: '{field}'")
        return False
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO users (ip_address, {field}) VALUES (?, ?) "  # nosec B608
            f"ON CONFLICT(ip_address) DO UPDATE SET {field} = excluded.{field}",
            (ip, value),
        )
    return True


def upsert_users_batch(pairs: list[tuple[str, str]]) -> None:
    """Массовое добавление пользователей (ip, protocol) с обновлением protocol если он пуст."""
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO users (ip_address, protocol) VALUES (?, ?) "
            "ON CONFLICT(ip_address) DO UPDATE SET protocol = excluded.protocol "
            "WHERE users.protocol IS NULL OR users.protocol = ''",
            pairs,
        )


def delete_user(ip: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE ip_address = ?", (ip,))


def bind_tg_to_ip(ip: str, tg_user_id: int) -> bool:
    """Привязывает Telegram ID к VPN-профилю."""
    return update_user_field(ip, "tg_user_id", tg_user_id)


def extend_paid_until(ip: str, days: int) -> str:
    """
    Прибавляет `days` дней к paid_until.
    Если paid_until в прошлом или NULL - считаем от сегодня.
    Возвращает новую дату в виде строки ISO.
    """
    from datetime import timedelta

    user = get_user_by_ip(ip)
    if user and user["paid_until"]:
        try:
            base = datetime.fromisoformat(user["paid_until"])
            # Если была бессрочная подписка, сбрасываем базу на сегодня
            if base.year >= 2099:
                base = datetime.now()

            new_date = base + timedelta(days=days) if base > datetime.now() else datetime.now() + timedelta(days=days)
        except ValueError:
            new_date = datetime.now() + timedelta(days=days)
    else:
        new_date = datetime.now() + timedelta(days=days)

    iso = new_date.strftime("%Y-%m-%dT%H:%M:%S")
    update_user_field(ip, "paid_until", iso)
    # Сбрасываем отметки уведомлений, чтобы будущие предупреждения работали
    user = get_user_by_ip(ip)
    if user and user["tg_user_id"]:
        clear_billing_notified(user["tg_user_id"])
    return iso


# ─── Многоустройственные операции (по tg_user_id) ────────────────────────────


def get_devices_by_tg_id(tg_user_id: int) -> list[sqlite3.Row]:
    """Возвращает все VPN-профили (устройства) одного аккаунта."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT ip_address, alias, speed_limit, protocol, paid_until, client_alias, prev_speed_limit FROM users WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchall()


def extend_paid_until_for_tg(tg_user_id: int, days: int) -> str:
    """
    Продлевает подписку ВСЕМ устройствам одного аккаунта (tg_user_id).
    Возвращает новую дату ISO.
    """
    from datetime import timedelta

    devices = get_devices_by_tg_id(tg_user_id)
    if not devices:
        raise ValueError(f"Нет устройств для tg_user_id={tg_user_id}")

    # Берём максимальную текущую дату среди всех устройств
    best_base = datetime.now()
    for d in devices:
        if d["paid_until"]:
            try:
                dt = datetime.fromisoformat(d["paid_until"])
                if dt > best_base:
                    best_base = dt
            except ValueError:
                pass

    # Если у клиента была бессрочная подписка (2099+ год)
    # и мы пытаемся добавить дни, значит мы отменяем бессрочность:
    # сбрасываем базу на "сейчас".
    if best_base.year >= 2099:
        best_base = datetime.now()

    new_date = best_base + timedelta(days=days) if best_base > datetime.now() else datetime.now() + timedelta(days=days)
    iso = new_date.strftime("%Y-%m-%dT%H:%M:%S")

    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET paid_until = ? WHERE tg_user_id = ?",
            (iso, tg_user_id),
        )
    # Сбрасываем отметки уведомлений, чтобы будущие предупреждения работали
    clear_billing_notified(tg_user_id)
    return iso


def get_billing_accounts() -> list[dict[str, Any]]:
    # Возвращает список уникальных аккаунтов для биллинга.
    # Группирует устройства по tg_user_id и берёт единую paid_until.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT tg_user_id, MIN(paid_until) as paid_until "
            "FROM users "
            "WHERE tg_user_id IS NOT NULL AND paid_until IS NOT NULL "
            "GROUP BY tg_user_id"
        ).fetchall()
    return [{"tg_user_id": r["tg_user_id"], "paid_until": r["paid_until"]} for r in rows]

# ─── Биллинг: отметки уведомлений ────────────────────────────────────────────


def was_notified_today(tg_user_id: int, notice_type: str) -> bool:
    """Возвращает True, если уведомление этого типа уже отправлялось сегодня."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM billing_notified WHERE tg_user_id=? AND notice_type=? AND date=?",
            (tg_user_id, notice_type, today),
        ).fetchone()
    return row is not None


def mark_notified(tg_user_id: int, notice_type: str) -> None:
    """Ставит отметку: сегодня уведомление этого типа уже было отправлено."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO billing_notified (tg_user_id, notice_type, date) VALUES (?, ?, ?)",
            (tg_user_id, notice_type, today),
        )


def clear_billing_notified(tg_user_id: int) -> None:
    """Сбрасывает все биллинг-отметки для аккаунта (вызывается при продлении)."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM billing_notified WHERE tg_user_id=?",
            (tg_user_id,),
        )




def save_user_config(ip_address: str, config_text: str) -> None:
    """Сохраняет текст выданного .conf файла для абонента."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET config_text = ? WHERE ip_address = ?", (config_text, ip_address))


def get_user_config(ip_address: str) -> str | None:
    """Возвращает текст выданного .conf файла абонента или None."""
    with get_conn() as conn:
        row = conn.execute("SELECT config_text FROM users WHERE ip_address = ?", (ip_address,)).fetchone()
        return row[0] if row and row[0] else None


def add_pending_tg_user(tg_user_id: int, first_name: str, username: str) -> None:
    """Добавляет пользователя в зал ожидания при нажатии /start."""
    from datetime import datetime
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO pending_tg_users (tg_user_id, first_name, username, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username,
                created_at = excluded.created_at
        """, (tg_user_id, first_name, username, now))


def get_pending_tg_users() -> list[dict]:
    """Возвращает список всех пользователей из зала ожидания."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT tg_user_id, first_name, username, created_at
            FROM pending_tg_users
            ORDER BY created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def remove_pending_tg_user(tg_user_id: int) -> None:
    """Удаляет пользователя из зала ожидания после успешной привязки."""
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_tg_users WHERE tg_user_id = ?", (tg_user_id,))
