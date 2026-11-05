"""
db/system.py — Системные настройки, журнал аудита, история метрик серверов и зал ожидания.
"""
from datetime import datetime
import sqlite3
from typing import Any

from config import logger
from db.connection import get_conn


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
    """Записывает действие администратора или системы в журнал аудита."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, action, target_ip, details, admin_id) VALUES (?, ?, ?, ?, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, target_ip, details, admin_id),
            )
    except Exception as e:
        logger.error(f"Ошибка записи в audit_log: {e}")


def get_audit_log(limit: int = 15) -> list[sqlite3.Row]:
    """Возвращает последние записи журнала аудита."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT timestamp, action, target_ip, details FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


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


# ─── Зал ожидания (Waiting Room) ─────────────────────────────────────────────


def add_pending_tg_user(tg_user_id: int, first_name: str, username: str) -> None:
    """Добавляет пользователя в зал ожидания при нажатии /start."""
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
