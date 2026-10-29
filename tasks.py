"""
tasks.py — Фоновый планировщик.
Сбор метрик, авто-бэкап, биллинг, утренний дайджест, виджет мониторинга.
"""

import contextlib
import csv
import json
import os
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

import psutil
import schedule

from pathlib import Path
from config import cfg, logger
from database import get_all_users, log_audit, update_user_field, log_server_stat, get_setting
from vpn_engine import apply_limit, check_and_restore_qos, get_node_metrics, get_ru_metrics, get_ru_monitor_data

if TYPE_CHECKING:
    import telebot


# ─── Сбор метрик ─────────────────────────────────────────────────────────────

_last_anomaly_alert: float = 0.0

_stat_last_se_net = None
_stat_last_se_time = 0.0
_stat_last_ru_net = None
_stat_last_ru_time = 0.0

_node_net_cache: dict[str, dict] = {}
_node_fail_counts: dict[str, int] = {}
_node_down_alerted: dict[str, bool] = {}

def collect_metrics(bot: "telebot.TeleBot") -> None:
    """Записывает CPU/RAM/Traffic в базу для истории (каждые 5 минут) для всех нод."""
    global _last_anomaly_alert, _node_net_cache, _node_fail_counts, _node_down_alerted
    try:
        now = time.time()
        for node in cfg.vpn_containers:
            alias = node["alias"]
            m = get_node_metrics(node)
            if m:
                if _node_down_alerted.get(alias):
                    _node_down_alerted[alias] = False
                    _node_fail_counts[alias] = 0
                    msg = f"✅ *СВЯЗЬ С СЕРВЕРОМ ВОССТАНОВЛЕНА!*\n\nСервер *{alias}* снова в сети и отвечает штатно."
                    with contextlib.suppress(Exception):
                        bot.send_message(cfg.admin_id, msg, parse_mode="Markdown")
                else:
                    _node_fail_counts[alias] = 0

                try:
                    cpu = float(m.get("cpu", 0))
                except Exception:
                    cpu = 0.0
                try:
                    ram = float(m.get("ram", 0))
                except Exception:
                    ram = 0.0

                rx = tx = 0.0
                last_net = _node_net_cache.get(alias)
                if last_net and (now - last_net["time"]) > 0:
                    dt = now - last_net["time"]
                    rx = max(0.0, (m.get("net_recv", 0) - last_net["recv"]) / dt)
                    tx = max(0.0, (m.get("net_sent", 0) - last_net["sent"]) / dt)
                _node_net_cache[alias] = {
                    "recv": m.get("net_recv", 0),
                    "sent": m.get("net_sent", 0),
                    "time": now,
                }

                log_server_stat(alias, cpu, ram, rx, tx)
                # Сохраняем и под старыми ключами SE / RU для совместимости
                if "SE" in alias:
                    log_server_stat("SE", cpu, ram, rx, tx)
                elif "RU" in alias:
                    log_server_stat("RU", cpu, ram, rx, tx)
            else:
                _node_fail_counts[alias] = _node_fail_counts.get(alias, 0) + 1
                if _node_fail_counts[alias] >= 2 and not _node_down_alerted.get(alias):
                    _node_down_alerted[alias] = True
                    host_str = node.get("host") or "Remote"
                    msg = (
                        f"🚨 *ВНИМАНИЕ: СЕРВЕР НЕДОСТУПЕН!*\n\n"
                        f"Сервер *{alias}* (`{host_str}`) не отвечает на запросы более 5 минут.\n"
                        "Проверьте доступность хоста!"
                    )
                    with contextlib.suppress(Exception):
                        bot.send_message(cfg.admin_id, msg, parse_mode="Markdown")

        check_and_restore_qos()

        # Проверка аномалий локального сервера
        if now - _last_anomaly_alert >= 600:
            alerts = []
            cpu_thresh = float(get_setting("anomaly_cpu_threshold", cfg.anomaly_cpu_threshold))
            ram_thresh = float(get_setting("anomaly_ram_threshold", cfg.anomaly_ram_threshold))
            local_cpu = psutil.cpu_percent(interval=None)
            local_ram = psutil.virtual_memory().percent

            if local_cpu > cpu_thresh:
                # Защита от ложных срабатываний: перепроверяем через 3 сек, чтобы отсеять 1-секундные всплески (cron, apt)
                time.sleep(3)
                cpu_confirm = psutil.cpu_percent(interval=2)
                load1, _, _ = os.getloadavg()
                num_cpus = os.cpu_count() or 1
                if cpu_confirm > cpu_thresh or (load1 / num_cpus * 100) > cpu_thresh:
                    alerts.append(f"🔥 CPU: {cpu_confirm:.1f}% (LA: {load1:.2f}, порог: {cpu_thresh}%)")
                else:
                    logger.info(f"Игнорирован кратковременный всплеск CPU: {local_cpu}% -> {cpu_confirm}% (LA: {load1:.2f})")
            if local_ram > ram_thresh:
                alerts.append(f"💾 RAM: {local_ram}% (порог: {ram_thresh}%)")
            if alerts:
                _last_anomaly_alert = now
                msg = "🚨 *АНОМАЛИЯ НА СЕРВЕРЕ!*\n\n" + "\n".join(alerts)
                with contextlib.suppress(Exception):
                    bot.send_message(cfg.admin_id, msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка сбора метрик: {e}")


# ─── Авто-бэкап ──────────────────────────────────────────────────────────────


def auto_backup(bot: "telebot.TeleBot") -> None:
    try:
        bot.send_message(cfg.admin_id, "🕐 *Автоматический бэкап (ежедневный)*", parse_mode="Markdown")
        for path, caption in [
            (cfg.db_file, "🗄 База данных бота (vpn_bot.db)"),
            ("/opt/3x-ui/db/x-ui.db", "🛡 База данных 3X-UI (x-ui.db)"),
            (cfg.metrics_file, "📈 Метрики (CSV)"),
        ]:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    bot.send_document(cfg.admin_id, f, caption=caption)
        log_audit("AUTO_BACKUP")
        logger.info("Авто-бэкап успешно отправлен.")
    except Exception as e:
        logger.error(f"Ошибка авто-бэкапа: {e}")


# ─── Утренний дайджест ────────────────────────────────────────────────────────


def morning_digest(bot: "telebot.TeleBot") -> None:
    """09:00 — отчёт только если есть проблемы."""
    try:
        now = datetime.now()
        all_users = get_all_users()
        expired, expiring = [], []

        for ip, data in all_users.items():
            alias = data.get("alias") or ip
            tg_id = data.get("tg_user_id")
            paid_until = data.get("paid_until")

            if not tg_id or not paid_until:
                continue
            try:
                exp = datetime.fromisoformat(paid_until)
                if exp.year >= 2099:
                    continue
                days_left = (exp - now).days
                if days_left < 0:
                    expired.append(f"  🔴 {alias} — просрочен {abs(days_left)} дн.")
                elif days_left <= 2:
                    expiring.append(f"  🟡 {alias} — истекает через {days_left} дн. ({exp.strftime('%d.%m')})")
            except Exception:
                pass

        if not expired and not expiring:
            return

        lines = [f"📋 *Утренний дайджест* {now.strftime('%d.%m.%Y')}\n"]
        if expired:
            lines.append(f"🔴 *Просрочено ({len(expired)}):*")
            lines.extend(expired)
            lines.append("")
        if expiring:
            lines.append(f"🟡 *Истекают скоро ({len(expiring)}):*")
            lines.extend(expiring)
            lines.append("")

        bot.send_message(cfg.admin_id, "\n".join(lines), parse_mode="Markdown")
        logger.info("Утренний дайджест отправлен.")
    except Exception as e:
        logger.error(f"Ошибка утреннего дайджеста: {e}")


# ─── Виджет мониторинга ───────────────────────────────────────────────────────

_WIDGET_FILE = os.path.join(cfg.data_dir, "widget.json")

# Трекеры скорости SE (локальный)
_last_net_io = None
_last_net_time = 0.0

# Трекеры скорости RF (удалённый)
_last_rf_recv = 0
_last_rf_sent = 0
_last_rf_time = 0.0


def widget_save(chat_id: int, msg_id: int) -> None:
    """Сохраняет chat_id и msg_id виджета на диск (переживает рестарт)."""
    with contextlib.suppress(Exception):
        with open(_WIDGET_FILE, "w") as _f:
            json.dump({"chat_id": chat_id, "msg_id": msg_id}, _f)


def widget_load():
    """Загружает сохранённые параметры виджета."""
    try:
        with open(_WIDGET_FILE) as _f:
            d = json.load(_f)
            return d["chat_id"], d["msg_id"]
    except Exception:
        return None, None


def _fmt_speed(bytes_per_sec: float) -> str:
    """Форматирует скорость в bps / Kbps / Mbps / Gbps."""
    bits = bytes_per_sec * 8
    for unit in ["bps", "Kbps", "Mbps", "Gbps"]:
        if bits < 1024:
            return f"{bits:.1f} {unit}"
        bits /= 1024
    return f"{bits:.1f} Tbps"


def _get_widget_text() -> str:
    """Собирает текст виджета с реальными скоростями SE и RU."""
    global _last_net_io, _last_net_time, _last_rf_recv, _last_rf_sent, _last_rf_time
    from vpn_engine import get_ru_metrics as get_rf_metrics, get_vpn_stats

    now = time.time()

    # ── Онлайн-счётчик ─────────────────────────────────────────────────────
    try:
        _stats = get_vpn_stats()
        _online = sum(1 for u in _stats if u["online_emoji"] == "🟢")
        _total = len(_stats)
    except Exception:
        _online = _total = 0

    # ── SE (локальный) ──────────────────────────────────────────────────────
    cpu_se = psutil.cpu_percent(interval=None)
    ram_se = psutil.virtual_memory().percent
    uptime_se = str(datetime.now() - datetime.fromtimestamp(psutil.boot_time())).split(".")[0]
    # убираем дни если меньше суток
    if uptime_se.startswith("0:"):
        uptime_se = uptime_se[2:]

    curr_net = psutil.net_io_counters()
    if _last_net_io is not None and (now - _last_net_time) > 0:
        dt = now - _last_net_time
        dl_se = (curr_net.bytes_recv - _last_net_io.bytes_recv) / dt
        ul_se = (curr_net.bytes_sent - _last_net_io.bytes_sent) / dt
    else:
        dl_se = ul_se = 0.0
    _last_net_io = curr_net
    _last_net_time = now

    def _mini_bar(pct, width=6):
        filled = int(round(pct / 100 * width))
        return "█" * filled + "░" * (width - filled)

    se_cpu_bar = _mini_bar(cpu_se)
    se_ram_bar = _mini_bar(ram_se)

    # ── RU (удалённый) ──────────────────────────────────────────────────────
    rf = get_rf_metrics()
    if rf:
        if _last_rf_recv > 0 and (now - _last_rf_time) > 0:
            dt_rf = now - _last_rf_time
            dl_rf = (rf["net_recv"] - _last_rf_recv) / dt_rf
            ul_rf = (rf["net_sent"] - _last_rf_sent) / dt_rf
        else:
            dl_rf = ul_rf = 0.0
        _last_rf_recv = rf["net_recv"]
        _last_rf_sent = rf["net_sent"]
        _last_rf_time = now

        ru_cpu_bar = _mini_bar(float(rf["cpu"]))
        ru_ram_bar = _mini_bar(float(rf["ram"]))
        ru_uptime = str(rf["uptime"])
        if ru_uptime.startswith("0:"):
            ru_uptime = ru_uptime[2:]

        ru_block = (
            "🇷🇺 *Россия  ·  RU Node*\n"
            f"🖥 `{ru_cpu_bar}` {float(rf.get('cpu', 0)):.0f}%   💾 `{ru_ram_bar}` {float(rf.get('ram', 0)):.0f}%\n"
            f"⬇️ `{_fmt_speed(dl_rf)}`   ⬆️ `{_fmt_speed(ul_rf)}`\n"
            f"⏱ `{ru_uptime}`"
        )
    else:
        ru_block = "🇷🇺 *Россия  ·  RU Node*\n❌ `Недоступен`"

    _time_str = datetime.now().strftime("%H:%M:%S")
    _online_line = f"🟢 {_online} онлайн  |  👥 {_total} всего" if _total else ""

    return (
        "📊 *KJZNNETx  |  Мониторинг*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🇸🇪 *Швеция  ·  SE Node*\n"
        f"🖥 `{se_cpu_bar}` {cpu_se:.0f}%   💾 `{se_ram_bar}` {ram_se:.0f}%\n"
        f"⬇️ `{_fmt_speed(dl_se)}`   ⬆️ `{_fmt_speed(ul_se)}`\n"
        f"⏱ `{uptime_se}`\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{ru_block}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + (f"{_online_line}\n" if _online_line else "")
        + f"🔄 `{_time_str}`"
    )


def update_widget(bot: "telebot.TeleBot") -> None:
    """Обновляет закреплённый виджет каждые 30 сек."""
    chat_id, msg_id = widget_load()
    if not chat_id or not msg_id:
        return
    try:
        text = _get_widget_text()
        with contextlib.suppress(Exception):
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка обновления виджета: {e}")


# ─── Биллинг: ежедневная проверка ────────────────────────────────────────────


def check_billing(bot: "telebot.TeleBot") -> None:
    logger.info("Запуск ежедневной проверки биллинга...")
    from database import get_billing_accounts, get_devices_by_tg_id, was_notified_today, mark_notified

    now = datetime.now()
    accounts = get_billing_accounts()

    for account in accounts:
        tg_id = account["tg_user_id"]
        paid_until = account["paid_until"]

        if not tg_id or not paid_until:
            continue
        try:
            expire_dt = datetime.fromisoformat(paid_until)
        except ValueError:
            logger.warning(f"check_billing: невалидная дата для tg_id={tg_id}: '{paid_until}'")
            continue

        if expire_dt.year >= 2099:
            continue

        days_left = (expire_dt - now).days
        devices = get_devices_by_tg_id(tg_id)

        try:
            warn_days = int(get_setting("billing_warn_days", cfg.billing_warn_days))
            crit_days = int(get_setting("billing_critical_days", cfg.billing_critical_days))

            # --- Карцер (приоритет выше предупреждений) ---
            if days_left < 0:
                punished = 0
                for d in devices:
                    if d["speed_limit"] != "punish" and d["protocol"]:
                        try:
                            current_limit = d["speed_limit"] or "max"
                            update_user_field(d["ip_address"], "prev_speed_limit", current_limit)
                            apply_limit(d["ip_address"], "punish", d["protocol"])
                            update_user_field(d["ip_address"], "speed_limit", "punish")
                            punished += 1
                        except Exception as e:
                            logger.error(f"Карцер: ошибка для {d['ip_address']}: {e}")

                if punished > 0 and not was_notified_today(tg_id, "punish"):
                    mark_notified(tg_id, "punish")
                    log_audit("AUTO_PUNISH", details=f"tg_id={tg_id}, devices={punished}, paid_until={paid_until}")
                    bot.send_message(
                        tg_id,
                        "🛑 *Срок действия истёк.*\n\n"
                        "Скорость ограничена до *1 Кбит/с* на всех конфигурациях.\n"
                        "Чтобы восстановить доступ — оплатите и пришлите чек через 💳 *Оплатить / Продлить*.",
                        parse_mode="Markdown",
                    )
                    logger.info(f"Карцер применён: tg_id={tg_id}, устройств={punished}")
                elif abs(days_left) in (3, 7, 14, 30) and not was_notified_today(tg_id, f"punish_{abs(days_left)}"):
                    mark_notified(tg_id, f"punish_{abs(days_left)}")
                    bot.send_message(
                        tg_id,
                        f"🛑 *Напоминание: подписка истекла {abs(days_left)} дн. назад.*\n\n"
                        "Скорость ограничена до *1 Кбит/с*.\n"
                        "Нажмите 💳 *Оплатить / Продлить*, чтобы восстановить доступ.",
                        parse_mode="Markdown",
                    )

            # --- Критическое предупреждение (≤ crit_days, но не в прошлом) ---
            elif days_left <= crit_days:
                if not was_notified_today(tg_id, "crit"):
                    mark_notified(tg_id, "crit")
                    if days_left == 0:
                        warn_title = "🚨 *Сегодня последний день действия подписки!*"
                        action_text = "Продлите сегодня, чтобы доступ не отключился."
                    elif days_left == 1:
                        warn_title = "🚨 *Завтра последний день подписки! (Остался 1 день)*"
                        action_text = "Пришлите чек через 💳 *Оплатить / Продлить*."
                    else:
                        warn_title = f"🚨 *Осталось {days_left} дн. до конца подписки!*"
                        action_text = "Пришлите чек через 💳 *Оплатить / Продлить*."

                    bot.send_message(
                        tg_id,
                        f"{warn_title}\n\n"
                        f"📅 Оплачено до: `{expire_dt.strftime('%d.%m.%Y')}`\n"
                        f"📱 Конфигураций: *{len(devices)}*\n\n"
                        f"{action_text}",
                        parse_mode="Markdown",
                    )

            # --- Предупреждение заранее (≤ warn_days, но выше критического порога) ---
            elif days_left <= warn_days:
                if not was_notified_today(tg_id, "warn"):
                    mark_notified(tg_id, "warn")
                    bot.send_message(
                        tg_id,
                        f"⏳ *Внимание!* Через *{days_left} дн.* истекает подписка.\n\n"
                        f"📅 Оплачено до: `{expire_dt.strftime('%d.%m.%Y')}`\n"
                        f"📱 Активных конфигураций: *{len(devices)}*\n\n"
                        f"Нажмите 💳 *Оплатить / Продлить* в меню бота.",
                        parse_mode="Markdown",
                    )
        except Exception as e:
            msg = str(e).lower()
            if "chat not found" in msg or "user is deactivated" in msg:
                logger.info(f"check_billing: пользователь {tg_id} недоступен, пропускаем")
            else:
                logger.error(f"check_billing: ошибка для tg_id={tg_id}: {e}")

    logger.info("Проверка биллинга завершена.")


# ─── Мониторинг Xray (алерт при краше) ───────────────────────────────────────

_XRAY_LAST_CRASH_FILE = Path(cfg.data_dir) / "xray_last_crash.txt"


def _load_last_crash_ts() -> str:
    try:
        return _XRAY_LAST_CRASH_FILE.read_text().strip()
    except FileNotFoundError:
        return ""
    except Exception:
        logger.exception("Не удалось прочитать last crash ts")
        return ""


def _save_last_crash_ts(ts: str) -> None:
    try:
        _XRAY_LAST_CRASH_FILE.write_text(ts)
    except Exception:
        logger.exception("Не удалось сохранить last crash ts")


_last_seen_crash_ts: str = _load_last_crash_ts()


def check_xray_crash(bot: "telebot.TeleBot") -> None:
    """Каждые 2 мин читает awg_monitor.log, шлёт алерт при новом краше Xray."""
    global _last_seen_crash_ts
    try:
        data = get_ru_monitor_data(force=True)
        if data.get("error"):
            return

        events = data.get("events", [])
        crash_events = [e for e in events if e.get("is_crash")]
        if not crash_events:
            return

        latest = crash_events[-1]
        if latest["ts"] == _last_seen_crash_ts:
            return  # уже отправляли

        _last_seen_crash_ts = latest["ts"]
        _save_last_crash_ts(latest["ts"])

        active = data.get("active_now", [])
        active_str = ""
        if active:
            active_str = "\n👥 *Активные в момент краша:*"
            for c in active[:5]:
                active_str += f"\n  • {c['name']} {c['ip']}  ↑{c['up_kb']}KB ↓{c['dn_kb']}KB"

        text = (
            f"⚠️ *XRAY ПЕРЕЗАПУСТИЛСЯ* на 🇷🇺 RU\n"
            f"🕐 `{latest['ts']}`\n"
            f"🔄 PID: `{data.get('xray_pid', '?')}`\n"
            f"🌐 Соединений к SE: `{data.get('conn_sw', '?')}`\n"
            f"{active_str}"
        )
        bot.send_message(cfg.admin_id, text, parse_mode="Markdown")
        logger.warning(f"Xray алерт отправлен: {latest['ts']}")
    except Exception as e:
        logger.error(f"check_xray_crash: {e}")


# ─── Запуск фонового планировщика ────────────────────────────────────────────


def start_scheduler(bot: "telebot.TeleBot") -> None:
    """Запускает фоновый поток с планировщиком задач."""
    schedule.clear()
    schedule.every(5).minutes.do(collect_metrics, bot)
    schedule.every().day.at(cfg.backup_time).do(auto_backup, bot)
    schedule.every().day.at(cfg.billing_check_time).do(check_billing, bot)
    schedule.every().day.at("09:00").do(morning_digest, bot)
    schedule.every(30).seconds.do(update_widget, bot)
    schedule.every(2).minutes.do(check_xray_crash, bot)

    def _run():
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Ошибка в планировщике: {e}")
                time.sleep(5)

    thread = threading.Thread(target=_run, daemon=True, name="scheduler")
    thread.start()
    logger.info("Фоновый планировщик запущен.")
