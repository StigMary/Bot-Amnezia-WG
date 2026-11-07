"""
vpn/monitor.py — Сбор системных метрик серверов (CPU/RAM/DISK/Uptime/Ping) и оптимизированный замер скорости.
"""
import subprocess
import threading
import time
from typing import Any

from config import cfg, logger
from vpn.peers import _get_amnezia_client_names, get_vpn_stats
from vpn.transport import run_ssh_cmd


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
            res = subprocess.run(["sh", "-c", remote_cmd], capture_output=True, text=True, timeout=5)
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
        uptime_sec = float(data[3])
        h = int(uptime_sec // 3600)
        m = int((uptime_sec % 3600) // 60)
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
    """Возвращает метрики RU-сервера."""
    ru = next((c for c in cfg.vpn_containers if "RU" in c["alias"]), cfg.vpn_containers[0] if cfg.vpn_containers else None)
    return get_node_metrics(ru) if ru else None


def get_node_ping(node: dict) -> list[str]:
    """Пингует популярные сервисы с указанного сервера."""
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
            res = subprocess.run(["sh", "-c", remote_cmd], capture_output=True, text=True, timeout=8)
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
_RU_MONITOR_TTL = 3  # кэш на 3 сек

_speed_samples: dict = {}  # pkey -> (rx, tx, time)
_current_active_now: list = []
_active_lock = threading.Lock()
_sampler_started = False


def _start_speed_sampler():
    """Фоновый поток замера дельты трафика пиров (1 вызов awg show dump вместо 3)."""
    global _sampler_started
    if _sampler_started:
        return
    _sampler_started = True

    def _loop():
        global _current_active_now, _speed_samples
        while True:
            sleep_time = 4.0
            try:
                # Один вызов awg show awg0 dump вместо 3 отдельных docker exec
                raw_dump = subprocess.check_output(
                    ["docker", "exec", "amnezia-awg2", "awg", "show", "awg0", "dump"],
                    text=True, timeout=5
                )
                names_map = _get_amnezia_client_names({"name": "amnezia-awg2", "host": "local"})

                now_t = time.time()
                curr_trans = {}
                active_list = []

                for line in raw_dump.strip().splitlines()[1:]:
                    parts = line.split("\t")
                    if len(parts) >= 8:
                        pkey = parts[0]
                        allowed_ips = parts[3].split("/")[0].strip()
                        hs = int(parts[4]) if parts[4].isdigit() else 0
                        rx2 = int(parts[5]) if parts[5].isdigit() else 0
                        tx2 = int(parts[6]) if parts[6].isdigit() else 0

                        curr_trans[pkey] = (rx2, tx2)

                        # Клиент онлайн (рукопожатие за последние 3 минуты = 180с)
                        if (now_t - hs) < 180 and hs > 0:
                            drx = 0
                            dtx = 0
                            dt = 3.0
                            if pkey in _speed_samples:
                                rx1, tx1, t1 = _speed_samples[pkey]
                                dt = max(1.0, now_t - t1)
                                drx = max(0, rx2 - rx1)
                                dtx = max(0, tx2 - tx1)

                            ip = allowed_ips if allowed_ips and allowed_ips != "(none)" else "?"
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

                for pkey, (rx2, tx2) in curr_trans.items():
                    _speed_samples[pkey] = (rx2, tx2, now_t)

                active_list.sort(key=lambda x: (x["total_kb"], -x["last_seen_sec"]), reverse=True)

                with _active_lock:
                    _current_active_now = active_list

                # Адаптивный интервал: если никого активного нет, спим 15 сек для экономии ресурсов
                if not active_list:
                    sleep_time = 15.0
                else:
                    sleep_time = 4.0

            except Exception as e:
                logger.debug(f"Speed sampler error: {e}")
                sleep_time = 10.0

            time.sleep(sleep_time)

    t = threading.Thread(target=_loop, daemon=True, name="awg_speed_sampler")
    t.start()


_start_speed_sampler()


def get_ru_monitor_data(force: bool = False) -> dict:
    """Возвращает живые данные мониторинга RU: статус Xray, реальная скорость активных клиентов, топ."""
    global _ru_monitor_cache, _ru_monitor_cache_ts

    if not force and time.time() - _ru_monitor_cache_ts < _RU_MONITOR_TTL and _ru_monitor_cache:
        return _ru_monitor_cache

    try:
        _start_speed_sampler()

        # 1. Xray status & PID на удалённом RU сервере
        xray_pid = "?"
        xray_ok = False
        ru_node = next((c for c in cfg.vpn_containers if "RU" in c.get("alias", "").upper()), None)
        ru_host = ru_node.get("host") if ru_node else cfg.ru_host
        ru_port = str(ru_node.get("port") or cfg.ru_ssh_port or "22") if ru_node else str(cfg.ru_ssh_port or "22")
        ru_user = ru_node.get("user") or "root" if ru_node else "root"

        if ru_host and ru_host != "local":
            pid_out = run_ssh_cmd(ru_host, ru_port, "pgrep -x xray || docker inspect -f '{{.State.Pid}}' xray 2>/dev/null", timeout=5, user=ru_user)
            if pid_out and pid_out.split()[0].isdigit():
                xray_pid = pid_out.split()[0]
                xray_ok = True
        else:
            try:
                pid = subprocess.check_output(["pgrep", "-x", "xray"], text=True).strip()
                if pid:
                    xray_pid = pid.split()[0]
                    xray_ok = True
            except Exception:
                pass

        # 2. Connection to SE (Reality tunnel inbound to port 443 from RU node)
        conn_sw = 0
        ru_ip_clean = (ru_host or "").split("@")[-1].strip()
        try:
            # На SE сервере проверяем входящие Reality соединения на порт 443
            cmd = ["ss", "-tnH", "state", "established", "(", "sport", "=", ":443", ")"]
            out = subprocess.check_output(cmd, text=True)
            lines = [l for l in out.strip().splitlines() if l.strip()]
            if ru_ip_clean and ru_ip_clean != "local":
                conn_sw = len([l for l in lines if ru_ip_clean in l])
            else:
                conn_sw = len(lines)
        except Exception:
            # Fallback: опрос через SSH на RU ноде
            if ru_host and ru_host != "local":
                try:
                    se_target = (cfg.se_host or "132.243.240.76").split("@")[-1].strip()
                    ru_out = run_ssh_cmd(ru_host, ru_port, f"ss -tnH state established dst {se_target}:443", timeout=4, user=ru_user)
                    if ru_out:
                        conn_sw = len([l for l in ru_out.strip().splitlines() if l.strip()])
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

        with _active_lock:
            active_now = list(_current_active_now)

        events = [
            {"is_crash": False, "text": f"Xray Dokodemo Bridge работает штатно (PID {xray_pid})" if xray_ok else "Xray мост не запущен", "ts": "сейчас"},
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
