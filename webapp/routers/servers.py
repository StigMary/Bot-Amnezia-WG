"""
webapp/routers/servers.py — Управление службами и действиями над серверами (рестарт контейнеров, x-ui, xray, ботов, перезагрузка, ping).
"""
import os
import subprocess
import threading
import time

from fastapi import APIRouter, Depends, HTTPException

from config import cfg
from database import log_audit
from vpn_engine import auto_apply_qos
from webapp.auth import require_admin

router = APIRouter(tags=["servers"])

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


@router.get("/api/server/{side}/services")
async def get_server_services_endpoint(side: str, admin=Depends(require_admin)):
    side_u = side.upper()
    services = []

    # 1. Поиск ноды в cfg.vpn_containers
    target = next(
        (
            c for c in cfg.vpn_containers
            if (c.get("id") and c["id"].upper() == side_u)
            or (c.get("alias") and c["alias"].upper() == side_u)
            or (c.get("id") and side_u in c["id"].upper())
            or (c.get("alias") and side_u in c["alias"].upper())
        ),
        None,
    )

    if target:
        c_name = target.get("name") or "amnezia-awg2"
        try:
            if target.get("host") == "local":
                st = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", c_name], text=True).strip()
            else:
                user = target.get("user") or "root"
                dest = f"{user}@{target['host']}" if user and "@" not in target["host"] else target["host"]
                st = subprocess.check_output(["ssh", "-p", str(target.get("port") or 22), "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", dest, f"docker inspect --format '{{{{.State.Status}}}}' {c_name}"], text=True).strip()
            services.append({"name": f"AmneziaWG ({c_name})", "status": st, "action": "restart_container", "action_label": "🔄 Рестарт"})
        except Exception:
            services.append({"name": f"AmneziaWG ({c_name})", "status": "stopped", "action": "restart_container", "action_label": "🔄 Запуск"})

    if side_u == "RU" or "RU" in side_u or (target and target.get("host") == "local"):
        # Systemd: xray
        try:
            st = subprocess.check_output(["systemctl", "is-active", "xray"], text=True).strip()
            services.append({"name": "Xray Core", "status": st, "action": "restart_xray", "action_label": "🔄 Рестарт"})
        except Exception:
            pass

        # Systemd: bot (vpn-bot или msk_bot)
        bot_svc = "vpn-bot"
        try:
            st = subprocess.check_output(["systemctl", "is-active", bot_svc], text=True).strip()
        except Exception:
            bot_svc = "msk_bot"
            try:
                st = subprocess.check_output(["systemctl", "is-active", bot_svc], text=True).strip()
            except Exception:
                st = "inactive"
        services.append({"name": "Telegram-бот", "status": st, "action": "restart_bot", "action_label": "🔄 Рестарт"})

        # Systemd: cloudflared
        try:
            st = subprocess.check_output(["systemctl", "is-active", "cloudflared"], text=True).strip()
            services.append({"name": "Cloudflare Tunnel", "status": st, "action": None, "action_label": None})
        except Exception:
            pass

    elif (side_u == "SE" or "SE" in side_u) and not target:
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


@router.post("/api/server/{side}/{action}")
async def server_action(side: str, action: str, admin=Depends(require_admin)):
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
