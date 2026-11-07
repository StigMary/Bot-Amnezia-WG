"""
vpn/monitor.py — Сбор системных метрик серверов (CPU/RAM/DISK/Uptime/Ping) и оптимизированный замер скорости.
"""
import subprocess
import threading
import time
from typing import Any

from config import cfg, logger
from database import get_all_users
from vpn.peers import _get_amnezia_client_names, get_vpn_stats
from vpn.transport import run_ssh_cmd, run_vpn_cmd


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


# ─── Мониторинг серверов: живой замер скорости (дельта) ───────────────────────

_server_monitor_cache: dict = {}
_server_monitor_cache_ts: dict = {}
_SERVER_MONITOR_TTL = 3  # кэш на 3 сек

_speed_samples: dict = {}  # pkey -> (rx, tx, time)
_current_active_now: list = []
_active_lock = threading.Lock()
_sampler_started = False


def _start_speed_sampler():
    """Фоновый поток замера дельты трафика пиров (опрос всех доступных нод)."""
    global _sampler_started
    if _sampler_started:
        return
    _sampler_started = True

    def _loop():
        global _current_active_now, _speed_samples
        while True:
            sleep_time = 4.0
            try:
                db_users = get_all_users()
                now_t = time.time()
                curr_trans = {}
                active_list = []

                for cont in cfg.vpn_containers:
                    try:
                        res = run_vpn_cmd(cont, ["awg", "show", cont.get("iface", "awg0"), "dump"], timeout=5)
                        if not res or res.returncode != 0 or not res.stdout:
                            continue
                        raw_dump = res.stdout
                        names_map = _get_amnezia_client_names(cont)
                        alias = cont.get("alias", "")

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
                                    dt = 4.0
                                    if pkey in _speed_samples:
                                        rx1, tx1, t1 = _speed_samples[pkey]
                                        dt = max(1.0, now_t - t1)
                                        drx = max(0, rx2 - rx1)
                                        dtx = max(0, tx2 - tx1)

                                    ip = allowed_ips if allowed_ips and allowed_ips != "(none)" else "?"
                                    name = (db_users.get(ip) or {}).get("alias") or names_map.get(pkey) or names_map.get(ip) or ip

                                    active_list.append({
                                        "name": name,
                                        "ip": ip,
                                        "server_alias": alias,
                                        "up_kb": round(drx / 1024, 1),
                                        "dn_kb": round(dtx / 1024, 1),
                                        "interval_sec": round(dt, 1),
                                        "total_kb": round((drx + dtx) / 1024, 1),
                                        "last_seen_sec": int(now_t - hs),
                                    })
                    except Exception as ce:
                        logger.debug(f"Speed sampler container {cont.get('alias')} error: {ce}")

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


def get_server_monitor_data(server_identifier: str = "RU", force: bool = False) -> dict:
    """Возвращает живые данные мониторинга любого сервера (SE, RU, Node N): статус Xray/3X-UI, скорость активных клиентов, топ."""
    global _server_monitor_cache, _server_monitor_cache_ts

    target_key = (server_identifier or "RU").strip().upper()
    now_ts = time.time()
    if not force and target_key in _server_monitor_cache:
        if now_ts - _server_monitor_cache_ts.get(target_key, 0.0) < _SERVER_MONITOR_TTL:
            return _server_monitor_cache[target_key]

    try:
        _start_speed_sampler()

        # 1. Поиск целевого контейнера / сервера
        target = None
        for c in cfg.vpn_containers:
            c_id = str(c.get("id") or "").upper()
            c_alias = str(c.get("alias") or "").upper()
            if target_key in (c_id, c_alias) or target_key in c_alias.split() or (target_key in ("RU", "SE") and target_key in c_id):
                target = c
                break

        if not target:
            if "RU" in target_key:
                target = next((c for c in cfg.vpn_containers if "RU" in c.get("alias", "").split() or "🇷🇺" in c.get("alias", "") or c.get("id") == "RU"), None)
            elif "SE" in target_key:
                target = next((c for c in cfg.vpn_containers if "SE" in c.get("alias", "").split() or "🇸🇪" in c.get("alias", "") or c.get("id") == "SE"), None)

        if not target and cfg.vpn_containers:
            target = cfg.vpn_containers[0]

        if not target:
            return {"error": f"Сервер '{server_identifier}' не найден"}

        is_local = (target.get("host") == "local")
        host = target.get("host")
        port = str(target.get("port") or "22")
        user = target.get("user") or "root"
        is_ru = "RU" in target.get("alias", "").split() or "🇷🇺" in target.get("alias", "") or target.get("id") == "RU"
        is_se = "SE" in target.get("alias", "").split() or "🇸🇪" in target.get("alias", "") or target.get("id") == "SE"
        srv_alias = target.get("alias", "")

        # 2. Xray / 3X-UI статус & PID
        xray_pid = "?"
        xray_ok = False
        conn_sw = 0

        if is_local:
            try:
                pid_out = subprocess.check_output(
                    ["sh", "-c", "docker inspect -f '{{.State.Pid}}' 3x-ui 2>/dev/null || docker inspect -f '{{.State.Pid}}' xray 2>/dev/null || pgrep -f xray-linux-amd64 | head -1 || pgrep -x xray | head -1"],
                    text=True, timeout=3
                ).strip()
                if pid_out and pid_out.split()[0].isdigit() and pid_out.split()[0] != "0":
                    xray_pid = pid_out.split()[0]
                    xray_ok = True
            except Exception:
                pass

            try:
                cmd = ["ss", "-tnH", "state", "established", "(", "sport", "=", ":443", ")"]
                out = subprocess.check_output(cmd, text=True)
                lines = [l for l in out.strip().splitlines() if l.strip()]
                conn_sw = len(lines)
            except Exception:
                pass
        else:
            pid_cmd = "docker inspect -f '{{.State.Pid}}' xray 2>/dev/null || docker inspect -f '{{.State.Pid}}' 3x-ui 2>/dev/null || pgrep -x xray || pgrep -f xray-linux-amd64 | head -1"
            pid_out = run_ssh_cmd(host, port, pid_cmd, timeout=5, user=user)
            if pid_out and pid_out.split()[0].isdigit() and pid_out.split()[0] != "0":
                xray_pid = pid_out.split()[0]
                xray_ok = True

            if is_ru:
                ru_ip_clean = (host or "").split("@")[-1].strip()
                try:
                    cmd = ["ss", "-tnH", "state", "established", "(", "sport", "=", ":443", ")"]
                    out = subprocess.check_output(cmd, text=True)
                    lines = [l for l in out.strip().splitlines() if l.strip()]
                    conn_sw = len([l for l in lines if ru_ip_clean in l]) if ru_ip_clean else len(lines)
                except Exception:
                    try:
                        se_target = (cfg.se_host or "132.243.240.76").split("@")[-1].strip()
                        ru_out = run_ssh_cmd(host, port, f"ss -tnH state established dst {se_target}:443", timeout=4, user=user)
                        if ru_out:
                            conn_sw = len([l for l in ru_out.strip().splitlines() if l.strip()])
                    except Exception:
                        pass
            else:
                try:
                    out = run_ssh_cmd(host, port, "ss -tnH state established '( dport = :443 or sport = :443 )' | wc -l", timeout=4, user=user)
                    if out and out.strip().isdigit():
                        conn_sw = int(out.strip())
                except Exception:
                    pass

        # 3. Топ абонентов конкретного сервера по суммарному трафику
        all_stats = get_vpn_stats()
        srv_peers = [s for s in all_stats if s.get("server_alias") == srv_alias]
        if not srv_peers:
            if is_ru:
                srv_peers = [s for s in all_stats if s.get("ip", "").startswith("10.8.1.")]
            elif is_se:
                srv_peers = [s for s in all_stats if s.get("ip", "").startswith("10.8.5.")]
            else:
                srv_peers = all_stats

        top_total = []
        for p in srv_peers:
            top_total.append({
                "name": p.get("db_alias", p.get("ip", "?")),
                "ip": p.get("ip", "?"),
                "dn_gb": round(p.get("raw_tx", 0) / (1024**3), 2),
                "up_gb": round(p.get("raw_rx", 0) / (1024**3), 2),
            })
        top_total.sort(key=lambda x: x["dn_gb"], reverse=True)

        # 4. Активные клиенты на этом сервере
        with _active_lock:
            server_active = [
                c for c in _current_active_now
                if c.get("server_alias") == srv_alias
            ]
            if not server_active:
                if is_ru:
                    server_active = [
                        c for c in _current_active_now
                        if ("RU" in c.get("server_alias", "").split() or "🇷🇺" in c.get("server_alias", "") or c.get("ip", "").startswith("10.8.1."))
                        and not c.get("ip", "").startswith("10.8.5.")
                    ]
                elif is_se:
                    server_active = [
                        c for c in _current_active_now
                        if ("SE" in c.get("server_alias", "").split() or "🇸🇪" in c.get("server_alias", "") or c.get("ip", "").startswith("10.8.5."))
                        and not c.get("ip", "").startswith("10.8.1.")
                    ]

        # Fallback если фоновый замер ещё стартует
        if not server_active:
            now_t = time.time()
            for p in srv_peers:
                hs = p.get("handshake_ts", 0)
                if (now_t - hs) < 180 and hs > 0:
                    server_active.append({
                        "name": p.get("db_alias", p.get("ip", "?")),
                        "ip": p.get("ip", "?"),
                        "server_alias": srv_alias,
                        "up_kb": 0.0,
                        "dn_kb": 0.0,
                        "interval_sec": 4.0,
                        "total_kb": 0.0,
                        "last_seen_sec": int(now_t - hs),
                    })
            server_active.sort(key=lambda x: x["last_seen_sec"])

        events = []
        if xray_ok:
            svc_name = "Xray Dokodemo Bridge" if is_ru else ("3X-UI / Xray Core" if is_se else "Xray Core")
            events.append({"is_crash": False, "text": f"{svc_name} работает штатно (PID {xray_pid})", "ts": "сейчас"})
        else:
            events.append({"is_crash": False, "text": "Служба Xray не активна на этой ноде", "ts": "сейчас"})

        c_name = target.get("name") or "amnezia-awg2"
        tunnel_msg = "туннель к SE стабилен" if is_ru else ("входящие Reality соединения активны" if is_se else "интерфейс активен")
        events.append({"is_crash": False, "text": f"AmneziaWG ({c_name}) активен, {tunnel_msg}", "ts": "сейчас"})

        data = {
            "server_alias": srv_alias,
            "server_id": target.get("id", target_key),
            "is_ru": is_ru,
            "is_se": is_se,
            "xray_ok": xray_ok,
            "xray_pid": xray_pid,
            "conn_sw": conn_sw,
            "top_total": top_total,
            "active_now": server_active,
            "events": events
        }
        _server_monitor_cache[target_key] = data
        _server_monitor_cache_ts[target_key] = time.time()
        return data
    except Exception as e:
        logger.error(f"Error in get_server_monitor_data for {server_identifier}: {e}")
        return {"error": str(e)}


def get_ru_monitor_data(force: bool = False) -> dict:
    """Обратная совместимость: возвращает живые данные мониторинга RU."""
    return get_server_monitor_data("RU", force=force)
