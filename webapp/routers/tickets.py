"""
webapp/routers/tickets.py — Служба технической поддержки: просмотр обращений, ответы в Telegram, закрытие и удаление.
"""
from fastapi import APIRouter, Depends, HTTPException
import telebot

from config import cfg, logger
from database import (
    close_ticket,
    delete_ticket,
    get_ticket,
    get_tickets,
    log_audit,
    reply_ticket,
)
from webapp.auth import require_admin
from webapp.schemas import TicketReplyPayload

router = APIRouter(tags=["tickets"])


@router.get("/api/tickets")
async def api_get_tickets(status: str = None, admin=Depends(require_admin)):
    return get_tickets(status)


@router.post("/api/ticket/{ticket_id}/reply")
async def api_reply_ticket(ticket_id: int, payload: TicketReplyPayload, admin=Depends(require_admin)):
    ticket = get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
        
    try:
        bot = telebot.TeleBot(cfg.token)
        if ticket["tg_user_id"]:
            bot.send_message(
                ticket["tg_user_id"],
                f"📩 <b>Ответ службы поддержки:</b>\n\n{payload.text}",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Failed to send ticket reply: {e}")
        
    reply_ticket(ticket_id, payload.text)
    log_audit("TICKET_REPLY", None, f"ticket_id={ticket_id}", cfg.admin_id)
    return {"ok": True}


@router.post("/api/ticket/{ticket_id}/close")
async def api_close_ticket(ticket_id: int, admin=Depends(require_admin)):
    ticket = get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
        
    try:
        bot = telebot.TeleBot(cfg.token)
        if ticket["tg_user_id"]:
            bot.send_message(
                ticket["tg_user_id"],
                "🎟️ Ваш тикет был закрыт администратором. Спасибо за обращение!",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Failed to notify ticket close: {e}")
        
    close_ticket(ticket_id)
    log_audit("TICKET_CLOSE", None, f"ticket_id={ticket_id}", cfg.admin_id)
    return {"ok": True}


@router.post("/api/ticket/{ticket_id}/delete")
async def api_delete_ticket(ticket_id: int, admin=Depends(require_admin)):
    delete_ticket(ticket_id)
    log_audit("TICKET_DELETE", None, f"ticket_id={ticket_id}", cfg.admin_id)
    return {"ok": True}
