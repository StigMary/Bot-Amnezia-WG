"""
webapp/routers/broadcast.py — Массовая рассылка сообщений пользователям бота через Telegram.
"""
import asyncio
import threading
from fastapi import APIRouter, Depends
import telebot

from config import cfg, logger
from database import get_all_users, log_audit
from webapp.auth import require_admin
from webapp.schemas import BroadcastPayload

router = APIRouter(tags=["broadcast"])


@router.post("/api/broadcast")
async def broadcast_message(payload: BroadcastPayload, admin=Depends(require_admin)):
    users = get_all_users()
    tg_ids = set()
    for u in users.values():
        if u.get("tg_user_id"):
            tg_ids.add(u["tg_user_id"])
            
    def _run_broadcast(recipients: set, text: str):
        bot = telebot.TeleBot(cfg.token)
        sent = 0
        for tid in recipients:
            try:
                bot.send_message(tid, text)
                sent += 1
                import time
                time.sleep(0.05)
            except Exception as e:
                logger.warning(f"Broadcast failed to {tid}: {e}")
                
    threading.Thread(target=_run_broadcast, args=(tg_ids, payload.text), daemon=True).start()
    log_audit("BROADCAST", None, f"recipients={len(tg_ids)}", cfg.admin_id)
    return {"ok": True, "msg": f"Рассылка запущена для {len(tg_ids)} пользователей"}
