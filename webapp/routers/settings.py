"""
webapp/routers/settings.py — Системные настройки бота, аудит действий, экспорт CSV и резервных копий БД.
"""
import time

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse

from config import cfg
from database import (
    get_all_users,
    get_audit_log,
    get_setting,
    log_audit,
    set_setting,
)
from vpn_engine import get_vpn_stats
from webapp.auth import require_admin

router = APIRouter(tags=["settings"])


@router.get("/api/settings")
async def get_settings_api(admin=Depends(require_admin)):
    return {
        "anomaly_cpu_threshold": get_setting("anomaly_cpu_threshold", str(cfg.anomaly_cpu_threshold)),
        "anomaly_ram_threshold": get_setting("anomaly_ram_threshold", str(cfg.anomaly_ram_threshold)),
        "billing_warn_days": get_setting("billing_warn_days", str(cfg.billing_warn_days)),
        "billing_critical_days": get_setting("billing_critical_days", str(cfg.billing_critical_days)),
        "payment_bank": get_setting("payment_bank", cfg.payment_bank or "Т-Банк"),
        "payment_details": get_setting("payment_details", cfg.payment_details or ""),
        "payment_amount": get_setting("payment_amount", cfg.payment_amount or "150 руб./мес."),
    }


@router.post("/api/settings")
async def save_settings_api(request: Request, admin=Depends(require_admin)):
    data = await request.json()
    for k, v in data.items():
        set_setting(k, str(v))
    return {"ok": True, "msg": "Настройки сохранены"}


@router.get("/api/export/csv")
async def export_csv(admin=Depends(require_admin)):
    stats = get_vpn_stats()
    users = get_all_users()
    
    lines = ["IP;Alias;Server;TG_ID;Paid_Until;Downloaded;Uploaded"]
    for s in stats:
        ip = s["ip"]
        db_u = users.get(ip, {})
        alias = db_u.get("alias", "")
        server = s.get("server_alias", "")
        tg_id = db_u.get("tg_user_id", "")
        paid = db_u.get("paid_until", "")
        down = s.get("downloaded", "0")
        up = s.get("uploaded", "0")
        lines.append(f"{ip};{alias};{server};{tg_id};{paid};{down};{up}")
        
    csv_content = "\n".join(lines)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"}
    )


@router.get("/api/export/backup")
async def export_backup(admin=Depends(require_admin)):
    log_audit("BACKUP", None, "database.db downloaded", cfg.admin_id)
    return FileResponse(cfg.db_file, media_type="application/octet-stream", filename=f"database_{int(time.time())}.db")


@router.get("/api/audit")
async def audit_log(flt: str = "all", admin=Depends(require_admin)):
    rows = get_audit_log(60)
    _icons = {
        "SET_SPEED": "⚡", "EXTEND": "📅", "DELETE_PEER": "🗑",
        "BIND": "🔗", "UNBIND": "🔓", "RENAME": "✏️",
        "BROADCAST": "📢", "BACKUP": "💾", "BOT_START": "🚀",
        "BOT_SHUTDOWN": "🛑", "AUTO_PUNISH": "🚨",
    }
    _names = {
        "SET_SPEED": "Скорость", "EXTEND": "Продление",
        "DELETE_PEER": "Удаление", "BIND": "Привязка TG",
        "UNBIND": "Отвязка TG", "RENAME": "Переим.",
        "BROADCAST": "Рассылка", "BACKUP": "Бэкап",
        "BOT_START": "Запуск", "BOT_SHUTDOWN": "Стоп",
        "AUTO_PUNISH": "Авто-карцер",
    }
    if flt != "all":
        rows = [r for r in rows if r["action"] == flt]
    result = []
    for r in rows[:40]:
        try:
            target_ip = r["target_ip"] if r["target_ip"] is not None else ""
        except (IndexError, KeyError):
            target_ip = ""
        try:
            details = r["details"] if r["details"] is not None else ""
        except (IndexError, KeyError):
            details = ""
            
        result.append({
            "ts": (r["timestamp"] or "")[:16],
            "action": r["action"],
            "icon": _icons.get(r["action"], "🔹"),
            "name": _names.get(r["action"], r["action"]),
            "ip": target_ip,
            "details": details,
        })
    return result
