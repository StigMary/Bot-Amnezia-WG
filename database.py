"""
database.py — Вся работа с SQLite.
Инициализация таблиц, миграция схемы, CRUD-функции, аудит.
"""
import os
import sqlite3
import contextlib
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

from config import cfg, logger


# ─── Контекстный менеджер соединения ─────────────────────────────────────────

@contextlib.contextmanager
def get_conn():
    """Потокобезопасное соединение с автокоммитом / роллбэком."""
    conn = sqlite3.connect(cfg.db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
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
    # Автосоздание директории для БД (например, data/)
    db_dir = os.path.dirname(cfg.db_file)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

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
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (3)")

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
    ]
    with get_conn() as conn:
        for sql in migrations:
            with contextlib.suppress(Exception):   # колонка уже существует — OK
                conn.execute(sql)
    logger.info("Миграция схемы БД выполнена.")


# ─── Аудит ───────────────────────────────────────────────────────────────────

def log_audit(
    action: str,
    target_ip: Optional[str] = None,
    details: Optional[str] = None,
    admin_id: Optional[int] = None,
) -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, action, target_ip, details, admin_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, target_ip, details, admin_id),
            )
    except Exception as e:
        logger.error(f"Ошибка записи в audit_log: {e}")


def get_audit_log(limit: int = 15) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT timestamp, action, target_ip, details FROM audit_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ─── Пользователи: чтение ────────────────────────────────────────────────────

def get_all_users() -> Dict[str, Dict[str, Any]]:
    """Возвращает всех пользователей: {ip: {alias, speed_limit, protocol, tg_user_id, paid_until}}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ip_address, alias, speed_limit, protocol, tg_user_id, paid_until, client_alias FROM users"
        ).fetchall()
    return {
        r["ip_address"]: {
            "alias":       r["alias"],
            "speed_limit": r["speed_limit"],
            "protocol":    r["protocol"],
            "tg_user_id":  r["tg_user_id"],
            "paid_until":  r["paid_until"],
            "client_alias":r["client_alias"],
        }
        for r in rows
    }


def get_user_by_tg_id(tg_user_id: int) -> Optional[sqlite3.Row]:
    """Ищет профиль пользователя по его Telegram ID."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE tg_user_id = ?", (tg_user_id,)
        ).fetchone()


def get_user_by_ip(ip: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE ip_address = ?", (ip,)
        ).fetchone()


def get_billing_users() -> List[sqlite3.Row]:
    """Все пользователи с привязанным TG ID и датой оплаты (для check_billing)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT ip_address, tg_user_id, paid_until, speed_limit, protocol "
            "FROM users WHERE tg_user_id IS NOT NULL AND paid_until IS NOT NULL"
        ).fetchall()


# ─── Пользователи: запись ────────────────────────────────────────────────────

ALLOWED_FIELDS = frozenset({"alias", "speed_limit", "tg_user_id", "paid_until", "protocol", "client_alias"})


def update_user_field(ip: str, field: str, value: Any) -> bool:
    """Обновляет поле пользователя (whitelist полей защищает от SQL-инъекции)."""
    if field not in ALLOWED_FIELDS:
        logger.error(f"Запрещённое поле для обновления: '{field}'")
        return False
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE users SET {field} = ? WHERE ip_address = ?", (value, ip)
        )
        if cur.rowcount == 0:
            conn.execute(
                f"INSERT INTO users (ip_address, {field}) VALUES (?, ?)", (ip, value)
            )
    return True


def upsert_users_batch(pairs: List[Tuple[str, str]]) -> None:
    """Массовое добавление пользователей (ip, protocol) без затирания существующих."""
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO users (ip_address, protocol) VALUES (?, ?)", pairs
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
    Если paid_until в прошлом или NULL — считаем от сегодня.
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
                
            new_date = base + timedelta(days=days) if base > datetime.now() \
                       else datetime.now() + timedelta(days=days)
        except ValueError:
            new_date = datetime.now() + timedelta(days=days)
    else:
        new_date = datetime.now() + timedelta(days=days)

    iso = new_date.strftime("%Y-%m-%dT%H:%M:%S")
    update_user_field(ip, "paid_until", iso)
    return iso


# ─── Многоустройственные операции (по tg_user_id) ────────────────────────────

def get_devices_by_tg_id(tg_user_id: int) -> List[sqlite3.Row]:
    """Возвращает все VPN-профили (устройства) одного аккаунта."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT ip_address, alias, speed_limit, protocol, paid_until, client_alias "
            "FROM users WHERE tg_user_id = ?",
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

    new_date = best_base + timedelta(days=days) if best_base > datetime.now() \
               else datetime.now() + timedelta(days=days)
    iso = new_date.strftime("%Y-%m-%dT%H:%M:%S")

    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET paid_until = ? WHERE tg_user_id = ?",
            (iso, tg_user_id),
        )
    return iso


def get_billing_accounts() -> List[Dict[str, Any]]:
    """
    Возвращает список уникальных аккаунтов для биллинга.
    Группирует устройства по tg_user_id и берёт единую paid_until.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT tg_user_id, MIN(paid_until) as paid_until "
            "FROM users "
            "WHERE tg_user_id IS NOT NULL AND paid_until IS NOT NULL "
            "GROUP BY tg_user_id"
        ).fetchall()
    return [{"tg_user_id": r["tg_user_id"], "paid_until": r["paid_until"]} for r in rows]
