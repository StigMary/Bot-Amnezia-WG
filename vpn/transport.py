"""
vpn/transport.py — SSH/Docker транспорт команд с мультиплексированием (ControlMaster) и валидация.
"""
from datetime import datetime, timedelta
import ipaddress
import os
import re
import shlex
import subprocess
from typing import Any

from config import cfg, logger

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


def format_bytes(size: float) -> str:
    """Форматирует размер в байтах в человекочитаемую строку (КБ, МБ, ГБ)."""
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} ПБ"


def get_online_emoji(handshake_ts: int) -> str:
    """Возвращает индикатор онлайна по времени последнего рукопожатия."""
    if handshake_ts == 0:
        return "⚫"
    delta = datetime.now() - datetime.fromtimestamp(handshake_ts)
    if delta < timedelta(minutes=3):
        return "🟢"
    if delta < timedelta(hours=1):
        return "🟡"
    return "🔴"


# ─── SSH: ControlMaster для переиспользования подключений ─────────────────────

_SSH_CONTROL_DIR = os.path.join(cfg.data_dir, "ssh_ctl")
os.makedirs(_SSH_CONTROL_DIR, mode=0o700, exist_ok=True)

_SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=yes",
    "-o", f"UserKnownHostsFile={os.path.expanduser('~/.ssh/known_hosts')}",
    "-o", "BatchMode=yes",
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={_SSH_CONTROL_DIR}/%h_%p_%r",
    "-o", "ControlPersist=600",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
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
    """Выполняет произвольную команду на удалённом сервере по SSH с переиспользованием сессии."""
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
