#!/usr/bin/env python3
"""
manage.py — Консольный мастер управления VPN Bot & WebApp.

Использование:
    python3 manage.py setup       — Пошаговый интерактивный установщик (первоначальная настройка)
    python3 manage.py add-node    — Интерактивное добавление нового VPN-сервера
    python3 manage.py list-nodes   — Список всех подключенных серверов и их онлайн-статус
    python3 manage.py test        — Проверка здоровья системы (БД, токен Telegram, ноды)
"""

import os
import re
import shlex
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
ENV_EXAMPLE = os.path.join(BASE_DIR, ".env.example")


def _c(text: str, color_code: str) -> str:
    """Окрашивание вывода в терминале."""
    if not sys.stdout.isatty():
        return text
    return f"\033[{color_code}m{text}\033[0m"


def green(text: str) -> str:
    return _c(text, "32")


def red(text: str) -> str:
    return _c(text, "31")


def yellow(text: str) -> str:
    return _c(text, "33")


def blue(text: str) -> str:
    return _c(text, "34")


def bold(text: str) -> str:
    return _c(text, "1")


def print_banner(title: str):
    print("\n" + blue("=" * 60))
    print(bold(blue(f"   🚀 {title}")))
    print(blue("=" * 60) + "\n")


def prompt(text: str, default: str = "") -> str:
    """Запрос ввода с подсказкой значения по умолчанию."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{bold(text)}{suffix}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n" + yellow("Отменено пользователем."))
        sys.exit(0)
    return val if val else default


def test_ssh_connection(host: str, port: str, user: str, timeout: int = 6) -> tuple[bool, str]:
    """Проверка подключения к хосту по SSH."""
    if host == "local":
        return True, "Локальный хост"
    dest = f"{user}@{host}" if user and "@" not in host else host
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=no",
        "-p", str(port),
        dest,
        "echo __SSH_OK__",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
        if res.returncode == 0 and "__SSH_OK__" in res.stdout:
            return True, "Подключение успешно"
        err = res.stderr.strip() or f"Код возврата {res.returncode}"
        return False, err
    except subprocess.TimeoutExpired:
        return False, "Таймаут подключения"
    except Exception as e:
        return False, str(e)


def detect_local_docker_containers() -> list[str]:
    """Обнаруживает локальные контейнеры WireGuard / Amnezia."""
    try:
        res = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
        if res.returncode == 0:
            lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
            return [l for l in lines if "awg" in l or "wireguard" in l or "vpn" in l]
    except Exception:
        pass
    return []


def read_env_lines() -> list[str]:
    """Считывает строки из .env если существует."""
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            return f.readlines()
    elif os.path.exists(ENV_EXAMPLE):
        with open(ENV_EXAMPLE, "r", encoding="utf-8") as f:
            return f.readlines()
    return []


def write_env_key(key: str, value: str):
    """Обновляет или добавляет ключ в .env."""
    lines = read_env_lines()
    found = False
    new_lines = []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for line in lines:
        if pattern.match(line):
            new_lines.append(f'{key}="{value}"\n')
            found = True
        else:
            new_lines.append(line)
    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f'{key}="{value}"\n')

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    os.chmod(ENV_FILE, 0o600)


def restart_services_if_systemd():
    """Перезапуск системных служб если они установлены."""
    for svc in ["vpn-bot", "msk_bot", "vpn-webapp"]:
        try:
            subprocess.run(["sudo", "systemctl", "restart", svc], stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ─── КОМАНДЫ ─────────────────────────────────────────────────────────────────

def cmd_add_node():
    """Интерактивное добавление нового сервера/ноды."""
    print_banner("Добавление нового VPN-сервера")
    print("Мастер поможет быстро подключить новый сервер в систему.\n")

    alias = prompt("1. Отображаемое имя (с эмодзи)", "🇳🇱 NL Server")
    host_type = prompt("2. Это локальный или удаленный сервер? (remote/local)", "remote").lower()

    if host_type == "local":
        host = "local"
        port = "-"
        user = "-"
    else:
        host = prompt("   IP-адрес или домен сервера")
        if not host:
            print(red("❌ Ошибка: IP-адрес обязателен для удаленного сервера!"))
            return
        port = prompt("   SSH-порт", "22")
        user = prompt("   SSH-пользователь (с sudo без пароля)", "root")

        print(blue(f"\n🔍 Проверка SSH связи с {user}@{host}:{port}..."))
        ok, msg = test_ssh_connection(host, port, user)
        if ok:
            print(green("   ✓ Связь по SSH установлена успешно!"))
        else:
            print(yellow(f"   ⚠️ Предупреждение SSH: {msg}"))
            print(yellow("   Убедитесь, что настроен вход по SSH-ключу (ssh-copy-id)."))
            cont = prompt("   Все равно продолжить добавление? (y/N)", "n").lower()
            if cont not in ("y", "yes", "да"):
                print(yellow("Отменено."))
                return

    container = prompt("3. Имя Docker-контейнера на сервере", "amnezia-awg2")
    iface = prompt("4. Имя WireGuard-интерфейса в контейнере", "awg0")

    # Формируем строку NODE
    node_str = f"{alias}|{host}|{port}|{user}|{container}|{iface}"

    # Находим следующий свободный индекс NODE_X
    lines = read_env_lines()
    existing_indices = []
    for line in lines:
        m = re.match(r"^\s*NODE_(\d+)\s*=", line)
        if m:
            existing_indices.append(int(m.group(1)))

    next_idx = max(existing_indices, default=0) + 1
    key_name = f"NODE_{next_idx}"

    write_env_key(key_name, node_str)
    print("\n" + green(f"✓ Сервер успешно записан в .env под ключом {key_name}:"))
    print(f"  {bold(key_name)}={node_str}\n")

    ans = prompt("Перезапустить службы бота и WebApp прямо сейчас? (Y/n)", "y").lower()
    if ans in ("y", "yes", "да", ""):
        restart_services_if_systemd()
        print(green("✓ Службы перезапущены! Новый сервер уже доступен в Telegram и WebApp."))


def cmd_list_nodes():
    """Выводит список настроенных нод с проверкой статуса."""
    print_banner("Список настроенных VPN-серверов")
    try:
        from config import cfg
        from vpn_engine import get_node_metrics
    except Exception as e:
        print(red(f"❌ Ошибка импорта модулей: {e}"))
        return

    nodes = cfg.vpn_containers
    if not nodes:
        print(yellow("Серверы не найдены в конфигурации! Выполните: python3 manage.py add-node"))
        return

    print(f"{'#':<3} | {'Название':<25} | {'Хост':<18} | {'Контейнер':<15} | {'Статус':<20}")
    print("-" * 90)

    for i, node in enumerate(nodes, 1):
        alias = node.get("alias", f"Node {i}")[:25]
        host = str(node.get("host", "local"))[:18]
        cont = str(node.get("name", "amnezia-awg2"))[:15]
        
        print(f"{i:<3} | {alias:<25} | {host:<18} | {cont:<15} | Проверка...", end="\r")
        m = get_node_metrics(node)
        if m:
            cpu = m.get("cpu", "0")
            ram = m.get("ram", 0)
            status = green(f"🟢 Онлайн (CPU {cpu}%, RAM {ram}%)")
        else:
            status = red("🔴 Офлайн")

        print(f"{i:<3} | {alias:<25} | {host:<18} | {cont:<15} | {status}")

    print("\n" + blue("Чтобы добавить сервер:") + " python3 manage.py add-node\n")


def cmd_setup():
    """Интерактивный пошаговый мастер первоначальной установки."""
    print_banner("Мастер первоначальной настройки VPN Bot & WebApp")
    print("Этот мастер поможет быстро заполнить .env и запустить бота за 1 минуту.\n")

    # 1. Telegram Token
    token = prompt("Шаг 1: Введите токен Telegram-бота (от @BotFather)")
    while not token or ":" not in token:
        print(red("Токен бота должен содержать двоеточие (например: 123456789:ABCdef...)"))
        token = prompt("Введите токен Telegram-бота")

    # 2. Admin ID
    admin_id = prompt("Шаг 2: Введите ваш числовой Telegram ID (от @userinfobot)")
    while not admin_id.isdigit():
        print(red("Telegram ID должен состоять только из цифр!"))
        admin_id = prompt("Введите ваш числовой Telegram ID")

    # 3. Primary Node
    print("\nШаг 3: Настройка основного (локального) сервера VPN:")
    local_containers = detect_local_docker_containers()
    default_cont = local_containers[0] if local_containers else "amnezia-awg2"
    if local_containers:
        print(green(f"✓ Обнаружен Docker-контейнер Amnezia: {default_cont}"))

    cont_name = prompt("Имя Docker-контейнера", default_cont)
    srv_name = prompt("Отображаемое имя сервера в боте", "🇷🇺 RU Server (Main)")

    node_1 = f"{srv_name}|local|22|root|{cont_name}|awg0"

    write_env_key("BOT_TOKEN", token)
    write_env_key("ADMIN_ID", admin_id)
    write_env_key("NODE_1", node_1)

    # 4. Additional node?
    extra = prompt("\nШаг 4: Хотите подключить второй (удаленный) сервер прямо сейчас? (y/N)", "n").lower()
    if extra in ("y", "yes", "да"):
        cmd_add_node()

    print("\n" + green("=" * 60))
    print(bold(green("🎉 Настройка успешно завершена! Файл .env сгенерирован.")))
    print(green("=" * 60))
    print(blue("Для запуска бота выполните:") + " sudo systemctl restart vpn-bot vpn-webapp\n")


def cmd_test():
    """Проверка здоровья системы."""
    print_banner("Диагностика системы (Self-Test)")
    # 1. Python & Dependencies
    print("1. Проверка окружения Python...", end=" ")
    try:
        import telebot
        import fastapi
        import uvicorn
        import psutil
        print(green("OK"))
    except ImportError as e:
        print(red(f"ОШИБКА: {e}"))

    # 2. .env & Config
    print("2. Проверка конфигурации .env...", end=" ")
    if not os.path.exists(ENV_FILE):
        print(red(f"Файл {ENV_FILE} не найден! Выполните python3 manage.py setup"))
        return
    try:
        from config import cfg
        print(green(f"OK (Admin ID: {cfg.admin_id}, Серверов: {len(cfg.vpn_containers)})"))
    except Exception as e:
        print(red(f"ОШИБКА: {e}"))
        return

    # 3. Database
    print("3. Проверка SQLite базы данных...", end=" ")
    try:
        from database import init_db, get_all_users
        init_db()
        users = get_all_users()
        print(green(f"OK ({len(users)} абонентов зарегистрировано)"))
    except Exception as e:
        print(red(f"ОШИБКА: {e}"))

    # 4. Nodes
    print("4. Проверка нод...")
    cmd_list_nodes()


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 manage.py [команда]\n")
        print("Доступные команды:")
        print(f"  {bold('setup')}       — Пошаговый интерактивный установщик")
        print(f"  {bold('add-node')}    — Добавить новый VPN-сервер")
        print(f"  {bold('list-nodes')}  — Список всех серверов и их онлайн-статус")
        print(f"  {bold('test')}        — Проверка работоспособности системы\n")
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "setup":
        cmd_setup()
    elif cmd in ("add-node", "add_node", "add"):
        cmd_add_node()
    elif cmd in ("list-nodes", "list_nodes", "list", "nodes"):
        cmd_list_nodes()
    elif cmd in ("test", "status", "check"):
        cmd_test()
    else:
        print(red(f"Неизвестная команда: {cmd}"))
        print("Доступные команды: setup, add-node, list-nodes, test")
        sys.exit(1)


if __name__ == "__main__":
    main()
