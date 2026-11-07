"""
vpn/qos.py — Управление QoS/трафик-контролем (tc/htb/ingress police) и восстановление лимитов.
"""
import ipaddress
import shlex
import subprocess

from config import cfg, logger
from database import get_all_users
from vpn.transport import _SSH_BASE_OPTS, run_vpn_cmd, validate_vpn_ip


def get_class_id(ip_str: str) -> str:
    """Генерирует tc class-id из IPv4-адреса. Защита от IPv6."""
    ip = ipaddress.ip_address(ip_str)
    if ip.version == 6:
        return "ffff"
    parts = ip_str.split(".")
    return f"{int(parts[2]):x}{int(parts[3]):02x}"


def _run_remote_tc(host: str, port: str, tc_cmd_str: str, timeout: int = 15, user: str = "root") -> None:
    """Выполняет TC-команды напрямую на хосте по SSH."""
    remote_timeout = max(5, timeout - 2)
    dest = f"{user}@{host}" if user and "@" not in host else host
    ssh_cmd = (
        ["ssh"] + _SSH_BASE_OPTS
        + ["-p", str(port), dest,
           f"timeout {remote_timeout} sudo sh -c {shlex.quote(tc_cmd_str)}"]
    )
    try:
        res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0 and res.stderr:
            logger.warning(f"TC на {dest}: {res.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        logger.warning(f"TC таймаут на {dest}:{port}")
    except Exception as e:
        logger.error(f"TC ошибка ({dest}:{port}): {e}")


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
    hex_ip = "".join(f"{int(o):02x}" for o in ip.split("."))

    if speed != "max":
        rate = "1kbit" if speed == "punish" else f"{speed}mbit"
        ceil = rate
        burst = "16k" if speed == "punish" else "500k"
        cburst = "16k" if speed == "punish" else "500k"
        ingress_burst = "16k" if speed == "punish" else "500k"
        ingress_prio = int(class_id, 16)

        # EGRESS (download клиента): HTB на root qdisc, ловит пакеты dst={ip}/32
        egress_cmd = (
            f"if ! tc qdisc show dev {iface} | grep -q htb; then "
            f"tc qdisc add dev {iface} root handle 1: htb default 9999 r2q 100 2>/dev/null || true; "
            f"fi; "
            f"tc class replace dev {iface} parent 1: classid 1:1 htb rate 1000mbit burst 500k cburst 500k quantum 200000 2>/dev/null || true; "
            f"tc class replace dev {iface} parent 1:1 classid 1:9999 htb rate 1000mbit ceil 1000mbit burst 500k cburst 500k quantum 200000 2>/dev/null || true; "
            f"tc class replace dev {iface} parent 1:1 classid 1:{class_id} htb rate {rate} ceil {ceil} burst {burst} cburst {cburst} quantum 25000; "
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

    if target["host"] == "local":
        run_vpn_cmd(target, ["sh", "-c", cmd_str], capture=False)
    else:
        _run_remote_tc(target["host"], target["port"], cmd_str, timeout=15, user=target.get("user") or "root")
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


def check_and_restore_qos() -> None:
    """Проверяет наличие корневого HTB-шейпера на интерфейсах. Если сбросился — восстанавливает QoS."""
    try:
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
