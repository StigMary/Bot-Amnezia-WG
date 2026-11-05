"""
webapp/metrics.py — Кэширование и асинхронный сбор метрик серверов и VPN.
"""
import asyncio
import os
import subprocess
import threading
import time

import psutil

from config import cfg, logger
from vpn_engine import get_ru_metrics, get_vpn_stats

_cache_lock = threading.Lock()
_cache = {}
_CACHE_TTL = {
    "dashboard": 5,    # 5 секунд
    "servers":   5,
    "users":     10,
    "vpn_stats": 8,
    "ru_metrics": 8,
    "cpu":       3,
}


def _cache_get(key: str):
    """Возвращает кэшированные данные если TTL не истек."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry["expires"]:
            return entry["data"]
    return None


def _cache_set(key: str, data, ttl: int = None):
    """Сохраняет данные в оперативный кэш с заданным TTL."""
    with _cache_lock:
        _cache[key] = {"data": data, "expires": time.time() + (ttl or _CACHE_TTL.get(key, 5))}


def invalidate_cache(keys: list[str] | None = None) -> None:
    """Инвалидирует кэш в памяти (все ключи или указанный список)."""
    with _cache_lock:
        if keys is None:
            _cache.clear()
        else:
            for k in keys:
                _cache.pop(k, None)


def _cached_cpu():
    """Замер загрузки CPU с кэшированием на 3 секунды."""
    v = _cache_get("cpu")
    if v is not None:
        return v
    # interval=0 — мгновенно (использует предыдущий замер ядра Linux)
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
    """Не блокирует event loop — запускает get_vpn_stats в threadpool."""
    v = _cache_get("vpn_stats")
    if v is not None:
        return v
    loop = asyncio.get_event_loop()
    v = await loop.run_in_executor(None, get_vpn_stats)
    _cache_set("vpn_stats", v)
    return v


async def _async_ru_metrics():
    """Не блокирует event loop — запускает get_ru_metrics в threadpool."""
    v = _cache_get("ru_metrics")
    if v is not None:
        return v
    loop = asyncio.get_event_loop()
    v = await loop.run_in_executor(None, get_ru_metrics)
    _cache_set("ru_metrics", v)
    return v


def get_local_ru_metrics():
    """Сбор метрик локального сервера (CPU, RAM, DISK, Uptime, Net)."""
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
    """Сбор метрик удаленного сервера SE по SSH."""
    if not cfg.se_host:
        return None
    cmd = "cat /proc/loadavg; free -m; df -m /; cat /proc/uptime; cat /proc/net/dev"
    try:
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
    """Асинхронное получение SE метрик с кэшированием."""
    v = _cache_get("se_metrics")
    if v is not None:
        return v
    loop = asyncio.get_event_loop()
    v = await loop.run_in_executor(None, get_remote_se_metrics)
    if v:
        _cache_set("se_metrics", v, ttl=5)
    return v
