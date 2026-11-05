"""
db/users.py — Репозиторий пользователей: CRUD операции, привязка устройств, продление подписок и конфиги.
"""
from datetime import datetime, timedelta
import sqlite3
from typing import Any

from config import logger
from db.connection import get_conn

ALLOWED_FIELDS = frozenset({"alias", "speed_limit", "tg_user_id", "paid_until", "protocol", "client_alias"})


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
    """Возвращает запись пользователя по IP адресу."""
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


def update_user_field(ip: str, field: str, value: Any) -> bool:
    """Обновляет поле пользователя с защитой whitelist от SQL-инъекций.

    Использует атомарный UPSERT (INSERT ... ON CONFLICT DO UPDATE).
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
    """Удаляет профиль абонента по IP."""
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE ip_address = ?", (ip,))


def bind_tg_to_ip(ip: str, tg_user_id: int) -> bool:
    """Привязывает Telegram ID к VPN-профилю."""
    return update_user_field(ip, "tg_user_id", tg_user_id)


def extend_paid_until(ip: str, days: int) -> str:
    """Прибавляет days дней к дате paid_until абонента.

    Если подписка просрочена или отсутствует, отсчёт идёт от текущего момента.
    """
    from db.system import clear_billing_notified

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

    # Сбрасываем отметки уведомлений, чтобы предупреждения сработали в срок
    if user and user["tg_user_id"]:
        clear_billing_notified(user["tg_user_id"])
    return iso


def get_devices_by_tg_id(tg_user_id: int) -> list[sqlite3.Row]:
    """Возвращает все VPN-профили (устройства) одного аккаунта."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT ip_address, alias, speed_limit, protocol, paid_until, client_alias, prev_speed_limit "
            "FROM users WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchall()


def extend_paid_until_for_tg(tg_user_id: int, days: int) -> str:
    """Продлевает подписку ВСЕМ устройствам одного аккаунта (tg_user_id)."""
    from db.system import clear_billing_notified

    devices = get_devices_by_tg_id(tg_user_id)
    if not devices:
        raise ValueError(f"Нет устройств для tg_user_id={tg_user_id}")

    best_base = datetime.now()
    for d in devices:
        if d["paid_until"]:
            try:
                dt = datetime.fromisoformat(d["paid_until"])
                if dt > best_base:
                    best_base = dt
            except ValueError:
                pass

    if best_base.year >= 2099:
        best_base = datetime.now()

    new_date = best_base + timedelta(days=days) if best_base > datetime.now() else datetime.now() + timedelta(days=days)
    iso = new_date.strftime("%Y-%m-%dT%H:%M:%S")

    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET paid_until = ? WHERE tg_user_id = ?",
            (iso, tg_user_id),
        )

    clear_billing_notified(tg_user_id)
    return iso


def get_billing_accounts() -> list[dict[str, Any]]:
    """Возвращает список уникальных аккаунтов для биллинга (сгруппированных по tg_user_id)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT tg_user_id, MIN(paid_until) as paid_until "
            "FROM users "
            "WHERE tg_user_id IS NOT NULL AND paid_until IS NOT NULL "
            "GROUP BY tg_user_id"
        ).fetchall()
    return [{"tg_user_id": r["tg_user_id"], "paid_until": r["paid_until"]} for r in rows]


def save_user_config(ip_address: str, config_text: str) -> None:
    """Сохраняет текст выданного .conf файла для абонента."""
    with get_conn() as conn:
        conn.execute("UPDATE users SET config_text = ? WHERE ip_address = ?", (config_text, ip_address))


def get_user_config(ip_address: str) -> str | None:
    """Возвращает текст выданного .conf файла абонента или None."""
    with get_conn() as conn:
        row = conn.execute("SELECT config_text FROM users WHERE ip_address = ?", (ip_address,)).fetchone()
        return row[0] if row and row[0] else None
