"""
webapp/routers/waiting_room.py — Зал ожидания: нераспознанные пиры и заявки Telegram пользователей на регистрацию.
"""
from fastapi import APIRouter, Depends, HTTPException

from config import cfg
from database import (
    extend_paid_until,
    get_pending_tg_users,
    log_audit,
    remove_pending_tg_user,
    update_user_field,
)
from vpn_engine import get_vpn_stats, sanitize_alias, validate_vpn_ip
from webapp.auth import require_admin
from webapp.schemas import RegisterPayload

router = APIRouter(tags=["waiting_room"])


@router.get("/api/waiting_room")
async def waiting_room(admin=Depends(require_admin)):
    tg_pending = get_pending_tg_users()
    stats = get_vpn_stats()
    peer_pending = []
    for u in stats:
        if str(u.get("db_alias", "")) == "Новый пользователь":
            peer_pending.append({
                "ip": u["ip"],
                "server": u["server_alias"],
                "endpoint": u.get("endpoint", "(none)"),
                "handshake_ts": u.get("handshake_ts", 0),
                "total_bytes": u["total_bytes"],
                "downloaded": u["downloaded"],
                "uploaded": u["uploaded"],
                "online_emoji": u["online_emoji"]
            })
    return {
        "tg_pending": tg_pending,
        "peer_pending": peer_pending
    }


@router.get("/api/pending_tg_users")
async def api_get_pending_tg(admin=Depends(require_admin)):
    return get_pending_tg_users()


@router.post("/api/user/{ip}/register")
async def register_user(ip: str, payload: RegisterPayload, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    
    clean_alias = sanitize_alias(payload.alias)
    update_user_field(ip, "alias", clean_alias)
    
    if payload.tg_id and payload.tg_id.isdigit():
        update_user_field(ip, "tg_user_id", int(payload.tg_id))
        try:
            remove_pending_tg_user(int(payload.tg_id))
        except Exception:
            pass
        
    if payload.days > 0:
        extend_paid_until(ip, payload.days)
        
    log_audit("REGISTER", ip, f"alias={clean_alias},days={payload.days}", cfg.admin_id)
    return {"ok": True}
