"""
vpn/peers.py — Управление пирами WireGuard/AmneziaWG, сбор статистики сессий и синхронизация с БД.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import re
import threading
import time
from typing import Any

from config import cfg, logger
from database import get_all_users, update_user_field, upsert_users_batch
from vpn.transport import format_bytes, get_online_emoji, run_vpn_cmd, validate_vpn_ip

# ─── Кэш статистики и имен ───────────────────────────────────────────────────

_stats_cache: list[dict] = []
_stats_cache_ts: float = 0.0
_stats_cache_ttl: float = 30.0  # кэш на 30 секунд
_stats_lock = threading.Lock()

_names_cache: dict[str, dict] = {}  # container_name -> {data: dict, expires: float}
_names_lock = threading.Lock()


def _wg_cmd(cont: dict) -> str:
    """Имя бинарника WG: 'awg' (AmneziaWG) или 'wg'. Конфигурируется через cont['wg_cmd']."""
    return cont.get("wg_cmd", "awg")


def _wg_iface(cont: dict) -> str:
    """Имя WG-интерфейса в контейнере. Конфигурируется через cont['wg_iface']."""
    return cont.get("wg_iface", "awg0")


def _get_amnezia_client_names(cont: dict) -> dict[str, str]:
    """Считывает имена клиентов из /opt/amnezia/awg/clientsTable с кэшированием на 60 секунд."""
    c_name = cont.get("name", "amnezia-awg2")
    now = time.time()

    with _names_lock:
        cached = _names_cache.get(c_name)
        if cached and now < cached["expires"]:
            return cached["data"]

    names = {}
    try:
        res = run_vpn_cmd(cont, ["cat", "/opt/amnezia/awg/clientsTable"], capture=True, timeout=8)
        if res and res.returncode == 0 and res.stdout:
            table = json.loads(res.stdout)
            for item in table:
                cid = item.get("clientId", "").strip()
                ud = item.get("userData", {})
                name = ud.get("clientName", "").strip()
                ip = ud.get("allowed_ips", "").split("/")[0].strip()
                if name:
                    if cid:
                        names[cid] = name
                    if ip:
                        names[ip] = name
    except Exception as e:
        logger.debug(f"Error reading clientsTable on {c_name}: {e}")

    with _names_lock:
        _names_cache[c_name] = {"data": names, "expires": now + 60.0}
    return names


def _poll_container(cont: dict, db_users: dict) -> tuple:
    """Опрашивает один контейнер. Возвращает (entries, new_users)."""
    entries, new_users = [], []
    amnezia_names = _get_amnezia_client_names(cont)
    res = run_vpn_cmd(cont, [_wg_cmd(cont), "show", "all", "dump"])
    if not res or res.returncode != 0:
        return entries, new_users

    for line in res.stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 8 or parts[4] == "(none)":
            continue
        ip = parts[4].split("/")[0]
        if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip) or not validate_vpn_ip(ip):
            continue
        handshake, rx, tx = int(parts[5]), int(parts[6]), int(parts[7])
        endpoint = parts[3] if len(parts) > 3 else "(none)"
        pkey = parts[1]
        name_from_amnezia = amnezia_names.get(pkey) or amnezia_names.get(ip)

        user_data = db_users.get(ip)
        if not user_data:
            initial_alias = name_from_amnezia or "Новый пользователь"
            new_users.append((ip, cont["alias"]))
            user_data = {"alias": initial_alias, "speed_limit": "max", "protocol": cont["alias"]}
            try:
                update_user_field(ip, "alias", initial_alias)
                update_user_field(ip, "protocol", cont["alias"])
            except Exception as e:
                logger.debug(f"User field update error: {e}")
        else:
            if not user_data.get("protocol"):
                try:
                    update_user_field(ip, "protocol", cont["alias"])
                    user_data["protocol"] = cont["alias"]
                except Exception as e:
                    logger.debug(f"Protocol field update error: {e}")
            if name_from_amnezia and (user_data.get("alias") == "Новый пользователь" or not user_data.get("alias")):
                user_data["alias"] = name_from_amnezia
                try:
                    update_user_field(ip, "alias", name_from_amnezia)
                except Exception as e:
                    logger.debug(f"Alias field update error: {e}")

        entries.append({
            "ip": ip,
            "server_alias": cont["alias"],
            "db_alias": user_data["alias"],
            "speed_limit": user_data["speed_limit"],
            "last_seen": "Никогда" if handshake == 0 else datetime.fromtimestamp(handshake).strftime("%d.%m %H:%M"),
            "handshake_ts": handshake,
            "endpoint": endpoint,
            "online_emoji": get_online_emoji(handshake),
            "downloaded": format_bytes(tx),
            "uploaded": format_bytes(rx),
            "total_bytes": rx + tx,
            "raw_rx": rx,
            "raw_tx": tx,
            "tg_user_id": user_data.get("tg_user_id", ""),
            "paid_until": user_data.get("paid_until", ""),
        })
    return entries, new_users


def get_vpn_stats(force: bool = False) -> list[dict[str, Any]]:
    """Собирает статистику VPN параллельно и кэширует на 30 сек."""
    global _stats_cache, _stats_cache_ts

    with _stats_lock:
        if not force and time.time() - _stats_cache_ts < _stats_cache_ttl:
            return _stats_cache

    db_users = get_all_users()
    stats: list[dict] = []
    new_users: list[tuple] = []

    with ThreadPoolExecutor(max_workers=max(1, len(cfg.vpn_containers))) as pool:
        futures = {pool.submit(_poll_container, cont, db_users): cont for cont in cfg.vpn_containers}
        for fut in as_completed(futures):
            try:
                entries, nu = fut.result()
                stats.extend(entries)
                new_users.extend(nu)
            except Exception as e:
                logger.error(f"Ошибка опроса контейнера: {e}")

    if new_users:
        upsert_users_batch(new_users)

    with _stats_lock:
        responded_servers = {s["server_alias"] for s in stats}
        for old in _stats_cache:
            if old.get("server_alias") not in responded_servers:
                stats.append(old)
        result = sorted(stats, key=lambda x: x["total_bytes"], reverse=True)
        _stats_cache = result
        _stats_cache_ts = time.time()
    return result


def delete_peer(ip: str, server_alias: str = "") -> None:
    """Удаляет пира из WireGuard-интерфейса и сохраняет конфиг."""
    target = next((c for c in cfg.vpn_containers if c["alias"] == server_alias), None)
    targets = [target] if target else list(cfg.vpn_containers)

    for t in targets:
        wg = _wg_cmd(t)
        iface = _wg_iface(t)

        res = run_vpn_cmd(t, [wg, "show", iface, "dump"])
        if not res or not res.stdout:
            continue

        pub_key = None
        for line in res.stdout.split("\n"):
            if f"{ip}/32" in line:
                pub_key = line.split("\t")[0]
                break

        if pub_key:
            run_vpn_cmd(t, [wg, "set", iface, "peer", pub_key, "remove"])
            run_vpn_cmd(t, [f"{wg}-quick", "save", iface])


def sync_db_with_vpn_peers() -> dict[str, Any]:
    """Синхронизирует БД с реальными пирами в WireGuard/AmneziaWG."""
    from database import delete_user, get_conn

    current_db_users = get_all_users()
    active_vpn_ips = {}
    successful_servers = set()

    for target in cfg.vpn_containers:
        try:
            wg = _wg_cmd(target)
            res = run_vpn_cmd(target, [wg, "show", target["iface"], "allowed-ips"], capture=True, timeout=15)
            if res and res.returncode == 0 and res.stdout:
                successful_servers.add(target["alias"])
                for line in res.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        peer_ip = parts[1].split("/")[0]
                        if validate_vpn_ip(peer_ip):
                            active_vpn_ips[peer_ip] = target["alias"]
            else:
                logger.warning(f"Не удалось получить пиры с {target['alias']}")
        except Exception as e:
            logger.error(f"Ошибка получения пиров с {target['alias']}: {e}")

    added = 0
    cleaned = 0

    # 1. Добавляем новые пиры
    for ip, server_alias in active_vpn_ips.items():
        if ip not in current_db_users:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO users (ip_address, alias, protocol, speed_limit) VALUES (?, ?, ?, 'max')",
                    (ip, f"Абонент {ip.split('.')[-1]}", server_alias)
                )
            added += 1
        elif not current_db_users[ip].get("protocol"):
            update_user_field(ip, "protocol", server_alias)

    # 2. Удаляем фантомные пиры
    for ip, data in current_db_users.items():
        proto = data.get("protocol")
        if proto not in successful_servers:
            continue
        if ip not in active_vpn_ips:
            if not ip.endswith(".0") and not ip.endswith(".255"):
                delete_user(ip)
                cleaned += 1

    logger.info(f"Синхронизация БД с VPN: добавлено новых={added}, удалено фантомных={cleaned}")
    return {"ok": True, "added": added, "cleaned": cleaned, "total_active": len(active_vpn_ips)}
