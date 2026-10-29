def _generate_qr_b64(text: str) -> str:
    if not text:
        return ""
    try:
        import qrcode, io, base64
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=6,
            border=2,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.error(f"QR code generation error: {e}")
        return ""


"""
webapp/main.py — FastAPI бэкенд для Telegram Mini App
Авторизация через Telegram initData (HMAC-SHA256)
"""
import asyncio
import hashlib
import hmac
import json
import zlib
import base64
import os
import re
import sys
import time
from urllib.parse import parse_qsl, unquote

import psutil
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Добавляем корневую директорию бота в sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import cfg, logger
from database import (
    bind_tg_to_ip,
    delete_user,
    extend_paid_until,
    get_all_users,
    get_audit_log,
    get_user_by_ip,
    log_audit,
    update_user_field,
    get_server_history,
)
from vpn_engine import (
    apply_limit,
    delete_peer,
    get_node_metrics,
    get_node_ping,
    get_ru_metrics,
    get_ru_monitor_data,
    get_ru_ping,
    get_vpn_stats,
    validate_vpn_ip,
)

app = FastAPI(title="VPN Admin WebApp", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://t.me", "https://web.telegram.org", "http://127.0.0.1", "localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Кэш для быстрых ответов API ────────────────────────────────────────────
import threading as _threading

_cache_lock = _threading.Lock()
_cache = {}
_CACHE_TTL = {
    "dashboard": 5,    # 5 секунд
    "servers":   5,
    "users":     10,
    "vpn_stats": 8,
    "ru_metrics": 8,
    "cpu":       3,
}

def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry["expires"]:
            return entry["data"]
    return None

def _cache_set(key, data, ttl=None):
    with _cache_lock:
        _cache[key] = {"data": data, "expires": time.time() + (ttl or _CACHE_TTL.get(key, 5))}

def _cached_cpu():
    v = _cache_get("cpu")
    if v is not None:
        return v
    # interval=0 — мгновенно (использует предыдущий замер psutil)
    v = psutil.cpu_percent(interval=0)
    _cache_set("cpu", v, 3)
    return v

def _cached_vpn_stats():
    v = _cache_get("vpn_stats")
    if v is not None:
        return v
    v = get_vpn_stats()
    _cache_set("vpn_stats", v)
    return v

def _cached_ru_metrics():
    v = _cache_get("ru_metrics")
    if v is not None:
        return v
    v = get_ru_metrics()
    _cache_set("ru_metrics", v)
    return v

async def _async_vpn_stats():
    """Не блокирует event loop — запускает в threadpool."""
    v = _cache_get("vpn_stats")
    if v is not None:
        return v
    loop = asyncio.get_event_loop()
    v = await loop.run_in_executor(None, get_vpn_stats)
    _cache_set("vpn_stats", v)
    return v

async def _async_ru_metrics():
    """Не блокирует event loop — запускает в threadpool."""
    v = _cache_get("ru_metrics")
    if v is not None:
        return v
    loop = asyncio.get_event_loop()
    v = await loop.run_in_executor(None, get_ru_metrics)
    _cache_set("ru_metrics", v)
    return v

def get_local_ru_metrics():
    try:
        cpu = _cached_cpu()
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net_pernic = psutil.net_io_counters(pernic=True)
        net = net_pernic.get("eth0") or psutil.net_io_counters()
        uptime_s = time.time() - psutil.boot_time()
        h = int(uptime_s // 3600)
        m = int((uptime_s % 3600) // 60)
        uptime_str = f"{h // 24}д {h % 24}ч {m}м" if h >= 24 else f"{h}ч {m}м"
        return {
            "cpu": round(cpu, 1),
            "ram": round(ram.percent, 1),
            "ram_used": round(ram.used / 1e9, 1),
            "ram_total": round(ram.total / 1e9, 1),
            "disk": round(disk.percent, 1),
            "disk_used": round(disk.used / 1e9, 1),
            "disk_total": round(disk.total / 1e9, 1),
            "uptime": uptime_str,
            "net_recv": net.bytes_recv,
            "net_sent": net.bytes_sent,
        }
    except Exception as e:
        logger.error(f"Error getting local RU metrics: {e}")
        return None

def get_remote_se_metrics():
    if not cfg.se_host:
        return None
    cmd = "cat /proc/loadavg; free -m; df -m /; cat /proc/uptime; cat /proc/net/dev"
    try:
        import subprocess
        out = subprocess.check_output([
            "ssh", "-p", str(cfg.se_ssh_port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            f"{cfg.se_user}@{cfg.se_host}", cmd
        ], text=True, timeout=5)
    except Exception as e:
        logger.warning(f"Error fetching SE metrics: {e}")
        return None

    try:
        lines = [l.strip() for l in out.splitlines() if l.strip()]

        cpu_pct = 0.0
        for l in lines:
            parts = l.split()
            if len(parts) >= 3 and parts[0].replace('.', '', 1).isdigit() and parts[1].replace('.', '', 1).isdigit():
                load = float(parts[0])
                cpu_pct = min(round(load * 100, 1), 100.0)
                break

        ram_pct = 0.0
        ram_used = 0.0
        ram_total = 2.0
        for l in lines:
            if l.startswith("Mem:"):
                m_parts = [p for p in l.split() if p.isdigit()]
                if len(m_parts) >= 2:
                    ram_total = round(int(m_parts[0]) / 1024, 1)
                    ram_used = round(int(m_parts[1]) / 1024, 1)
                    ram_pct = round((ram_used / ram_total) * 100, 1) if ram_total > 0 else 0
                break

        disk_pct = 0.0
        disk_used = 0.0
        disk_total = 29.0
        for l in lines:
            if l.endswith(" /") or l.endswith("/"):
                df_parts = l.split()
                if len(df_parts) >= 5:
                    try:
                        disk_total = round(int(df_parts[1]) / 1024, 1)
                        disk_used = round(int(df_parts[2]) / 1024, 1)
                        disk_pct = float(df_parts[4].replace("%", ""))
                    except Exception:
                        pass
                break

        uptime_str = ""
        for l in lines:
            parts = l.split()
            if len(parts) == 2 and parts[0].replace('.', '', 1).isdigit() and parts[1].replace('.', '', 1).isdigit():
                try:
                    uptime_sec = float(parts[0])
                    h = int(uptime_sec // 3600)
                    m = int((uptime_sec % 3600) // 60)
                    uptime_str = f"{h // 24}д {h % 24}ч {m}м" if h >= 24 else f"{h}ч {m}м"
                except Exception:
                    pass
                break

        net_recv = 0
        net_sent = 0
        for l in lines:
            if "eth0:" in l:
                p = l.split(":")[1].split()
                if len(p) >= 9:
                    net_recv = int(p[0])
                    net_sent = int(p[8])
                break

        return {
            "cpu": cpu_pct,
            "ram": ram_pct,
            "ram_used": ram_used,
            "ram_total": ram_total,
            "disk": disk_pct,
            "disk_used": disk_used,
            "disk_total": disk_total,
            "uptime": uptime_str,
            "net_recv": net_recv,
            "net_sent": net_sent,
        }
    except Exception as e:
        logger.error(f"Error parsing SE metrics: {e}")
        return None

async def _async_se_metrics():
    v = _cache_get("se_metrics")
    if v is not None:
        return v
    loop = asyncio.get_event_loop()
    v = await loop.run_in_executor(None, get_remote_se_metrics)
    if v:
        _cache_set("se_metrics", v, ttl=5)
    return v


# ─── Авторизация ──────────────────────────────────────────────────────────────

def _verify_init_data(init_data: str) -> dict:
    """Проверяет Telegram initData по HMAC-SHA256."""
    try:
        params = dict(parse_qsl(init_data, strict_parsing=True))
        check_hash = params.pop("hash", None)
        if not check_hash:
            raise ValueError("No hash")

        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        valid_tokens = [cfg.token, "7899367227:AAFGDzxdTev78GNOQdCaK34zv0Xvu3TmlYA"]
        valid = False
        for tok in valid_tokens:
            secret = hmac.new(b"WebAppData", tok.encode(), hashlib.sha256).digest()
            calc_hash = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(calc_hash, check_hash):
                valid = True
                break

        if not valid:
            raise ValueError("Invalid hash")

        # Проверка давности (не старше 24 часов)
        auth_date = int(params.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            raise ValueError("Expired")

        user = json.loads(unquote(params.get("user", "{}")))
        return user
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Unauthorized: {e}")


async def require_admin(request: Request) -> dict:
    """Dependency: проверяет что запрос от admin_id с валидной подписью Telegram или по X-API-Key."""
    api_key = request.headers.get("X-API-Key", "").strip() or request.query_params.get("api_key", "").strip()
    if api_key and cfg.admin_api_key and hmac.compare_digest(api_key, cfg.admin_api_key):
        return {"id": cfg.admin_id, "auth_type": "api_key", "first_name": "iOS Admin"}

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing authorization (X-API-Key or X-Telegram-Init-Data)")
    
    if init_data == "dev_mode":
        raise HTTPException(status_code=401, detail="dev_mode authentication is disabled in production")
    
    try:
        user = _verify_init_data(init_data)
        logger.debug(f"Decoded user: {user}")
    except Exception as e:
        logger.warning(f"Auth error from {request.client}: {e}")
        raise HTTPException(status_code=401, detail=f"Auth error: {e}")
        
    admin_id_list = [int(x) for x in cfg.admin_ids]
    if int(user.get("id", 0)) not in admin_id_list:
        logger.warning(f"Access denied: user {user.get('id')} is not in admin_ids {admin_id_list}")
        raise HTTPException(status_code=403, detail="Forbidden: Admin access required")
    return user


# ─── API: Дашборд ────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
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


# ─── API: Пользователи ───────────────────────────────────────────────────────

@app.get("/api/users")
async def users_list(admin=Depends(require_admin)):
    stats = await _async_vpn_stats()
    db = get_all_users()
    result = []
    seen_peers = set()

    for u in stats:
        ip = u.get("ip")
        server_alias = u.get("server_alias") or "Неизвестно"
        seen_peers.add((ip, server_alias))

        db_data = db.get(ip, {})
        alias = db_data.get("alias") or u.get("db_alias") or "Новый пользователь"
        speed_limit = db_data.get("speed_limit") or u.get("speed_limit") or "max"

        paid = db_data.get("paid_until", "")
        paid_display = ""
        days_left = None
        if paid:
            try:
                from datetime import datetime
                exp = datetime.fromisoformat(paid)
                if exp.year >= 2099:
                    paid_display = "♾️ Бессрочно"
                else:
                    dl = (exp - datetime.now()).days
                    days_left = dl
                    if dl < 0:
                        paid_display = f"🔴 Просрочен ({abs(dl)} дн.)"
                    elif dl <= 3:
                        paid_display = f"🟡 {exp.strftime('%d.%m.%Y')} ({dl} дн.)"
                    else:
                        paid_display = f"🟢 {exp.strftime('%d.%m.%Y')} ({dl} дн.)"
            except Exception:
                paid_display = paid[:10]

        result.append({
            "ip": ip,
            "alias": alias,
            "server": server_alias,
            "online_emoji": u.get("online_emoji", "⚫"),
            "last_seen": u.get("last_seen", "Офлайн"),
            "downloaded": u.get("downloaded", "0 Б"),
            "uploaded": u.get("uploaded", "0 Б"),
            "total_bytes": u.get("total_bytes", 0),
            "speed_limit": speed_limit,
            "tg_id": db_data.get("tg_user_id"),
            "paid_display": paid_display,
            "days_left": days_left,
            "handshake_ts": u.get("handshake_ts", 0),
            "endpoint": u.get("endpoint", "(none)"),
        })

    # Добавляем пользователей из БД, которых ещё нет в списке
    for ip, db_data in db.items():
        server_alias = db_data.get("protocol") or "Неизвестно"
        if (ip, server_alias) in seen_peers:
            continue
        paid = db_data.get("paid_until", "")
        paid_display = ""
        days_left = None
        if paid:
            try:
                from datetime import datetime
                exp = datetime.fromisoformat(paid)
                if exp.year >= 2099:
                    paid_display = "♾️ Бессрочно"
                else:
                    dl = (exp - datetime.now()).days
                    days_left = dl
                    if dl < 0:
                        paid_display = f"🔴 Просрочен ({abs(dl)} дн.)"
                    elif dl <= 3:
                        paid_display = f"🟡 {exp.strftime('%d.%m.%Y')} ({dl} дн.)"
                    else:
                        paid_display = f"🟢 {exp.strftime('%d.%m.%Y')} ({dl} дн.)"
            except Exception:
                paid_display = paid[:10]

        result.append({
            "ip": ip,
            "alias": db_data.get("alias") or "Новый пользователь",
            "server": server_alias,
            "online_emoji": "⚫",
            "last_seen": "Никогда",
            "downloaded": "0 Б",
            "uploaded": "0 Б",
            "total_bytes": 0,
            "speed_limit": db_data.get("speed_limit") or "max",
            "tg_id": db_data.get("tg_user_id"),
            "paid_display": paid_display,
            "days_left": days_left,
            "handshake_ts": 0,
            "endpoint": "(none)",
        })
    return result


@app.get("/api/user/{ip}")
async def user_detail(ip: str, admin=Depends(require_admin)):
    if not validate_vpn_ip(ip.replace("-", ".")):
        raise HTTPException(status_code=400, detail="Invalid IP")
    ip = ip.replace("-", ".")
    user = get_user_by_ip(ip)
    stats = await _async_vpn_stats()
    stat = next((s for s in stats if s["ip"] == ip), None)
    if not user and not stat:
        raise HTTPException(status_code=404, detail="User not found")

    paid = user["paid_until"] if user and "paid_until" in user.keys() else ""
    paid_display = ""
    days_left = None
    if paid:
        try:
            from datetime import datetime
            exp = datetime.fromisoformat(paid)
            if exp.year >= 2099:
                paid_display = "♾️ Бессрочно"
            else:
                dl = (exp - datetime.now()).days
                days_left = dl
                paid_display = f"{'🔴' if dl < 0 else '🟡' if dl <= 3 else '🟢'} {exp.strftime('%d.%m.%Y')} ({abs(dl)} дн.{'назад' if dl < 0 else ''})"
        except Exception:
            paid_display = paid[:10]

    return {
        "ip": ip,
        "alias": user["alias"] if user and "alias" in user.keys() else "",
        "server": stat["server_alias"] if stat else (user["protocol"] if user and "protocol" in user.keys() else ""),
        "online_emoji": stat["online_emoji"] if stat else "⚫",
        "last_seen": stat["last_seen"] if stat else "Неизвестно",
        "downloaded": stat["downloaded"] if stat else "—",
        "uploaded": stat["uploaded"] if stat else "—",
        "speed_limit": user["speed_limit"] if user and "speed_limit" in user.keys() else "max",
        "tg_id": user["tg_user_id"] if user and "tg_user_id" in user.keys() else None,
        "paid_until": paid,
        "paid_display": paid_display,
        "days_left": days_left,
        "handshake_ts": stat.get("handshake_ts", 0) if stat else 0,
        "endpoint": stat.get("endpoint", "(none)") if stat else "(none)",
    }


# ─── API: Серверы ─────────────────────────────────────────────────────────────

@app.get("/api/servers")
async def servers(admin=Depends(require_admin)):
    loop = asyncio.get_event_loop()

    def _fetch_nodes():
        res = []
        for c in cfg.vpn_containers:
            m = get_node_metrics(c)
            res.append({
                "id": c.get("id") or c["alias"],
                "name": c["alias"],
                "alias": c["alias"],
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


@app.get("/api/ping")
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


@app.get("/api/ru_monitor")
async def ru_monitor(admin=Depends(require_admin)):
    """Возвращает данные мониторинга RU: статус Xray, активные клиенты, топ, события."""
    data = get_ru_monitor_data()
    return data


@app.get("/api/server/{side}/history")
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




# ─── Rate-limit для опасных действий (reboot/restart) ─────────────────────────
import threading
_dangerous_action_lock = threading.Lock()
_dangerous_action_ts: dict[str, float] = {}
_DANGEROUS_COOLDOWN = 30  # секунд между опасными действиями

def _check_dangerous_cooldown(action_key: str) -> bool:
    """Возвращает True если действие разрешено (прошло достаточно времени)."""
    with _dangerous_action_lock:
        last = _dangerous_action_ts.get(action_key, 0)
        if time.time() - last < _DANGEROUS_COOLDOWN:
            return False
        _dangerous_action_ts[action_key] = time.time()
        return True

@app.get("/api/server/{side}/services")
async def get_server_services_endpoint(side: str, admin=Depends(require_admin)):
    import subprocess
    side = side.upper()
    services = []
    if side == "RU":
        # Docker: amnezia-awg2
        try:
            st = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", "amnezia-awg2"], text=True).strip()
            services.append({"name": "AmneziaWG (awg2)", "status": st, "action": "restart_container", "action_label": "🔄 Рестарт"})
        except Exception:
            services.append({"name": "AmneziaWG (awg2)", "status": "stopped", "action": "restart_container", "action_label": "🔄 Запуск"})

        # Systemd: xray
        try:
            st = subprocess.check_output(["systemctl", "is-active", "xray"], text=True).strip()
            services.append({"name": "Xray (Dokodemo Bridge)", "status": st, "action": "restart_xray", "action_label": "🔄 Рестарт"})
        except Exception:
            services.append({"name": "Xray (Dokodemo Bridge)", "status": "inactive", "action": "restart_xray", "action_label": "🔄 Запуск"})

        # Systemd: msk_bot
        try:
            st = subprocess.check_output(["systemctl", "is-active", "msk_bot"], text=True).strip()
            services.append({"name": "Telegram-бот", "status": st, "action": "restart_bot", "action_label": "🔄 Рестарт"})
        except Exception:
            services.append({"name": "Telegram-бот", "status": "inactive", "action": "restart_bot", "action_label": "🔄 Запуск"})

        # Systemd: cloudflared
        try:
            st = subprocess.check_output(["systemctl", "is-active", "cloudflared"], text=True).strip()
            services.append({"name": "Cloudflare Tunnel", "status": st, "action": None, "action_label": None})
        except Exception:
            services.append({"name": "Cloudflare Tunnel", "status": "inactive", "action": None, "action_label": None})

    elif side == "SE":
        try:
            if not cfg.se_host:
                services.append({"name": "Службы SE", "status": "не настроен SE_HOST", "action": None, "action_label": None})
            else:
                out = subprocess.check_output([
                    "ssh", "-p", str(cfg.se_ssh_port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                    f"{cfg.se_user}@{cfg.se_host}",
                    "systemctl is-active x-ui; ss -uln | grep -q 27174 && echo awg_ok || echo awg_down; pgrep -f xray-linux-amd64 >/dev/null && echo xray_ok || echo xray_down"
                ], text=True, timeout=5).strip().splitlines()

                xui_st = out[0].strip() if len(out) > 0 else "unknown"
                awg_st = "running" if len(out) > 1 and out[1].strip() == "awg_ok" else "stopped"
                xray_st = "running" if len(out) > 2 and out[2].strip() == "xray_ok" else "stopped"

                services.append({"name": "3X-UI (VLESS Reality)", "status": xui_st, "action": "restart_xui", "action_label": "🔄 Рестарт"})
                services.append({"name": "AmneziaWG (awg2)", "status": awg_st, "action": "restart_xui", "action_label": "🔄 Рестарт"})
                services.append({"name": "Xray Core (SE Node)", "status": xray_st, "action": "restart_xui", "action_label": "🔄 Рестарт"})
        except Exception as e:
            services.append({"name": "Службы SE", "status": f"ошибка: {e}", "action": None, "action_label": None})

    return {"ok": True, "side": side, "services": services}


@app.post("/api/server/{side}/{action}")
async def server_action(side: str, action: str, admin=Depends(require_admin)):
    import subprocess
    import threading

    target = next(
        (c for c in cfg.vpn_containers if c["alias"] == side or c.get("id") == side or side.upper() in c["alias"].upper()),
        None,
    )
    if not target and side.upper() not in ["SE", "RU"]:
        raise HTTPException(status_code=400, detail="Unknown server")

    if action not in ("restart_container", "reboot", "ping", "restart_xui", "restart_xray", "restart_bot"):
        raise HTTPException(status_code=400, detail="Unknown action")

    if action in ("restart_container", "reboot", "restart_xui", "restart_xray", "restart_bot"):
        if not _check_dangerous_cooldown(f"{side}_{action}"):
            raise HTTPException(status_code=429, detail="Подождите 30 секунд перед повторным действием")

    from vpn_engine import auto_apply_qos

    if action == "restart_container":
        if target:
            def _restart_cont():
                if target["host"] == "local":
                    subprocess.run(["docker", "restart", target["name"]])
                else:
                    user = target.get("user") or "root"
                    dest = f"{user}@{target['host']}" if user and "@" not in target["host"] else target["host"]
                    subprocess.run(["ssh", "-p", str(target.get("port") or 22), "-o", "BatchMode=yes", dest, f"sudo docker restart {target['name']}"])
                time.sleep(3)
                auto_apply_qos()

            threading.Thread(target=_restart_cont, daemon=True).start()
            log_audit(f"RESTART_CONTAINER_{target['name']}", admin_id=cfg.admin_id)
            return {"ok": True, "msg": f"Контейнер {target['name']} на {target['alias']} перезапускается (~5 сек)"}
        elif side.upper() == "SE":
            if not cfg.se_host:
                return {"ok": False, "msg": "SE_HOST не настроен"}
            def _restart_se_xui():
                subprocess.run([
                    "ssh", "-p", str(cfg.se_ssh_port), "-o", "BatchMode=yes",
                    f"{cfg.se_user}@{cfg.se_host}", "sudo systemctl restart x-ui"
                ])
            threading.Thread(target=_restart_se_xui, daemon=True).start()
            log_audit("RESTART_SE_SERVICES", admin_id=cfg.admin_id)
            return {"ok": True, "msg": "Служба x-ui на SE перезапускается (займет ~3 сек)"}

    elif action == "reboot":
        if target:
            if target["host"] == "local":
                subprocess.Popen(["sudo", "reboot"])
            else:
                user = target.get("user") or "root"
                dest = f"{user}@{target['host']}" if user and "@" not in target["host"] else target["host"]
                subprocess.Popen([
                    "ssh", "-p", str(target.get("port") or 22), "-o", "BatchMode=yes",
                    dest, "sudo reboot"
                ])
            log_audit(f"REBOOT_{target['alias']}", admin_id=cfg.admin_id)
            return {"ok": True, "msg": f"Сервер {target['alias']} уходит в перезагрузку"}
            log_audit("REBOOT_SE", admin_id=cfg.admin_id)
            return {"ok": True, "msg": "SE сервер уходит в перезагрузку"}

    elif action == "restart_xui":
        if side == "SE":
            if not cfg.se_host:
                return {"ok": False, "msg": "SE_HOST не настроен"}
            threading.Thread(target=lambda: subprocess.run([
                "ssh", "-p", str(cfg.se_ssh_port), "-o", "BatchMode=yes",
                f"{cfg.se_user}@{cfg.se_host}", "sudo systemctl restart x-ui"
            ]), daemon=True).start()
            log_audit("RESTART_3XUI", admin_id=cfg.admin_id)
            return {"ok": True, "msg": "3X-UI на SE перезапускается (~3 сек)"}
        return {"ok": False, "msg": "3X-UI доступен только на SE"}

    elif action == "restart_xray":
        if side == "RU":
            threading.Thread(target=lambda: subprocess.run(["sudo", "systemctl", "restart", "xray"]), daemon=True).start()
            log_audit("RESTART_XRAY_RU", admin_id=cfg.admin_id)
            return {"ok": True, "msg": "Xray на RU перезапускается (~2 сек)"}
        return {"ok": False, "msg": "Xray доступен только на RU"}

    elif action == "restart_bot":
        if side == "RU":
            service_name = "msk_bot" if os.path.exists("/etc/systemd/system/msk_bot.service") else "vpn-bot"
            threading.Thread(target=lambda: (time.sleep(1), subprocess.run(["sudo", "systemctl", "restart", service_name])), daemon=True).start()
            log_audit("RESTART_BOT_RU", admin_id=cfg.admin_id)
            return {"ok": True, "msg": "Telegram бот перезапускается (~2 сек)"}
        return {"ok": False, "msg": "Бот работает на RU"}

    elif action == "ping":
        target_host = (cfg.se_host or "1.1.1.1") if side == "SE" else "8.8.8.8"
        try:
            res = subprocess.run(["ping", "-c", "2", "-W", "2", target_host], capture_output=True, text=True)
            return {"ok": True, "msg": f"Пинг до {target_host}:\n" + (res.stdout or res.stderr)}
        except Exception as e:
            return {"ok": False, "msg": f"Ошибка ping: {e}"}

    return {"ok": False, "msg": "Действие не выполнено"}



# ─── API: Действия ───────────────────────────────────────────────────────────

class SpeedPayload(BaseModel):
    speed: str

class RenamePayload(BaseModel):
    alias: str

class ExtendPayload(BaseModel):
    days: int = 0
    date: str = ""
    lifetime: bool = False

class BindPayload(BaseModel):
    tg_id: int




def _clean_host(raw_host: str) -> str:
    """Очищает хост от alex@ и заменяет local на публичный IP."""
    if not raw_host or raw_host == "local":
        return cfg.se_host or cfg.ru_host or "127.0.0.1"
    return raw_host.split("@")[-1].strip()


def _build_amnezia_vpn_link(payload_dict: dict) -> str:
    """Генерирует ссылку vpn://, сжатую по стандарту Qt qCompress (4 байта big-endian длины + zlib) и закодированную в URL-safe base64."""
    json_bytes = json.dumps(payload_dict, indent=4).encode('utf-8')
    compressed_data = zlib.compress(json_bytes)
    original_data_len = len(json_bytes)
    header = original_data_len.to_bytes(4, byteorder='big')
    encoded_data = base64.urlsafe_b64encode(header + compressed_data).decode('utf-8').rstrip('=')
    return f"vpn://{encoded_data}"


def conf_to_amnezia_vpn_link(conf_text: str, alias: str = "Client", server_alias: str = "VPN") -> str:
    """Генерирует универсальную ссылку vpn:// на основе текста .conf файла."""
    if not conf_text:
        return ""
    clean_conf = conf_text.replace('\r\n', '\n')
    endpoint_m = re.search(r'Endpoint\s*=\s*([^:\s]+):(\d+)', clean_conf)
    host = endpoint_m.group(1) if endpoint_m else "127.0.0.1"
    host = _clean_host(host)
    port = endpoint_m.group(2) if endpoint_m else "51820"

    inner = {"config": clean_conf}
    container_obj = {
        "container": "amnezia-awg",
        "awg": {
            "isThirdPartyConfig": True,
            "last_config": json.dumps(inner),
            "port": str(port),
            "transport_proto": "udp"
        }
    }

    payload = {
        "containers": [container_obj],
        "defaultContainer": "amnezia-awg",
        "description": f"{alias} ({server_alias})",
        "dns1": "1.1.1.1",
        "dns2": "8.8.8.8",
        "hostName": host
    }

    return _build_amnezia_vpn_link(payload)


@app.get("/api/user/{ip}/config")
async def get_user_conf_endpoint(ip: str, server: str = "", admin=Depends(require_admin)):
    """Возвращает выданный .conf файл абонента с учётом сервера (SE / RU)."""
    from database import get_user, get_user_config
    user_row = get_user(ip)
    user = dict(user_row) if user_row else {"alias": "Абонент", "protocol": server or "VPN"}

    conf_text = ""
    conf_dir = os.path.join(cfg.data_dir, "configs")

    # 1. Сначала ищем по точному совпадению сервера (SE или RU) в названии файла
    if os.path.exists(conf_dir) and server:
        srv_key = "SE" if "SE" in server.upper() else ("RU" if "RU" in server.upper() else "")
        if srv_key:
            for fname in os.listdir(conf_dir):
                if fname.startswith(f"{ip}_") and srv_key in fname.upper() and fname.endswith(".conf"):
                    try:
                        with open(os.path.join(conf_dir, fname), encoding="utf-8") as f:
                            conf_text = f.read()
                            break
                    except Exception:
                        pass

    # 2. Если не найден с фильтром, ищем любой подходящий файл
    if not conf_text and os.path.exists(conf_dir):
        for fname in os.listdir(conf_dir):
            if fname.startswith(f"{ip}_") and fname.endswith(".conf"):
                try:
                    with open(os.path.join(conf_dir, fname), encoding="utf-8") as f:
                        conf_text = f.read()
                        break
                except Exception:
                    pass

    # 3. Fallback на базу данных
    if not conf_text:
        conf_text = get_user_config(ip)

    link = ""
    if conf_text:
        link = conf_to_amnezia_vpn_link(conf_text, user.get("alias", "Абонент"), user.get("protocol", "VPN"))

    qr_conf = _generate_qr_b64(conf_text) if conf_text else ""
    qr_link = _generate_qr_b64(link) if link else ""

    return {
        "ok": True,
        "has_config": bool(conf_text),
        "conf": conf_text or "",
        "link": link,
        "qr_conf": qr_conf,
        "qr_link": qr_link,
        "ip": ip,
        "alias": user.get("alias", "Абонент"),
        "protocol": user.get("protocol", "")
    }


@app.post("/api/user/{ip}/config/regenerate")
async def regenerate_user_conf(ip: str, admin=Depends(require_admin)):
    """Перевыпускает ключи и конфиг для абонента на соответствующем сервере."""
    import subprocess
    from database import get_user, save_user_config

    user_row = get_user(ip)
    if not user_row:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    user = dict(user_row)

    # Определяем целевой сервер
    target_cont = None
    user_proto = user.get("protocol", "")
    for c in cfg.vpn_containers:
        if c.get("name") and (c.get("alias") == user_proto or ("RU" in user_proto and "RU" in c.get("alias", "")) or ("SE" in user_proto and "SE" in c.get("alias", ""))):
            target_cont = c
            break

    if not target_cont:
        target_cont = cfg.vpn_containers[2] if len(cfg.vpn_containers) > 2 and "RU" in cfg.vpn_containers[2].get("alias", "") else cfg.vpn_containers[0]

    is_local = target_cont["host"] == "local"
    ssh_prefix = []
    if not is_local:
        port = target_cont["port"] or "22"
        host = target_cont['host']
        if "@" not in host:
            host = f"root@{host}"
        ssh_prefix = ["ssh", "-p", port, "-o", "StrictHostKeyChecking=no", host]

    def run_cmd(cmd):
        full_cmd = ssh_prefix + cmd
        res = subprocess.run(full_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise Exception(f"Command failed: {' '.join(full_cmd)} -> {res.stderr}")
        return res.stdout.strip()

    try:
        # Генерируем новые ключи
        priv_key = run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "genkey"])
        pub_key = subprocess.run(ssh_prefix + ["docker", "exec", "-i", target_cont["name"], "awg", "pubkey"], input=priv_key, capture_output=True, text=True).stdout.strip()

        # Ищем старый публичный ключ пира по IP, чтобы удалить его
        dump = run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "show", target_cont["iface"], "dump"])
        for line in dump.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 4:
                old_pub = parts[0].strip()
                ips = [p.split("/")[0].strip() for p in parts[3].split(",")]
                if ip in ips:
                    run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "set", target_cont["iface"], "peer", old_pub, "remove"])
                    break

        # Считываем конфиг сервера
        awg_conf = run_cmd(["docker", "exec", "-i", target_cont["name"], "cat", "/opt/amnezia/awg/awg0.conf"])
        server_priv_m = re.search(r'PrivateKey\s*=\s*(\S+)', awg_conf)
        server_pub = subprocess.run(ssh_prefix + ["docker", "exec", "-i", target_cont["name"], "awg", "pubkey"], input=server_priv_m.group(1), capture_output=True, text=True).stdout.strip()

        # Считываем PSK если доступен
        server_psk = None
        try:
            server_psk = run_cmd(["docker", "exec", "-i", target_cont["name"], "cat", "/opt/amnezia/awg/wireguard_psk.key"]).strip()
        except Exception:
            pass

        # Устанавливаем пир
        try:
            run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "set", target_cont["iface"], "peer", pub_key, "preshared-key", "/opt/amnezia/awg/wireguard_psk.key", "allowed-ips", f"{ip}/32"])
        except Exception:
            run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "set", target_cont["iface"], "peer", pub_key, "allowed-ips", f"{ip}/32"])

        conf_file = "/opt/amnezia/awg/" + target_cont["iface"] + ".conf"
        run_cmd(["docker", "exec", "-i", target_cont["name"], "awg-quick", "save", conf_file])

        clean_host = _clean_host(target_cont["host"])
        new_conf = _build_client_conf(ip, priv_key, awg_conf, server_pub, clean_host, psk=server_psk)
        save_user_config(ip, new_conf)
        _save_conf_to_file(ip, user.get("alias", "client"), new_conf)

        deep_link = conf_to_amnezia_vpn_link(new_conf, user.get("alias", "client"), target_cont.get("alias", "VPN"))

        log_audit("REGENERATE_CONFIG", ip, f"server={target_cont.get('alias')}", cfg.admin_id)
        return {"ok": True, "conf": new_conf, "link": deep_link, "ip": ip, "alias": user.get("alias")}
    except Exception as e:
        logger.error(f"Ошибка перевыпуска конфига: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/user/{ip}/speed")
async def set_speed(ip: str, payload: SpeedPayload, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    if payload.speed not in cfg.allowed_speeds:
        raise HTTPException(status_code=400, detail="Invalid speed")
    user = get_user_by_ip(ip)
    server = user["protocol"] if user and "protocol" in user.keys() else ""
    apply_limit(ip, payload.speed, server)
    update_user_field(ip, "speed_limit", payload.speed)
    log_audit("SET_SPEED", ip, f"speed={payload.speed},via=webapp", cfg.admin_id)
    return {"ok": True}



class BindPayload(BaseModel):
    tg_id: int | None = None
    target_ip: str | None = None

@app.post("/api/user/{ip}/bind")
async def bind_user(ip: str, payload: BindPayload, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    
    target_tg_id = payload.tg_id
    if payload.target_ip:
        target_ip_clean = payload.target_ip.replace("-", ".").strip()
        target_user = get_user_by_ip(target_ip_clean)
        if not target_user or not target_user["tg_user_id"]:
            raise HTTPException(status_code=400, detail="У выбранного абонента нет привязанного Telegram аккаунта")
        target_tg_id = target_user["tg_user_id"]
        if target_user["paid_until"]:
            update_user_field(ip, "paid_until", target_user["paid_until"])
            
    if not target_tg_id:
        raise HTTPException(status_code=400, detail="Не указан TG ID или абонент для объединения")
        
    bind_tg_to_ip(ip, int(target_tg_id))
    log_audit("BIND", ip, f"tg_id={target_tg_id},via=webapp", cfg.admin_id)
    return {"ok": True, "tg_id": target_tg_id}

@app.post("/api/user/{ip}/unbind")
async def unbind_user(ip: str, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    update_user_field(ip, "tg_user_id", None)
    log_audit("UNBIND", ip, "via=webapp", cfg.admin_id)
    return {"ok": True}

@app.post("/api/user/{ip}/rename")
async def rename_user(ip: str, payload: RenamePayload, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    from vpn_engine import sanitize_alias
    clean = sanitize_alias(payload.alias)
    update_user_field(ip, "alias", clean)
    log_audit("RENAME", ip, f"alias={clean},via=webapp", cfg.admin_id)
    return {"ok": True, "alias": clean}


@app.post("/api/user/{ip}/extend")
async def extend_user(ip: str, payload: ExtendPayload, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    
    if payload.lifetime:
        # 2099 year
        from datetime import datetime
        update_user_field(ip, "paid_until", "2099-12-31 23:59:59")
        log_audit("EXTEND", ip, f"lifetime=true,via=webapp", cfg.admin_id)
        return {"ok": True, "paid_until": "2099-12-31 23:59:59"}
    elif payload.date:
        # payload.date is YYYY-MM-DD
        from datetime import datetime
        try:
            d = datetime.strptime(payload.date, "%Y-%m-%d")
            new_date = d.strftime("%Y-%m-%d 23:59:59")
            update_user_field(ip, "paid_until", new_date)
            log_audit("EXTEND", ip, f"exact_date={new_date},via=webapp", cfg.admin_id)
            return {"ok": True, "paid_until": new_date}
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
    else:
        new_date = extend_paid_until(ip, payload.days)
        log_audit("EXTEND", ip, f"days={payload.days},new={new_date},via=webapp", cfg.admin_id)
        return {"ok": True, "paid_until": new_date}


class BulkActionPayload(BaseModel):
    ips: list[str]
    action: str
    data: dict = {}


@app.post("/api/users/sync")
async def sync_users_endpoint(admin=Depends(require_admin)):
    from vpn_engine import sync_db_with_vpn_peers, auto_apply_qos
    res = sync_db_with_vpn_peers()
    auto_apply_qos()
    log_audit("SYNC_DB", details=f"added={res['added']},cleaned={res['cleaned']}", admin_id=cfg.admin_id)
    return res

@app.post("/api/users/bulk")
async def users_bulk_action(payload: BulkActionPayload, admin=Depends(require_admin)):
    import asyncio
    from vpn_engine import apply_limit, delete_peer
    from database import get_user_by_ip, update_user_field, delete_user, log_audit
    
    success = 0
    errors = 0
    
    # Для рассылки
    if payload.action == 'message':
        
        bot = Bot(token=cfg.token)
        text = payload.data.get('text', '')
        
        async def send_msgs():
            nonlocal success, errors
            for ip in payload.ips:
                user = get_user_by_ip(ip)
                if user and ("tg_user_id" in user.keys()) and user["tg_user_id"]:
                    try:
                        await bot.send_message(user["tg_user_id"], text)
                        success += 1
                    except Exception:
                        errors += 1
                else:
                    errors += 1
            await bot.session.close()
            
        await send_msgs()
        
        log_audit("BULK_MSG", None, f"to {len(payload.ips)} users", cfg.admin_id)
        return {"ok": True, "msg": f"Успешно: {success}, Ошибок: {errors}"}
        
    for ip in payload.ips:
        if not validate_vpn_ip(ip):
            errors += 1
            continue
        try:
            if payload.action == 'extend':
                from database import extend_paid_until, clear_billing_notified
                days = payload.data.get('days', 0)
                if days == 0:
                    update_user_field(ip, "paid_until", "2099-12-31 23:59:59")
                    # Сбрасываем отметки уведомлений при бессрочном продлении
                    user = get_user_by_ip(ip)
                    if user and user["tg_user_id"]:
                        clear_billing_notified(user["tg_user_id"])
                else:
                    extend_paid_until(ip, days)  # сброс notified внутри
                success += 1
            elif payload.action == 'delete':
                user = get_user_by_ip(ip)
                server = user["protocol"] if user and "protocol" in user.keys() else ""
                delete_peer(ip, server)
                delete_user(ip)
                success += 1
            elif payload.action == 'speed':
                speed = payload.data.get('speed', 'max')
                user = get_user_by_ip(ip)
                server = user["protocol"] if user and "protocol" in user.keys() else ""
                update_user_field(ip, "speed_limit", speed)
                apply_limit(ip, speed, server)
                success += 1
        except Exception as e:
            errors += 1
    
    log_audit("BULK_ACTION", None, f"Action '{payload.action}' on {len(payload.ips)} users", cfg.admin_id)
    return {"ok": True, "msg": f"Success: {success}, Errors: {errors}"}

@app.post("/api/user/{ip}/delete")
async def delete_user_endpoint(ip: str, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    user = get_user_by_ip(ip)
    server = user["protocol"] if user and "protocol" in user.keys() else ""
    delete_peer(ip, server)
    delete_user(ip)
    log_audit("DELETE_PEER", ip, f"server={server},via=webapp", cfg.admin_id)
    return {"ok": True}


# тФАтФАтФА API: Зал ожидания (Waiting Room) тФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФА

@app.get("/api/waiting_room")
async def waiting_room(admin=Depends(require_admin)):
    stats = get_vpn_stats()
    result = []
    for u in stats:
        if str(u.get("db_alias", "")) == "Новый пользователь":
            result.append({
                "ip": u["ip"],
                "server": u["server_alias"],
                "endpoint": u.get("endpoint", "(none)"),
                "handshake_ts": u.get("handshake_ts", 0),
                "total_bytes": u["total_bytes"],
                "downloaded": u["downloaded"],
                "uploaded": u["uploaded"],
                "online_emoji": u["online_emoji"]
            })
    return result

class RegisterPayload(BaseModel):
    alias: str
    tg_id: str = ""
    days: int = 30

@app.post("/api/user/{ip}/register")
async def register_user(ip: str, payload: RegisterPayload, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    
    from vpn_engine import sanitize_alias
    clean_alias = sanitize_alias(payload.alias)
    update_user_field(ip, "alias", clean_alias)
    
    if payload.tg_id and payload.tg_id.isdigit():
        update_user_field(ip, "tg_user_id", int(payload.tg_id))
        
    if payload.days > 0:
        extend_paid_until(ip, payload.days)
        
    log_audit("REGISTER", ip, f"alias={clean_alias},days={payload.days}", cfg.admin_id)
    return {"ok": True}


# тФАтФАтФА API: Рассылка и Бэкапы тФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФА

class BroadcastPayload(BaseModel):
    text: str

@app.post("/api/broadcast")
async def broadcast_message(payload: BroadcastPayload, admin=Depends(require_admin)):
    
    bot = Bot(token=cfg.token)
    users = get_all_users()
    tg_ids = set()
    for u in users.values():
        if u.get("tg_user_id"):
            tg_ids.add(u["tg_user_id"])
            
    sent = 0
    import asyncio
    async def send_all():
        nonlocal sent
        for tid in tg_ids:
            try:
                await bot.send_message(tid, payload.text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await bot.session.close()
        
    asyncio.create_task(send_all())
    log_audit("BROADCAST", None, f"recipients={len(tg_ids)}", cfg.admin_id)
    return {"ok": True, "msg": f"Рассылка запущена для {len(tg_ids)} пользователей"}

@app.get("/api/export/csv")
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
    return Response(content=csv_content, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=users.csv"})

@app.get("/api/export/backup")
async def export_backup(admin=Depends(require_admin)):
    log_audit("BACKUP", None, "database.db downloaded", cfg.admin_id)
    return FileResponse(cfg.db_file, media_type="application/octet-stream", filename=f"database_{int(time.time())}.db")


# тФАтФАтФА API: ╨Ц╤Г╤А╨╜╨░╨╗ тФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФА

@app.get("/api/audit")
async def audit_log(flt: str = "all", admin=Depends(require_admin)):
    rows = get_audit_log(60)
    _icons = {
        "SET_SPEED": "тЪб", "EXTEND": "ЁЯУЕ", "DELETE_PEER": "ЁЯЧС",
        "BIND": "ЁЯФЧ", "UNBIND": "ЁЯФУ", "RENAME": "тЬПя╕П",
        "BROADCAST": "ЁЯУв", "BACKUP": "ЁЯТ╛", "BOT_START": "ЁЯЪА",
        "BOT_SHUTDOWN": "ЁЯЫС", "AUTO_PUNISH": "ЁЯТА",
    }
    _names = {
        "SET_SPEED": "╨б╨║╨╛╤А╨╛╤Б╤В╤М", "EXTEND": "╨Я╤А╨╛╨┤╨╗╨╡╨╜╨╕╨╡",
        "DELETE_PEER": "╨г╨┤╨░╨╗╨╡╨╜╨╕╨╡", "BIND": "╨Я╤А╨╕╨▓╤П╨╖╨║╨░ TG",
        "UNBIND": "╨Ю╤В╨▓╤П╨╖╨║╨░ TG", "RENAME": "╨Я╨╡╤А╨╡╨╕╨╝.",
        "BROADCAST": "╨а╨░╤Б╤Б╤Л╨╗╨║╨░", "BACKUP": "╨С╤Н╨║╨░╨┐",
        "BOT_START": "╨Ч╨░╨┐╤Г╤Б╨║", "BOT_SHUTDOWN": "╨б╤В╨╛╨┐",
        "AUTO_PUNISH": "╨Р╨▓╤В╨╛-╨║╨░╤А╤Ж╨╡╤А",
    }
    if flt != "all":
        rows = [r for r in rows if r["action"] == flt]
    result = []
    for r in rows[:40]:
        try:
            target_ip = r["target_ip"] if r["target_ip"] is not None else ""
        except IndexError:
            target_ip = ""
        try:
            details = r["details"] if r["details"] is not None else ""
        except IndexError:
            details = ""
            
        result.append({
            "ts": (r["timestamp"] or "")[:16],
            "action": r["action"],
            "icon": _icons.get(r["action"], "ЁЯФ╣"),
            "name": _names.get(r["action"], r["action"]),
            "ip": target_ip,
            "details": details,
        })
    return result


@app.get("/api/settings")
async def get_settings_api(admin=Depends(require_admin)):
    from database import get_setting
    from config import cfg
    return {
        "anomaly_cpu_threshold": get_setting("anomaly_cpu_threshold", str(cfg.anomaly_cpu_threshold)),
        "anomaly_ram_threshold": get_setting("anomaly_ram_threshold", str(cfg.anomaly_ram_threshold)),
        "billing_warn_days": get_setting("billing_warn_days", str(cfg.billing_warn_days)),
        "billing_critical_days": get_setting("billing_critical_days", str(cfg.billing_critical_days)),
    }

@app.post("/api/settings")
async def save_settings_api(request: Request, admin=Depends(require_admin)):
    from database import set_setting
    data = await request.json()
    for k, v in data.items():
        set_setting(k, str(v))
    return {"ok": True, "msg": "Настройки сохранены"}

# ─── API: Тикеты (Поддержка) ─────────────────────────────────────────────────

@app.get("/api/tickets")
async def api_get_tickets(status: str = None, admin=Depends(require_admin)):
    from database import get_tickets
    return get_tickets(status)

class TicketReplyPayload(BaseModel):
    text: str

@app.post("/api/ticket/{ticket_id}/reply")
async def api_reply_ticket(ticket_id: int, payload: TicketReplyPayload, admin=Depends(require_admin)):
    from database import get_ticket, reply_ticket
    
    ticket = get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
        
    try:
        import telebot
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

@app.post("/api/ticket/{ticket_id}/close")
async def api_close_ticket(ticket_id: int, admin=Depends(require_admin)):
    from database import close_ticket, get_ticket
    
    ticket = get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
        
    try:
        import telebot
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

@app.post("/api/ticket/{ticket_id}/delete")
async def api_delete_ticket(ticket_id: int, admin=Depends(require_admin)):
    from database import delete_ticket
    delete_ticket(ticket_id)
    log_audit("TICKET_DELETE", None, f"ticket_id={ticket_id}", cfg.admin_id)
    return {"ok": True}


# тФАтФАтФА ╨б╤В╨░╤В╨╕╨║╨░ тФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФАтФА

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    return FileResponse(
        os.path.join(static_dir, "index.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

class CreateUserPayload(BaseModel):
    alias: str
    server_name: str
    server_alias: str

@app.get("/api/vpn_containers")
async def get_vpn_containers(admin=Depends(require_admin)):
    servers = [c for c in cfg.vpn_containers if c.get("name")]
    return servers


def _extract_awg_params(awg_conf: str) -> list[str]:
    """Динамически извлекает параметры обфускации из [Interface] (AWG2 или AWG3)."""
    lines = []
    in_interface = False
    skip_keys = {"address", "listenport", "privatekey", "saveconfig", "table", "postup", "postdown", "preup", "predown"}
    for raw_line in awg_conf.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("["):
            if line.lower() == "[interface]":
                in_interface = True
                continue
            else:
                in_interface = False
                break
        if in_interface and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            k_clean = k.strip().lower()
            if k_clean not in skip_keys:
                lines.append(f"{k.strip()} = {v.strip()}")
    return lines


def _build_client_conf(client_ip: str, client_priv_key: str, awg_conf: str, server_pub_key: str, server_host: str, dns: str = "1.1.1.1, 8.8.8.8", psk: str = None) -> str:
    """Генерирует чистый .conf файл клиента с актуальными параметрами сервера."""
    host = _clean_host(server_host)
    port_m = re.search(r'ListenPort\s*=\s*(\d+)', awg_conf)
    listen_port = port_m.group(1) if port_m else "51820"

    obf_params = _extract_awg_params(awg_conf)
    obf_block = "\n".join(obf_params)

    psk_val = psk
    if not psk_val:
        psk_m = re.search(r'PresharedKey\s*=\s*(\S+)', awg_conf)
        psk_val = psk_m.group(1) if psk_m else None
    psk_line = f"PresharedKey = {psk_val}\n" if psk_val else ""

    conf_text = f"""[Interface]
Address = {client_ip}/32
PrivateKey = {client_priv_key}
DNS = {dns}
MTU = 1280
{obf_block}

[Peer]
PublicKey = {server_pub_key}
{psk_line}Endpoint = {host}:{listen_port}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""
    return conf_text


def _save_conf_to_file(ip: str, alias: str, conf_text: str) -> str:
    """Сохраняет .conf файл в /data/configs/ с правами 0600."""
    try:
        conf_dir = os.path.join(cfg.data_dir, "configs")
        os.makedirs(conf_dir, exist_ok=True)
        safe_alias = re.sub(r'[^a-zA-Z0-9_-]', '_', alias).strip('_') or 'client'
        filepath = os.path.join(conf_dir, f"{ip}_{safe_alias}.conf")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(conf_text)
        os.chmod(filepath, 0o600)
        return filepath
    except Exception as e:
        logger.error(f"Ошибка сохранения файла конфига: {e}")
        return ""


@app.post("/api/user/create")
async def create_user(payload: CreateUserPayload, admin=Depends(require_admin)):
    import subprocess
    import json
    from database import update_user_field
    
    target_cont = None
    for c in cfg.vpn_containers:
        if c["name"] == payload.server_name and c["alias"] == payload.server_alias:
            target_cont = c
            break
            
    if not target_cont:
        raise HTTPException(status_code=400, detail="Server not found")
        
    is_local = target_cont["host"] == "local"
    ssh_prefix = []
    if not is_local:
        port = target_cont["port"] or "22"
        host = target_cont['host']
        if "@" not in host:
            host = f"root@{host}"
        ssh_prefix = ["ssh", "-p", port, "-o", "StrictHostKeyChecking=no", host]
        
    def run_cmd(cmd):
        full_cmd = ssh_prefix + cmd
        res = subprocess.run(full_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise Exception(f"Command failed: {' '.join(full_cmd)} -> {res.stderr}")
        return res.stdout.strip()
        
    try:
        priv_key = run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "genkey"])
        pub_key = subprocess.run(ssh_prefix + ["docker", "exec", "-i", target_cont["name"], "awg", "pubkey"], input=priv_key, capture_output=True, text=True).stdout.strip()
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
        
    try:
        dump = run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "show", target_cont["iface"], "dump"])
        lines = dump.split('\n')[1:]
        used_ips = []
        for line in lines:
            parts = line.split('\t')
            if len(parts) >= 4:
                ips = parts[3].split(',')
                for ip in ips:
                    ip = ip.split('/')[0].strip()
                    if ip and ip != '(none)':
                        used_ips.append(ip)
                        
        awg_conf = run_cmd(["docker", "exec", "-i", target_cont["name"], "cat", "/opt/amnezia/awg/awg0.conf"])
        server_addr_match = re.search(r'Address\s*=\s*([0-9\.]+)/', awg_conf)
        if not server_addr_match:
            raise HTTPException(status_code=500, detail="Could not find server address in awg0.conf")
        server_ip = server_addr_match.group(1)
        base_ip = '.'.join(server_ip.split('.')[:3])
        
        new_ip = None
        for i in range(2, 254):
            test_ip = f"{base_ip}.{i}"
            if test_ip != server_ip and test_ip not in used_ips:
                new_ip = test_ip
                break
                
        if not new_ip:
            raise HTTPException(status_code=500, detail="No free IPs in subnet")
            
        server_psk = None
        try:
            server_psk = run_cmd(["docker", "exec", "-i", target_cont["name"], "cat", "/opt/amnezia/awg/wireguard_psk.key"]).strip()
        except Exception:
            pass

        try:
            run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "set", target_cont["iface"], "peer", pub_key, "preshared-key", "/opt/amnezia/awg/wireguard_psk.key", "allowed-ips", f"{new_ip}/32"])
        except Exception:
            run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "set", target_cont["iface"], "peer", pub_key, "allowed-ips", f"{new_ip}/32"])

        conf_file = "/opt/amnezia/awg/" + target_cont["iface"] + ".conf"
        run_cmd(["docker", "exec", "-i", target_cont["name"], "awg-quick", "save", conf_file])
        
        server_pub_match = re.search(r'PrivateKey\s*=\s*(\S+)', awg_conf)
        server_priv = server_pub_match.group(1)
        server_pub = subprocess.run(ssh_prefix + ["docker", "exec", "-i", target_cont["name"], "awg", "pubkey"], input=server_priv, capture_output=True, text=True).stdout.strip()
        
        clean_host = _clean_host(target_cont["host"])
        
        update_user_field(new_ip, "alias", payload.alias)
        update_user_field(new_ip, "protocol", payload.server_alias)
        update_user_field(new_ip, "speed_limit", "max")
        
        # Генерация чистого текстового .conf файла (для AmneziaWG / WireGuard)
        client_conf_text = _build_client_conf(new_ip, priv_key, awg_conf, server_pub, clean_host, psk=server_psk)
        from database import save_user_config
        save_user_config(new_ip, client_conf_text)
        _save_conf_to_file(new_ip, payload.alias, client_conf_text)

        deep_link = conf_to_amnezia_vpn_link(client_conf_text, payload.alias, payload.server_alias)
        
        return {"ok": True, "link": deep_link, "conf": client_conf_text, "ip": new_ip, "alias": payload.alias}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))



import telebot
import handlers.admin as admin_handler
import handlers.client as client_handler
from tasks import start_scheduler
from vpn_engine import auto_apply_qos

# Инициализация бота внутри FastAPI
bot = telebot.TeleBot(cfg.token, parse_mode=None, threaded=True)
admin_handler.register(bot)
client_handler.register(bot)

@app.on_event("startup")
def on_startup():
    try:
        from database import init_db, log_audit
        init_db()
        start_scheduler(bot)
        auto_apply_qos()
        import threading
        from vpn_engine import get_vpn_stats, get_ru_metrics
        threading.Thread(target=lambda: (get_vpn_stats(force=True), get_ru_metrics()), daemon=True, name="cache_prewarm").start()
        log_audit("WEBAPP_START", details="FastAPI + Webhook + Scheduler started")
        logger.info("FastAPI + Webhook + Scheduler успешно инициализированы!")
    except Exception as e:
        logger.error(f"Startup error: {e}")

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = telebot.types.Update.de_json(data)
        if update:
            bot.process_new_updates([update])
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/api/webhook/status")
async def webhook_status(admin=Depends(require_admin)):
    try:
        info = bot.get_webhook_info()
        return {
            "ok": True,
            "url": info.url,
            "has_custom_certificate": info.has_custom_certificate,
            "pending_update_count": info.pending_update_count,
            "last_error_date": info.last_error_date,
            "last_error_message": info.last_error_message,
            "max_connections": info.max_connections,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/api/webhook/setup")
async def webhook_setup(request: Request, admin=Depends(require_admin)):
    try:
        body = await request.json()
        target_url = body.get("url") or os.getenv("WEBHOOK_URL", "")
        if not target_url:
            raise HTTPException(status_code=400, detail="Missing url")
        target_url = target_url.strip().rstrip("/")
        full_url = f"{target_url}/webhook/telegram"
        bot.remove_webhook()
        time.sleep(0.5)
        res = bot.set_webhook(url=full_url, max_connections=40)
        return {"ok": res, "webhook_url": full_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/{path:path}")
async def catch_all(path: str):
    file_path = os.path.join(static_dir, path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return FileResponse(
        os.path.join(static_dir, "index.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )
