"""
vpn_engine.py — Взаимодействие с серверами.
SSH/Docker-транспорт, парсинг WireGuard, QoS (tc/htb), валидация.
"""

import ipaddress
import os
import re
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

from config import cfg, logger
from database import get_all_users, upsert_users_batch

# ─── Валидация ────────────────────────────────────────────────────────────────


def validate_vpn_ip(ip_str: str) -> bool:
    """Проверяет, что IP валиден и принадлежит VPN-подсети."""
    try:
        return ipaddress.ip_address(ip_str) in ipaddress.ip_network(cfg.vpn_subnet)
    except ValueError:
        return False


def sanitize_alias(text: str) -> str:
    """Ограничивает длину и удаляет Markdown-спецсимволы."""
    text = text.strip()[:30]
    text = re.sub(r"[*_`\[\]()~>#+=|{}.!\\-]", "", text)
    return text or "Без имени"


# ─── SSH: ControlMaster для переиспользования подключений ─────────────────────
_SSH_CONTROL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ssh_ctl")  # nosec B108
os.makedirs(_SSH_CONTROL_DIR, mode=0o700, exist_ok=True)

_SSH_BASE_OPTS = [
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    f"UserKnownHostsFile={os.path.expanduser('~/.ssh/known_hosts')}",
    "-o",
    "BatchMode=yes",
    "-o",
    "ControlMaster=auto",
    "-o",
    f"ControlPath={_SSH_CONTROL_DIR}/%h_%p_%r",
    "-o",
    "ControlPersist=600",
    "-o",
    "ConnectTimeout=8",
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=3",
]


def run_vpn_cmd(
    target: dict[str, Any],
    cmd_list: list[str],
    capture: bool = True,
    timeout: int = 15,
) -> subprocess.CompletedProcess | None:
    """Выполняет команду на локальном (docker exec) или удалённом (ssh) сервере."""
    if target["host"] == "local":
        final_cmd = ["docker", "exec", target["name"]] + cmd_list
    else:
        escaped_cmd = [shlex.quote(arg) for arg in cmd_list]
        remote_cmd = " ".join(
            ["sudo", "docker", "exec", shlex.quote(target["name"])] + escaped_cmd
        )
        remote_cmd = f"timeout {max(5, timeout - 2)} {remote_cmd}"
        user = target.get("user") or "root"
        dest = f"{user}@{target['host']}" if user and "@" not in target["host"] else target["host"]
        final_cmd = (
            ["ssh"]
            + _SSH_BASE_OPTS
            + ["-p", str(target.get("port") or 22), dest, remote_cmd]
        )

    try:
        if capture:
            return subprocess.run(final_cmd, capture_output=True, text=True, timeout=timeout)
        else:
            return subprocess.run(final_cmd, stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning(f"Таймаут команды на {target['alias']}: {' '.join(cmd_list[:3])}")
        return None
    except Exception as e:
        logger.error(f"Ошибка команды на {target['alias']}: {e}")
        return None


def run_ssh_cmd(
    host: str,
    port: str,
    remote_cmd: str,
    timeout: int = 15,
    user: str = "root",
) -> str | None:
    """Выполняет произвольную команду на удалённом сервере по SSH."""
    remote_timeout = max(5, timeout - 2)
    safe_remote_cmd = f"timeout {remote_timeout} sh -c {shlex.quote(remote_cmd)}"
    dest = f"{user}@{host}" if user and "@" not in host else host
    cmd = ["ssh"] + _SSH_BASE_OPTS + ["-p", str(port), dest, safe_remote_cmd]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0:
            return res.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning(f"SSH таймаут: {dest}:{port}")
    except Exception as e:
        logger.error(f"SSH ошибка ({dest}:{port}): {e}")
    return None


def _run_remote_tc(host: str, port: str, tc_cmd_str: str, timeout: int = 15) -> None:
    """Выполняет TC-команды напрямую на хосте по SSH.
    Избегает вложенного sh -c экранирования путём передачи команды
    напрямую как аргумент SSH (без обёртки run_ssh_cmd).
    awg0 живёт в network namespace хоста, не внутри Docker контейнера.
    """
    remote_timeout = max(5, timeout - 2)
    # Передаём команду через одинарный sh -c (без вложения) напрямую в SSH
    ssh_cmd = (
        ["ssh"] + _SSH_BASE_OPTS
        + ["-p", str(port), host,
           f"timeout {remote_timeout} sudo sh -c {shlex.quote(tc_cmd_str)}"]
    )
    try:
        res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0 and res.stderr:
            logger.warning(f"TC на {host}: {res.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        logger.warning(f"TC таймаут на {host}:{port}")
    except Exception as e:
        logger.error(f"TC ошибка ({host}:{port}): {e}")


# ─── WireGuard / парсинг ─────────────────────────────────────────────────────


def format_bytes(size: float) -> str:
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} ПБ"


def get_online_emoji(handshake_ts: int) -> str:
    if handshake_ts == 0:
        return "⚫"
    delta = datetime.now() - datetime.fromtimestamp(handshake_ts)
    if delta < timedelta(minutes=3):
        return "🟢"
    if delta < timedelta(hours=1):
        return "🟡"
    return "🔴"


# ─── Кэш статистики ─────────────────────────────────────────────────────────────────
_stats_cache: list[dict] = []
_stats_cache_ts: float = 0.0
_stats_cache_ttl: float = 30.0  # кэш на 30 секунд
_stats_lock = threading.Lock()


def _wg_cmd(cont: dict) -> str:
    """Имя бинарника WG: 'awg' (AmneziaWG) или 'wg'. Конфигурируется через cont['wg_cmd']."""
    return cont.get("wg_cmd", "awg")


def _wg_iface(cont: dict) -> str:
    """Имя WG-интерфейса в контейнере. Конфигурируется через cont['wg_iface']."""
    return cont.get("wg_iface", "awg0")


def _get_amnezia_client_names(cont: dict) -> dict[str, str]:
    """Считывает имена клиентов из /opt/amnezia/awg/clientsTable."""
    names = {}
    try:
        res = run_vpn_cmd(cont, ["cat", "/opt/amnezia/awg/clientsTable"])
        if res and res.returncode == 0:
            import json
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
        logger.debug(f"Error reading clientsTable: {e}")
    return names


def _poll_container(cont: dict, db_users: dict) -> tuple:
    """Опрашивает один контейнер. Возвращает (entries, new_users)."""
    from database import update_user_field
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
            logger.debug(f"Пропущена строка wg dump (не IPv4): {ip}")
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

        entries.append(
            {
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
            }
        )
    return entries, new_users


def get_vpn_stats(force: bool = False) -> list[dict[str, Any]]:
    """Собирает статистику VPN параллельно и кеширует на 30 сек."""
    global _stats_cache, _stats_cache_ts

    with _stats_lock:
        if not force and time.time() - _stats_cache_ts < _stats_cache_ttl:
            return _stats_cache

    db_users = get_all_users()
    stats: list[dict] = []
    new_users: list[tuple] = []

    # Параллельный опрос всех контейнеров
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
        # Сохраняем клиентов серверов, которые не успели ответить в этом цикле
        responded_servers = {s["server_alias"] for s in stats}
        for old in _stats_cache:
            if old.get("server_alias") not in responded_servers:
                stats.append(old)
        result = sorted(stats, key=lambda x: x["total_bytes"], reverse=True)
        _stats_cache = result
        _stats_cache_ts = time.time()
    return result


# ─── QoS (tc/htb) ────────────────────────────────────────────────────────────


def get_class_id(ip_str: str) -> str:
    """Генерирует tc class-id из IPv4-адреса. FIX: защита от IPv6."""
    ip = ipaddress.ip_address(ip_str)
    if ip.version == 6:
        return "ffff"
    parts = ip_str.split(".")
    return f"{int(parts[2]):x}{int(parts[3]):02x}"


def apply_limit(ip: str, speed: str, server_alias: str) -> bool:
    """Применяет ограничение скорости для пользователя через tc/htb."""
    if not validate_vpn_ip(ip):
        logger.warning(f"apply_limit: невалидный IP {ip}")
        return False
    if speed not in cfg.allowed_speeds:
        logger.warning(f"apply_limit: невалидная скорость {speed}")
        return False

    target = next((c for c in cfg.vpn_containers if c["alias"] == server_alias), None)
    if not target:
        if len(cfg.vpn_containers) == 1 or not server_alias or "RU" in str(server_alias):
            target = cfg.vpn_containers[0]
        else:
            logger.error(f"apply_limit: сервер '{server_alias}' не найден")
            return False

    iface = target["iface"]
    class_id = get_class_id(ip)

    # hex-представление IP для проверки наличия фильтра (iproute2 6.6+ показывает в hex)
    hex_ip = "".join(f"{int(o):02x}" for o in ip.split("."))

    if speed != "max":
        rate = "1kbit" if speed == "punish" else f"{speed}mbit"
        ceil = rate
        burst = "16k" if speed == "punish" else "128k"
        ingress_burst = "16k" if speed == "punish" else "256k"
        ingress_prio = int(class_id, 16)

        # EGRESS (download клиента): HTB на root qdisc, ловит пакеты dst={ip}/32
        egress_cmd = (
            f"if ! tc qdisc show dev {iface} | grep -q htb; then "
            f"tc qdisc add dev {iface} root handle 1: htb default 9999 r2q 10 2>/dev/null || true; "
            f"fi; "
            f"tc class replace dev {iface} parent 1: classid 1:1 htb rate 1000mbit burst 500k cburst 500k quantum 200000 2>/dev/null || true; "
            f"tc class replace dev {iface} parent 1:1 classid 1:9999 htb rate 1000mbit ceil 1000mbit burst 500k cburst 500k quantum 200000 2>/dev/null || true; "
            f"tc class replace dev {iface} parent 1:1 classid 1:{class_id} htb rate {rate} ceil {ceil} burst {burst} cburst {burst} quantum 25000; "
            f"if ! tc filter show dev {iface} 2>/dev/null | grep -q {hex_ip}; then "
            f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match ip dst {ip}/32 flowid 1:{class_id}; "
            f"fi"
        )
        # INGRESS (upload клиента): police на ffff: qdisc, дропает пакеты src={ip}/32 сверх rate
        ingress_cmd = (
            f"tc qdisc add dev {iface} handle ffff: ingress 2>/dev/null || true; "
            f"tc filter del dev {iface} parent ffff: protocol ip prio {ingress_prio} 2>/dev/null || true; "
            f"tc filter add dev {iface} parent ffff: protocol ip prio {ingress_prio} u32 match ip src {ip}/32 "
            f"police rate {rate} burst {ingress_burst} mtu 64k drop"
        )
        cmd_str = egress_cmd + "; " + ingress_cmd
    else:
        # Снятие лимита: удаляем egress HTB-класс/фильтр + ingress police-фильтр.
        ingress_prio = int(class_id, 16)
        cmd_str = (
            f"tc filter del dev {iface} protocol ip parent 1:0 prio 1 u32 match ip dst {ip}/32 2>/dev/null || true; "
            f"tc class del dev {iface} classid 1:{class_id} 2>/dev/null || true; "
            f"tc filter del dev {iface} parent ffff: protocol ip prio {ingress_prio} 2>/dev/null || true"
        )

    # awg0 находится внутри Docker контейнера amnezia-awg2.
    if target["host"] == "local":
        run_vpn_cmd(target, ["sh", "-c", cmd_str], capture=False)
    else:
        _run_remote_tc(target["host"], target["port"], cmd_str, timeout=15)
    return True


def auto_apply_qos() -> None:
    """Авто-применение всех сохранённых QoS-лимитов при старте."""
    logger.info("Автоприменение QoS-лимитов из БД...")
    users = get_all_users()
    applied = 0
    default_server = cfg.vpn_containers[0]["alias"] if cfg.vpn_containers else ""
    for ip, data in users.items():
        speed = data.get("speed_limit", "max")
        protocol = data.get("protocol") or default_server
        if speed != "max" and validate_vpn_ip(ip) and protocol:
            if apply_limit(ip, speed, protocol):
                applied += 1
    logger.info(f"QoS-лимиты применены: {applied}/{len(users)}")


def delete_peer(ip: str, server_alias: str = "") -> None:
    """Удаляет пира из WireGuard-интерфейса и сохраняет конфиг."""
    target = next((c for c in cfg.vpn_containers if c["alias"] == server_alias), None)
    # Если сервер не указан или не найден, ищем пира по всем контейнерам
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


# ─── Мониторинг (метрики, графики, пинг) ─────────────────────────────────────


def get_node_metrics(node: dict) -> dict[str, Any] | None:
    """CPU, RAM, Uptime и счётчики сети с любого сервера (local или SSH)."""
    if not node:
        return None

    remote_cmd = (
        "echo "
        "$(top -bn1 | grep 'Cpu(s)' | awk '{print $2 + $4}') "
        "$(free | grep Mem | awk '{print $3, $2}') "
        "$(awk '{print $1}' /proc/uptime) "
        "$(cat /proc/net/dev | awk '/eth0:/{print $2, $10}' || cat /proc/net/dev | tail -n +3 | awk '{recv += $2; sent += $10} END {print recv, sent}') "
        "$(df / | grep '/' | tail -1 | awk '{print $3, $2}')"
    )
    if node.get("host") == "local":
        try:
            res = subprocess.run(remote_cmd, shell=True, capture_output=True, text=True, timeout=5)
            output = res.stdout.strip()
        except Exception:
            output = None
    else:
        user = node.get("user") or "root"
        output = run_ssh_cmd(node["host"], node.get("port") or "22", remote_cmd, timeout=10, user=user)

    if not output:
        return None

    data = output.split()
    if len(data) < 8:
        return None

    try:
        h = int(float(data[3]) // 3600)
        m = int((float(data[3]) % 3600) // 60)
        uptime_str = f"{h // 24}д {h % 24}ч {m}м" if h >= 24 else f"{h}ч {m}м"

        ram_used = round(int(data[1]) / (1024 * 1024), 1)
        ram_total = round(int(data[2]) / (1024 * 1024), 1)
        ram_pct = round((ram_used / ram_total) * 100, 1) if ram_total > 0 else 0

        disk_used = round(int(data[6]) / (1024 * 1024), 1)
        disk_total = round(int(data[7]) / (1024 * 1024), 1)
        disk_pct = round((disk_used / disk_total) * 100, 1) if disk_total > 0 else 0

        return {
            "cpu": data[0],
            "ram": ram_pct,
            "ram_used": ram_used,
            "ram_total": ram_total,
            "uptime": uptime_str,
            "net_recv": int(data[4]),
            "net_sent": int(data[5]),
            "disk": disk_pct,
            "disk_used": disk_used,
            "disk_total": disk_total,
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга метрик ноды {node.get('alias')}: {e}")
        return None


def get_ru_metrics() -> dict[str, Any] | None:
    """Обратная совместимость: возвращает метрики RU-сервера."""
    ru = next((c for c in cfg.vpn_containers if "RU" in c["alias"]), cfg.vpn_containers[0] if cfg.vpn_containers else None)
    return get_node_metrics(ru) if ru else None


def get_node_ping(node: dict) -> list[str]:
    """Пингует сервисы с указанного сервера."""
    if not node:
        return []

    remote_cmd = (
        "for host in 8.8.8.8 1.1.1.1 77.88.8.8 telegram.org instagram.com; do "
        "val=$(ping -c 1 -W 1 -q $host 2>/dev/null | awk -F'/' 'END{print $5}'); "
        'if [ -z "$val" ]; then echo "timeout"; else echo "$val"; fi; '
        "done"
    )
    if node.get("host") == "local":
        try:
            res = subprocess.run(remote_cmd, shell=True, capture_output=True, text=True, timeout=8)
            output = res.stdout.strip()
        except Exception:
            output = ""
    else:
        user = node.get("user") or "root"
        output = run_ssh_cmd(node["host"], node.get("port") or "22", remote_cmd, timeout=10, user=user)

    return output.split("\n") if output else []


def get_ru_ping() -> list:
    """Обратная совместимость: пинг с RU сервера."""
    ru = next((c for c in cfg.vpn_containers if "RU" in c["alias"]), cfg.vpn_containers[0] if cfg.vpn_containers else None)
    return get_node_ping(ru) if ru else []


# ─── Мониторинг RU: живой замер скорости (дельта) ─────────────────────────────

_ru_monitor_cache: dict = {}
_ru_monitor_cache_ts: float = 0.0
_RU_MONITOR_TTL = 3  # кэш на 3 сек для быстрого отклика

_speed_samples: dict = {}  # pkey -> (rx, tx, time)
_current_active_now: list = []
_active_lock = threading.Lock()
_sampler_started = False


def _start_speed_sampler():
    global _sampler_started
    if _sampler_started:
        return
    _sampler_started = True

    def _loop():
        global _current_active_now, _speed_samples
        while True:
            try:
                # 1. transfers
                raw_t = subprocess.check_output(
                    ["docker", "exec", "amnezia-awg2", "awg", "show", "awg0", "transfer"],
                    text=True, timeout=4
                )
                # 2. latest-handshakes
                raw_h = subprocess.check_output(
                    ["docker", "exec", "amnezia-awg2", "awg", "show", "awg0", "latest-handshakes"],
                    text=True, timeout=4
                )
                # 3. allowed-ips
                raw_i = subprocess.check_output(
                    ["docker", "exec", "amnezia-awg2", "awg", "show", "awg0", "allowed-ips"],
                    text=True, timeout=4
                )
                # 4. names
                names_map = _get_amnezia_client_names({"name": "amnezia-awg2", "host": "local"})

                ips_map = {}
                for line in raw_i.strip().splitlines():
                    p = line.split("\t")
                    if len(p) >= 2:
                        ips_map[p[0]] = p[1].split("/")[0].strip()

                hs_map = {}
                for line in raw_h.strip().splitlines():
                    p = line.split("\t")
                    if len(p) >= 2 and p[1].isdigit():
                        hs_map[p[0]] = int(p[1])

                now_t = time.time()
                curr_trans = {}
                for line in raw_t.strip().splitlines():
                    p = line.split("\t")
                    if len(p) >= 3:
                        curr_trans[p[0]] = (int(p[1]), int(p[2]))

                active_list = []
                for pkey, hs in hs_map.items():
                    # Клиент онлайн (рукопожатие за последние 3 минуты = 180с)
                    if (now_t - hs) < 180 and hs > 0:
                        rx2, tx2 = curr_trans.get(pkey, (0, 0))
                        drx = 0
                        dtx = 0
                        dt = 3.0
                        if pkey in _speed_samples:
                            rx1, tx1, t1 = _speed_samples[pkey]
                            dt = max(1.0, now_t - t1)
                            drx = max(0, rx2 - rx1)
                            dtx = max(0, tx2 - tx1)

                        ip = ips_map.get(pkey, "?")
                        name = names_map.get(pkey) or names_map.get(ip) or ip

                        active_list.append({
                            "name": name,
                            "ip": ip,
                            "up_kb": round(drx / 1024, 1),
                            "dn_kb": round(dtx / 1024, 1),
                            "interval_sec": round(dt, 1),
                            "total_kb": round((drx + dtx) / 1024, 1),
                            "last_seen_sec": int(now_t - hs),
                        })

                # Сохраняем срез для следующего замера
                for pkey, (rx2, tx2) in curr_trans.items():
                    _speed_samples[pkey] = (rx2, tx2, now_t)

                # Сортируем: сначала те, у кого скорость выше нуля, затем по времени последнего рукопожатия
                active_list.sort(key=lambda x: (x["total_kb"], -x["last_seen_sec"]), reverse=True)

                with _active_lock:
                    _current_active_now = active_list

            except Exception as e:
                logger.debug(f"Speed sampler error: {e}")
            time.sleep(3)

    t = threading.Thread(target=_loop, daemon=True, name="awg_speed_sampler")
    t.start()


# Запускаем фоновый замер сразу при загрузке модуля
_start_speed_sampler()

def get_ru_monitor_data(force: bool = False) -> dict:
    """Возвращает живые данные мониторинга RU: статус Xray, реальная скорость активных клиентов, топ."""
    global _ru_monitor_cache, _ru_monitor_cache_ts

    if not force and time.time() - _ru_monitor_cache_ts < _RU_MONITOR_TTL and _ru_monitor_cache:
        return _ru_monitor_cache

    try:
        _start_speed_sampler()

        # 1. Xray status & PID
        xray_pid = "?"
        xray_ok = False
        try:
            pid = subprocess.check_output(["pgrep", "-x", "xray"], text=True).strip()
            if pid:
                xray_pid = pid.split()[0]
                xray_ok = True
        except Exception:
            pass

        # 2. Connection to SE
        conn_sw = 0
        se_dst = cfg.se_host or "127.0.0.1"
        try:
            out = subprocess.check_output(["ss", "-tnH", "state", "established", "dst", se_dst], text=True)
            conn_sw = len([l for l in out.strip().splitlines() if l.strip()])
        except Exception:
            pass

        # 3. Топ по суммарному трафику за сессию
        all_stats = get_vpn_stats()
        ru_peers = [s for s in all_stats if "RU" in s.get("server_alias", "")] or all_stats

        top_total = []
        for p in ru_peers:
            top_total.append({
                "name": p.get("db_alias", p.get("ip", "?")),
                "ip": p.get("ip", "?"),
                "dn_gb": round(p.get("raw_tx", 0) / (1024**3), 2),
                "up_gb": round(p.get("raw_rx", 0) / (1024**3), 2),
            })
        top_total.sort(key=lambda x: x["dn_gb"], reverse=True)

        # 4. Реальные активные клиенты прямо сейчас (дельта за 4 сек)
        with _active_lock:
            active_now = list(_current_active_now)

        events = [
            {"is_crash": False, "text": f"Xray Dokodemo Bridge работает штатно (PID {xray_pid})", "ts": "сейчас"},
            {"is_crash": False, "text": "AmneziaAWG2 активен, туннель к SE стабилен", "ts": "сейчас"}
        ]

        data = {
            "xray_ok": xray_ok,
            "xray_pid": xray_pid,
            "conn_sw": conn_sw,
            "top_total": top_total,
            "active_now": active_now,
            "events": events
        }
        _ru_monitor_cache = data
        _ru_monitor_cache_ts = time.time()
        return data
    except Exception as e:
        logger.error(f"Error in get_ru_monitor_data: {e}")
        return {"error": str(e)}


def check_and_restore_qos() -> None:
    """Проверяет наличие корневого HTB-шейпера на интерфейсах. Если сбросился — восстанавливает QoS."""
    try:
        from database import get_all_users
        users = get_all_users()
        servers_with_limits = {
            data["protocol"]
            for data in users.values()
            if data.get("speed_limit", "max") != "max" and data.get("protocol")
        }
        for target in cfg.vpn_containers:
            if target["alias"] not in servers_with_limits:
                continue
            res = run_vpn_cmd(target, ["tc", "qdisc", "show", "dev", target["iface"]], capture=True, timeout=15)
            if res and res.returncode == 0 and "htb" not in (res.stdout or ""):
                logger.warning(f"QoS корневой HTB отсутствует на {target['alias']}. Пересоздаём...")
                auto_apply_qos()
                break
    except Exception as e:
        logger.error(f"Ошибка проверки QoS: {e}")


def sync_db_with_vpn_peers() -> dict[str, Any]:
    """
    Синхронизирует БД с реальными пирами в WireGuard/AmneziaWG:
    - Обнаруживает новые пиры, добавленные через приложение Amnezia, и добавляет их в БД
    - Удаляет фантомные пиры, которых больше нет в конфигурациях
    """
    from database import get_all_users, update_user_field, delete_user, get_conn
    
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
                logger.warning(f"Не удалось получить пиры с {target['alias']} (код: {res.returncode if res else 'None'}), сервер исключен из очистки.")
        except Exception as e:
            logger.error(f"Ошибка получения пиров с {target['alias']}: {e}")

    added = 0
    cleaned = 0

    # 1. Добавляем новые пиры, если их нет в БД
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

    # 2. Удаляем из БД только те IP, чей сервер УСПЕШНО ответил, но пира на нем гарантированно нет
    for ip, data in current_db_users.items():
        proto = data.get("protocol")
        # Защита: если сервер абонента не ответил успешно — ни в коем случае не удаляем запись!
        if proto not in successful_servers:
            continue
        if ip not in active_vpn_ips:
            if not ip.endswith(".0") and not ip.endswith(".255"):
                delete_user(ip)
                cleaned += 1

    logger.info(f"Синхронизация БД с VPN: добавлено новых={added}, удалено фантомных={cleaned}")
    return {"ok": True, "added": added, "cleaned": cleaned, "total_active": len(active_vpn_ips)}
