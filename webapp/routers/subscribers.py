"""
webapp/routers/subscribers.py — Управление абонентами: список, карточка, конфиги, скорость, продление, создание, удаление.
"""
from datetime import datetime
import os
import re
import subprocess

from fastapi import APIRouter, Depends, HTTPException
import telebot

from config import cfg, logger
from database import (
    bind_tg_to_ip,
    clear_billing_notified,
    delete_user,
    extend_paid_until,
    get_all_users,
    get_user,
    get_user_by_ip,
    get_user_config,
    log_audit,
    save_user_config,
    update_user_field,
)
from vpn_engine import (
    apply_limit,
    auto_apply_qos,
    delete_peer,
    sanitize_alias,
    sync_db_with_vpn_peers,
    validate_vpn_ip,
)
from webapp.auth import require_admin
from webapp.metrics import _async_vpn_stats, invalidate_cache
from webapp.schemas import (
    BindPayload,
    BulkActionPayload,
    CreateUserPayload,
    ExtendPayload,
    RenamePayload,
    SpeedPayload,
)
from webapp.utils import (
    _build_client_conf,
    _clean_host,
    _delete_conf_files,
    _generate_qr_b64,
    _save_conf_to_file,
    conf_to_amnezia_vpn_link,
)

router = APIRouter(tags=["subscribers"])


def _invalidate_all_caches() -> None:
    """Мгновенно сбрасывает кэши в памяти (metrics и peers)."""
    try:
        from vpn.peers import invalidate_stats_cache
        invalidate_stats_cache()
    except Exception:
        pass
    try:
        invalidate_cache(["users", "vpn_stats", "dashboard"])
    except Exception:
        pass


@router.get("/api/users")
async def users_list(admin=Depends(require_admin)):
    stats = await _async_vpn_stats()
    db = get_all_users()
    result = []
    seen_ips = set()

    for u in stats:
        ip = u.get("ip")
        if not ip:
            continue
        seen_ips.add(ip)
        server_alias = u.get("server_alias") or "Неизвестно"

        db_data = db.get(ip, {})
        alias = db_data.get("alias") or u.get("db_alias") or "Новый пользователь"
        speed_limit = db_data.get("speed_limit") or u.get("speed_limit") or "max"

        paid = db_data.get("paid_until", "")
        paid_display = ""
        days_left = None
        if paid:
            try:
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
        if ip in seen_ips:
            continue
        seen_ips.add(ip)
        server_alias = db_data.get("protocol") or "Неизвестно"
        paid = db_data.get("paid_until", "")
        paid_display = ""
        days_left = None
        if paid:
            try:
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


@router.get("/api/user/{ip}")
async def user_detail(ip: str, server: str = "", admin=Depends(require_admin)):
    if not validate_vpn_ip(ip.replace("-", ".")):
        raise HTTPException(status_code=400, detail="Invalid IP")
    ip = ip.replace("-", ".")
    user = get_user_by_ip(ip)
    stats = await _async_vpn_stats()
    stat = None
    if server:
        stat = next((s for s in stats if s["ip"] == ip and (s.get("server_alias") == server or server in s.get("server_alias", "") or s.get("server_alias", "") in server)), None)
    if not stat:
        stat = next((s for s in stats if s["ip"] == ip), None)
    if not user and not stat:
        raise HTTPException(status_code=404, detail="User not found")

    paid = user["paid_until"] if user and "paid_until" in user.keys() else ""
    paid_display = ""
    days_left = None
    if paid:
        try:
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
        "server": server or (stat["server_alias"] if stat else (user["protocol"] if user and "protocol" in user.keys() else "")),
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


@router.get("/api/user/{ip}/config")
async def get_user_conf_endpoint(ip: str, server: str = "", admin=Depends(require_admin)):
    """Возвращает выданный .conf файл абонента с учётом сервера (SE / RU)."""
    user_row = get_user(ip)
    target_server = server or (user_row["protocol"] if user_row and "protocol" in user_row.keys() else "")
    user = dict(user_row) if user_row else {"alias": "Абонент", "protocol": target_server or "VPN"}

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

    # 2. Если не найден с фильтром и сервер не был строго задан
    if not conf_text and not server and os.path.exists(conf_dir):
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
        link = conf_to_amnezia_vpn_link(conf_text, user.get("alias", "Абонент"), target_server or user.get("protocol", "VPN"))

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
        "protocol": target_server or user.get("protocol", "")
    }


@router.post("/api/user/{ip}/config/regenerate")
async def regenerate_user_conf(ip: str, admin=Depends(require_admin)):
    """Перевыпускает ключи и конфиг для абонента на соответствующем сервере."""
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


@router.post("/api/user/{ip}/speed")
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
    _invalidate_all_caches()
    log_audit("SET_SPEED", ip, f"speed={payload.speed},via=webapp", cfg.admin_id)
    return {"ok": True}


@router.post("/api/user/{ip}/bind")
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
    try:
        from database import remove_pending_tg_user
        remove_pending_tg_user(int(target_tg_id))
    except Exception:
        pass
    _invalidate_all_caches()
    log_audit("BIND", ip, f"tg_id={target_tg_id},via=webapp", cfg.admin_id)
    return {"ok": True, "tg_id": target_tg_id}


@router.post("/api/user/{ip}/unbind")
async def unbind_user(ip: str, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    update_user_field(ip, "tg_user_id", None)
    _invalidate_all_caches()
    log_audit("UNBIND", ip, "via=webapp", cfg.admin_id)
    return {"ok": True}


@router.post("/api/user/{ip}/rename")
async def rename_user(ip: str, payload: RenamePayload, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    clean = sanitize_alias(payload.alias)
    update_user_field(ip, "alias", clean)
    _invalidate_all_caches()
    log_audit("RENAME", ip, f"alias={clean},via=webapp", cfg.admin_id)
    return {"ok": True, "alias": clean}


@router.post("/api/user/{ip}/extend")
async def extend_user(ip: str, payload: ExtendPayload, admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    
    if payload.lifetime:
        update_user_field(ip, "paid_until", "2099-12-31 23:59:59")
        user = get_user_by_ip(ip)
        if user and user.get("tg_user_id"):
            clear_billing_notified(user["tg_user_id"])
        _invalidate_all_caches()
        log_audit("EXTEND", ip, "lifetime=true,via=webapp", cfg.admin_id)
        return {"ok": True, "paid_until": "2099-12-31 23:59:59"}
    elif payload.date:
        try:
            d = datetime.strptime(payload.date, "%Y-%m-%d")
            new_date = d.strftime("%Y-%m-%d 23:59:59")
            update_user_field(ip, "paid_until", new_date)
            user = get_user_by_ip(ip)
            if user and user.get("tg_user_id"):
                clear_billing_notified(user["tg_user_id"])
            _invalidate_all_caches()
            log_audit("EXTEND", ip, f"exact_date={new_date},via=webapp", cfg.admin_id)
            return {"ok": True, "paid_until": new_date}
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
    else:
        new_date = extend_paid_until(ip, payload.days)
        _invalidate_all_caches()
        log_audit("EXTEND", ip, f"days={payload.days},new={new_date},via=webapp", cfg.admin_id)
        return {"ok": True, "paid_until": new_date}


@router.post("/api/users/sync")
async def sync_users(admin=Depends(require_admin)):
    res = sync_db_with_vpn_peers()
    auto_apply_qos()
    _invalidate_all_caches()
    log_audit("SYNC_PEERS", None, f"added={res.get('added', 0)}", cfg.admin_id)
    return {"ok": True, "added": res.get("added", 0), "total": res.get("total", 0)}


@router.post("/api/users/bulk")
async def users_bulk_action(payload: BulkActionPayload, admin=Depends(require_admin)):
    success = 0
    errors = 0
    
    # Для рассылки
    if payload.action == 'message':
        bot = telebot.TeleBot(cfg.token)
        text = payload.data.get('text', '')
        
        for ip in payload.ips:
            user = get_user_by_ip(ip)
            if user and user.get("tg_user_id"):
                try:
                    bot.send_message(user["tg_user_id"], text)
                    success += 1
                except Exception:
                    errors += 1
            else:
                errors += 1
        
        log_audit("BULK_MSG", None, f"to {len(payload.ips)} users", cfg.admin_id)
        return {"ok": True, "msg": f"Успешно: {success}, Ошибок: {errors}"}
        
    for ip in payload.ips:
        if not validate_vpn_ip(ip):
            errors += 1
            continue
        try:
            if payload.action == 'extend':
                days = payload.data.get('days', 0)
                if days == 0:
                    update_user_field(ip, "paid_until", "2099-12-31 23:59:59")
                    user = get_user_by_ip(ip)
                    if user and user.get("tg_user_id"):
                        clear_billing_notified(user["tg_user_id"])
                else:
                    extend_paid_until(ip, days)
                success += 1
            elif payload.action == 'delete':
                user = get_user_by_ip(ip)
                server = user["protocol"] if user and "protocol" in user.keys() else ""
                delete_peer(ip, server)
                _delete_conf_files(ip, server)
                delete_user(ip)
                success += 1
            elif payload.action == 'speed':
                speed = payload.data.get('speed', 'max')
                user = get_user_by_ip(ip)
                server = user["protocol"] if user and "protocol" in user.keys() else ""
                update_user_field(ip, "speed_limit", speed)
                apply_limit(ip, speed, server)
                success += 1
        except Exception:
            errors += 1
    
    _invalidate_all_caches()
    log_audit("BULK_ACTION", None, f"Action '{payload.action}' on {len(payload.ips)} users", cfg.admin_id)
    return {"ok": True, "msg": f"Success: {success}, Errors: {errors}"}


@router.post("/api/user/{ip}/delete")
async def delete_user_endpoint(ip: str, server: str = "", admin=Depends(require_admin)):
    ip = ip.replace("-", ".")
    if not validate_vpn_ip(ip):
        raise HTTPException(status_code=400, detail="Invalid IP")
    user = get_user_by_ip(ip)
    target_server = server or (user["protocol"] if user and "protocol" in user.keys() else "")
    delete_peer(ip, target_server)
    _delete_conf_files(ip, target_server)
    delete_user(ip)
    _invalidate_all_caches()
    log_audit("DELETE_PEER", ip, f"server={target_server},via=webapp", cfg.admin_id)
    return {"ok": True}


@router.get("/api/vpn_containers")
async def get_vpn_containers(admin=Depends(require_admin)):
    servers = [c for c in cfg.vpn_containers if c.get("name")]
    return servers


@router.post("/api/user/create")
async def create_user(payload: CreateUserPayload, admin=Depends(require_admin)):
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
        logger.error(f"Error generating keys for peer: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
    try:
        # Собираем все занятые IP из БД и всех нод для исключения конфликтов адресации
        used_ips = set()
        try:
            db_all = get_all_users()
            used_ips.update(db_all.keys())
        except Exception as e:
            logger.debug(f"Could not read db users for IP allocation: {e}")

        # Целевой контейнер
        dump = run_cmd(["docker", "exec", "-i", target_cont["name"], "awg", "show", target_cont["iface"], "dump"])
        for line in dump.split('\n')[1:]:
            parts = line.split('\t')
            if len(parts) >= 4:
                for ip_str in parts[3].split(','):
                    ip_clean = ip_str.split('/')[0].strip()
                    if ip_clean and ip_clean != '(none)':
                        used_ips.add(ip_clean)

        # Опрашиваем остальные контейнеры
        for cont in cfg.vpn_containers:
            if cont["name"] == target_cont["name"] and cont["host"] == target_cont["host"]:
                continue
            try:
                c_dump = run_vpn_cmd(cont, [_wg_cmd(cont), "show", _wg_iface(cont), "dump"])
                if c_dump and c_dump.stdout:
                    for line in c_dump.stdout.split('\n')[1:]:
                        parts = line.split('\t')
                        if len(parts) >= 4:
                            for ip_str in parts[3].split(','):
                                ip_clean = ip_str.split('/')[0].strip()
                                if ip_clean and ip_clean != '(none)':
                                    used_ips.add(ip_clean)
            except Exception:
                pass
                        
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
            raise HTTPException(status_code=500, detail="No free IPs in subnet across all nodes")
            
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
        save_user_config(new_ip, client_conf_text)
        _save_conf_to_file(new_ip, payload.alias, client_conf_text, payload.server_alias)
        _invalidate_all_caches()

        deep_link = conf_to_amnezia_vpn_link(client_conf_text, payload.alias, payload.server_alias)
        
        return {"ok": True, "link": deep_link, "conf": client_conf_text, "ip": new_ip, "alias": payload.alias}
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise HTTPException(status_code=500, detail=str(e))
