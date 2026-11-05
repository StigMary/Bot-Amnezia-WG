"""
webapp/main.py — Главная точка входа FastAPI бэкенда для Telegram Mini App.
Модульная архитектура: маршруты разделены по APIRouter в webapp/routers/.
"""
import hmac
import os
import sys
import threading
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import telebot

# Добавляем корневую директорию проекта в sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config import cfg, logger
import handlers.admin as admin_handler
import handlers.client as client_handler
from tasks import start_scheduler
from vpn_engine import auto_apply_qos

from webapp.auth import require_admin, _verify_init_data
from webapp.metrics import (
    _async_ru_metrics,
    _async_se_metrics,
    _async_vpn_stats,
    _cache_get,
    _cache_set,
    _cached_cpu,
    _cached_vpn_stats,
    get_local_ru_metrics,
    get_remote_se_metrics,
)
from webapp.schemas import (
    BindPayload,
    BroadcastPayload,
    BulkActionPayload,
    CreateUserPayload,
    ExtendPayload,
    RegisterPayload,
    RenamePayload,
    SpeedPayload,
    TicketReplyPayload,
)
from webapp.utils import (
    _build_amnezia_vpn_link,
    _build_client_conf,
    _clean_host,
    _extract_awg_params,
    _generate_qr_b64,
    _save_conf_to_file,
    conf_to_amnezia_vpn_link,
)

# Импорт модульных роутеров
from webapp.routers import (
    broadcast_router,
    dashboard_router,
    servers_router,
    settings_router,
    subscribers_router,
    tickets_router,
    waiting_room_router,
)

app = FastAPI(title="VPN Admin WebApp", docs_url=None, redoc_url=None)

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://t.me", "https://web.telegram.org", "http://127.0.0.1", "localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение модульных маршрутов
app.include_router(dashboard_router)
app.include_router(subscribers_router)
app.include_router(waiting_room_router)
app.include_router(servers_router)
app.include_router(settings_router)
app.include_router(tickets_router)
app.include_router(broadcast_router)

# Статические файлы WebApp
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


# ─── Инициализация Telegram Бота и Webhook ───────────────────────────────────

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
        from vpn_engine import get_ru_metrics, get_vpn_stats
        threading.Thread(
            target=lambda: (get_vpn_stats(force=True), get_ru_metrics()),
            daemon=True,
            name="cache_prewarm"
        ).start()
        log_audit("WEBAPP_START", details="FastAPI + Webhook + Scheduler started")
        logger.info("FastAPI + Webhook + Scheduler успешно инициализированы!")
    except Exception as e:
        logger.error(f"Startup error: {e}")


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    secret = os.getenv("WEBHOOK_SECRET", "")
    if secret:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got, secret):
            raise HTTPException(status_code=403, detail="Forbidden")
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
        res = bot.set_webhook(
            url=full_url,
            max_connections=40,
            secret_token=os.getenv("WEBHOOK_SECRET") or None,
        )
        return {"ok": res, "webhook_url": full_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/{path:path}")
async def catch_all(path: str):
    if path.startswith("api/") or path == "api":
        raise HTTPException(status_code=404, detail="Not Found")
    base = os.path.realpath(static_dir)
    candidate = os.path.realpath(os.path.join(base, path))
    if os.path.commonpath([base, candidate]) == base and os.path.isfile(candidate):
        return FileResponse(candidate)
    return FileResponse(
        os.path.join(static_dir, "index.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )
