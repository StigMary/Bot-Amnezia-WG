"""
webapp/routers/dashboard.py — Роутер для дашборда, карточек серверов, сетевого пинга, ru_monitor и графиков истории.
"""
import asyncio
from fastapi import APIRouter, Depends

from config import cfg
from database import get_all_users, get_server_history
from vpn_engine import (
    get_node_metrics,
    get_node_ping,
    get_ru_monitor_data,
    get_server_monitor_data,
)
from webapp.auth import require_admin
from webapp.metrics import (
    _async_se_metrics,
    _async_vpn_stats,
    get_local_ru_metrics,
)

router = APIRouter(tags=["dashboard"])


@router.get("/api/dashboard")
async def dashboard(admin=Depends(require_admin)):
    stats, se = await asyncio.gather(_async_vpn_stats(), _async_se_metrics())
    all_users = get_all_users()
    online = sum(1 for u in stats if u.get("online_emoji") in ("🟢", "🟡"))
    total = len(all_users) if all_users else len(stats)
    ru = get_local_ru_metrics()

    return {
        "online": online,
        "total": total,
        "se": {
            "cpu": se["cpu"] if se else None,
            "ram": se["ram"] if se else None,
            "disk": se["disk"] if se else None,
            "uptime": se["uptime"] if se else None,
            "online": bool(se),
        },
        "ru": {
            "cpu": ru["cpu"] if ru else None,
            "ram": ru["ram"] if ru else None,
            "disk": ru["disk"] if ru else None,
            "uptime": ru["uptime"] if ru else None,
            "online": bool(ru),
        },
    }


@router.get("/api/servers")
async def servers(admin=Depends(require_admin)):
    loop = asyncio.get_event_loop()

    def _fetch_nodes():
        res = []
        if cfg.se_host:
            se_node = {
                "id": "SE",
                "name": "🇸🇪 SE Server (Main)",
                "alias": "🇸🇪 SE Server (Main)",
                "host": cfg.se_host,
                "port": cfg.se_ssh_port,
                "user": cfg.se_user,
                "container": "xray",
            }
            m = get_node_metrics(se_node)
            res.append({
                "id": "SE",
                "name": "🇸🇪 SE Server (Main)",
                "alias": "🇸🇪 SE Server (Main)",
                "host": cfg.se_host,
                "port": cfg.se_ssh_port,
                "container": "xray",
                "online": bool(m),
                "cpu": float(m["cpu"]) if m and m.get("cpu") else 0,
                "ram": m.get("ram", 0) if m else 0,
                "ram_used": m.get("ram_used", 0) if m else 0,
                "ram_total": m.get("ram_total", 0) if m else 0,
                "disk": m.get("disk", 0) if m else 0,
                "uptime": m.get("uptime", "Офлайн") if m else "Офлайн",
                "net_recv": m.get("net_recv", 0) if m else 0,
                "net_sent": m.get("net_sent", 0) if m else 0,
            })

        for c in cfg.vpn_containers:
            m = get_node_metrics(c)
            alias = c.get("alias", "Server")
            c_id = c.get("id") or ("RU" if "RU" in alias.upper() else alias)
            res.append({
                "id": c_id,
                "name": alias,
                "alias": alias,
                "host": c["host"],
                "port": c.get("port"),
                "container": c["name"],
                "online": bool(m),
                "cpu": float(m["cpu"]) if m and m.get("cpu") else 0,
                "ram": m.get("ram", 0) if m else 0,
                "ram_used": m.get("ram_used", 0) if m else 0,
                "ram_total": m.get("ram_total", 0) if m else 0,
                "disk": m.get("disk", 0) if m else 0,
                "uptime": m.get("uptime", "Офлайн") if m else "Офлайн",
                "net_recv": m.get("net_recv", 0) if m else 0,
                "net_sent": m.get("net_sent", 0) if m else 0,
            })
        return res

    nodes_data = await loop.run_in_executor(None, _fetch_nodes)
    se_data = next((n for n in nodes_data if "SE" in n["name"]), nodes_data[0] if nodes_data else None)
    ru_data = next((n for n in nodes_data if "RU" in n["name"]), nodes_data[1] if len(nodes_data) > 1 else None)
    return {
        "servers": nodes_data,
        "se": se_data,
        "ru": ru_data,
    }


@router.get("/api/ping")
async def ping(server: str = "", admin=Depends(require_admin)):
    hosts = ["8.8.8.8", "1.1.1.1", "77.88.8.8", "telegram.org", "instagram.com"]
    labels = ["Google", "Cloudflare", "Yandex", "Telegram", "Instagram"]
    target = next((c for c in cfg.vpn_containers if c["alias"] == server or c.get("id") == server), None)
    if not target:
        target = next((c for c in cfg.vpn_containers if "RU" in c["alias"]), cfg.vpn_containers[0] if cfg.vpn_containers else None)
    pings = get_node_ping(target) if target else []
    result = []
    for i, label in enumerate(labels):
        val = pings[i] if i < len(pings) else None
        try:
            ms = float(val) if val and val != "timeout" else None
        except Exception:
            ms = None
        result.append({"host": hosts[i], "label": label, "ms": ms})
    return result


@router.get("/api/ru_monitor")
async def ru_monitor(admin=Depends(require_admin)):
    """Возвращает данные мониторинга RU: статус Xray, активные клиенты, топ, события."""
    data = get_server_monitor_data("RU")
    return data


@router.get("/api/server/{side}/monitor")
async def server_monitor(side: str, admin=Depends(require_admin)):
    """Универсальный мониторинг любого сервера (SE, RU, Node N): статус Xray/служб, активные клиенты, топ."""
    data = get_server_monitor_data(side)
    return data


@router.get("/api/server/{side}/history")
async def server_history(side: str, hours: int = 24, admin=Depends(require_admin)):
    history = get_server_history(side, hours)
    if not history:
        matched = next(
            (c["alias"] for c in cfg.vpn_containers if side.upper() in c["alias"].upper() or c.get("id") == side),
            None,
        )
        if matched:
            history = get_server_history(matched, hours)
    return history
