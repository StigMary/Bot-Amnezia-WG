"""
vpn_engine.py — Взаимодействие с серверами.
SSH/Docker-транспорт, парсинг WireGuard, QoS (tc/htb), валидация.
"""

import ipaddress
import os
import re
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
    text = re.sub(r"[*_`\[\]()~>#+-=|{}.!\\]", "", text)
    return text or "Без имени"


# ─── SSH: ControlMaster для переиспользования подключений ─────────────────────
_SSH_CONTROL_DIR = "/tmp/ssh_ctl"  # nosec B108
os.makedirs(_SSH_CONTROL_DIR, exist_ok=True)

_SSH_BASE_OPTS = [
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "UserKnownHostsFile=/home/vpnuser/.ssh/known_hosts",
    "-o",
    "ControlMaster=auto",
    "-o",
    f"ControlPath={_SSH_CONTROL_DIR}/%h_%p_%r",
    "-o",
    "ControlPersist=120",
    "-o",
    "ConnectTimeout=8",
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
        final_cmd = (
            ["ssh"]
            + _SSH_BASE_OPTS
            + ["-p", str(target["port"]), target["host"]]
            + ["sudo", "docker", "exec", target["name"]]
            + cmd_list
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
) -> str | None:
    """Выполняет произвольную команду на удалённом сервере по SSH."""
    cmd = ["ssh"] + _SSH_BASE_OPTS + ["-p", str(port), host, remote_cmd]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0:
            return res.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning(f"SSH таймаут: {host}:{port}")
    except Exception as e:
        logger.error(f"SSH ошибка ({host}:{port}): {e}")
    return None


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


def _poll_container(cont: dict, db_users: dict) -> tuple:
    """Опрашивает один контейнер. Возвращает (entries, new_users)."""
    entries, new_users = [], []
    res = run_vpn_cmd(cont, [_wg_cmd(cont), "show", "all", "dump"])
    if not res or res.returncode != 0:
        return entries, new_users
    for line in res.stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 8 or parts[4] == "(none)":
            continue
        ip = parts[4].split("/")[0]
        if not validate_vpn_ip(ip):
            logger.warning(f"Невалидный IP из wg dump: {ip}")
            continue
        handshake, rx, tx = int(parts[5]), int(parts[6]), int(parts[7])
        user_data = db_users.get(ip)
        if not user_data:
            new_users.append((ip, cont["alias"]))
            user_data = {"alias": "Новый пользователь", "speed_limit": "max"}
        entries.append(
            {
                "ip": ip,
                "server_alias": cont["alias"],
                "db_alias": user_data["alias"],
                "speed_limit": user_data["speed_limit"],
                "last_seen": "Никогда" if handshake == 0 else datetime.fromtimestamp(handshake).strftime("%d.%m %H:%M"),
                "online_emoji": get_online_emoji(handshake),
                "downloaded": format_bytes(tx),
                "uploaded": format_bytes(rx),
                "total_bytes": rx + tx,
                "handshake_ts": handshake,
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
    with ThreadPoolExecutor(max_workers=len(cfg.vpn_containers)) as pool:
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

    result = sorted(stats, key=lambda x: x["total_bytes"], reverse=True)
    with _stats_lock:
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
        logger.error(f"apply_limit: сервер '{server_alias}' не найден")
        return False

    iface = target["iface"]
    class_id = get_class_id(ip)

    cmd_str = (
        f"tc filter del dev {iface} protocol ip parent 1:0 prio 1 u32 match ip dst {ip} 2>/dev/null || true; "
        f"tc class del dev {iface} classid 1:{class_id} 2>/dev/null || true; "
    )

    if speed != "max":
        rate = "1kbit" if speed == "punish" else f"{speed}mbit"
        cmd_str += (
            f"if ! tc qdisc show dev {iface} | grep -q htb; then "
            f"tc qdisc add dev {iface} root handle 1: htb default 10 2>/dev/null || true; "
            f"tc class add dev {iface} parent 1: classid 1:1 htb rate 1000mbit 2>/dev/null || true; "
            f"fi; "
            f"tc class add dev {iface} parent 1:1 classid 1:{class_id} htb rate {rate} ceil {rate}; "
            f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match ip dst {ip} flowid 1:{class_id};"
        )

    run_vpn_cmd(target, ["sh", "-c", cmd_str], capture=False)
    return True


def auto_apply_qos() -> None:
    """Авто-применение всех сохранённых QoS-лимитов при старте."""
    logger.info("Автоприменение QoS-лимитов из БД...")
    users = get_all_users()
    applied = 0
    for ip, data in users.items():
        speed = data.get("speed_limit", "max")
        if speed != "max" and validate_vpn_ip(ip) and data.get("protocol"):
            if apply_limit(ip, speed, data["protocol"]):
                applied += 1
    logger.info(f"QoS-лимиты применены: {applied}/{len(users)}")


def delete_peer(ip: str, server_alias: str) -> None:
    """Удаляет пира из WireGuard-интерфейса и сохраняет конфиг."""
    target = next((c for c in cfg.vpn_containers if c["alias"] == server_alias), None)
    if not target:
        return

    wg = _wg_cmd(target)
    iface = _wg_iface(target)

    res = run_vpn_cmd(target, [wg, "show", iface, "dump"])
    if not res or not res.stdout:
        return

    pub_key = None
    for line in res.stdout.split("\n"):
        if f"{ip}/32" in line:
            pub_key = line.split("\t")[0]
            break

    if pub_key:
        run_vpn_cmd(target, [wg, "set", iface, "peer", pub_key, "remove"])
        run_vpn_cmd(target, [f"{wg}-quick", "save", iface])


# ─── Мониторинг (метрики, графики, пинг) ─────────────────────────────────────


def get_rf_metrics() -> dict[str, Any] | None:
    """CPU, RAM, Uptime и счётчики сети с RF-сервера."""
    rf = next((c for c in cfg.vpn_containers if "RF" in c["alias"]), None)
    if not rf or rf["host"] == "local":
        return None

    remote_cmd = (
        "echo "
        "$(top -bn1 | grep 'Cpu(s)' | awk '{print $2 + $4}') "
        "$(free | grep Mem | awk '{print int($3/$2 * 100.0)}') "
        "$(awk '{print $1}' /proc/uptime) "
        "$(cat /proc/net/dev | tail -n +3 | awk '{recv += $2; sent += $10} END {print recv, sent}')"
    )
    output = run_ssh_cmd(rf["host"], rf["port"], remote_cmd, timeout=10)
    if not output:
        return None

    data = output.split()
    if len(data) < 5:
        return None

    return {
        "cpu": float(data[0]),
        "ram": float(data[1]),
        "uptime": str(timedelta(seconds=float(data[2]))).split(".")[0],
        "net_recv": int(data[3]),
        "net_sent": int(data[4]),
    }


def get_rf_ping() -> list:
    """Пингует сервисы с RF-сервера."""
    rf = next((c for c in cfg.vpn_containers if "RF" in c["alias"]), None)
    if not rf or rf["host"] == "local":
        return []

    remote_cmd = (
        "for host in 8.8.8.8 1.1.1.1 77.88.8.8 telegram.org instagram.com; do "
        "val=$(ping -c 3 -W 2 -q $host | awk -F'/' 'END{print $5}'); "
        'if [ -z "$val" ]; then echo "timeout"; else echo "$val"; fi; '
        "done"
    )
    output = run_ssh_cmd(rf["host"], rf["port"], remote_cmd, timeout=25)
    return output.split("\n") if output else []
