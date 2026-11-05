"""
db/tickets.py — Репозиторий службы поддержки: создание тикетов, история сообщений, ответы и статусы.
"""
from datetime import datetime
import json
from typing import Any

from db.connection import get_conn


def create_ticket(tg_user_id: int, alias: str, text: str) -> int:
    """Создаёт новый тикет или дописывает сообщение в уже открытый тикет пользователя."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, messages FROM tickets WHERE tg_user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (tg_user_id,)
        ).fetchone()

        msg_obj = {"sender": "user", "text": text, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

        if row:
            try:
                msgs = json.loads(row["messages"])
            except Exception:
                msgs = [{"sender": "user", "text": row["messages"]}]
            msgs.append(msg_obj)
            conn.execute(
                "UPDATE tickets SET messages = ?, status = 'open' WHERE id = ?",
                (json.dumps(msgs, ensure_ascii=False), row["id"])
            )
            return row["id"]
        else:
            msgs = [msg_obj]
            cursor = conn.execute(
                "INSERT INTO tickets (tg_user_id, alias, messages, created_at) VALUES (?, ?, ?, ?)",
                (tg_user_id, alias, json.dumps(msgs, ensure_ascii=False), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return cursor.lastrowid


def get_tickets(status: str = None) -> list[dict[str, Any]]:
    """Возвращает список тикетов с привязкой IP пользователя (с опциональным фильтром по статусу)."""
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
    """Возвращает детальную карточку тикета по ID."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT t.*, u.ip_address as ip FROM tickets t LEFT JOIN users u ON t.tg_user_id = u.tg_user_id WHERE t.id = ?",
            (ticket_id,)
        ).fetchone()
    return dict(row) if row else None


def reply_ticket(ticket_id: int, reply_text: str) -> None:
    """Добавляет ответ администратора в историю переписки тикета."""
    with get_conn() as conn:
        row = conn.execute("SELECT messages FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            return

        try:
            msgs = json.loads(row["messages"])
        except Exception:
            msgs = [{"sender": "user", "text": row["messages"]}]

        msgs.append({
            "sender": "admin",
            "text": reply_text,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        conn.execute(
            "UPDATE tickets SET messages = ? WHERE id = ?",
            (json.dumps(msgs, ensure_ascii=False), ticket_id)
        )


def close_ticket(ticket_id: int) -> None:
    """Переводит тикет в статус 'closed'."""
    with get_conn() as conn:
        conn.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))


def delete_ticket(ticket_id: int) -> None:
    """Удаляет тикет из базы данных."""
    with get_conn() as conn:
        conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
