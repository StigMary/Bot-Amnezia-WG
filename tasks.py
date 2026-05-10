"""
tasks.py — Фоновый планировщик.
Сбор метрик, авто-бэкап, биллинг, утренний дайджест, виджет мониторинга.
"""
import os
import csv
import time
import json
import threading
import contextlib
import schedule
import psutil
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from config import cfg, logger
from database import log_audit, get_billing_users, get_all_users, update_user_field
from vpn_engine import apply_limit

if TYPE_CHECKING:
    import telebot


# ─── Сбор метрик ─────────────────────────────────────────────────────────────

_last_anomaly_alert: float = 0.0


def collect_metrics(bot: "telebot.TeleBot") -> None:
    """Записывает CPU/RAM/Traffic в CSV и проверяет аномалии."""
    global _last_anomaly_alert
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory().percent
        net = psutil.net_io_counters()
        total_mb = (net.bytes_sent + net.bytes_recv) / 1024 / 1024
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        file_exists = os.path.isfile(cfg.metrics_file)
        with open(cfg.metrics_file, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow(["Timestamp", "CPU", "RAM", "Traffic_MB"])
            w.writerow([ts, cpu, ram, round(total_mb, 2)])

        now = time.time()
        if now - _last_anomaly_alert >= 600:
            alerts = []
            if cpu > cfg.anomaly_cpu_threshold:
                alerts.append(f"🔥 CPU: {cpu}% (порог: {cfg.anomaly_cpu_threshold}%)")
            if ram > cfg.anomaly_ram_threshold:
                alerts.append(f"💾 RAM: {ram}% (порог: {cfg.anomaly_ram_threshold}%)")
            if alerts:
                _last_anomaly_alert = now
                msg = "🚨 *АНОМАЛИЯ НА SE СЕРВЕРЕ!*\n\n" + "\n".join(alerts)
                with contextlib.suppress(Exception):
                    bot.send_message(cfg.admin_id, msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка сбора метрик: {e}")


# ─── Авто-бэкап ──────────────────────────────────────────────────────────────

def auto_backup(bot: "telebot.TeleBot") -> None:
    try:
        bot.send_message(cfg.admin_id, "🕐 *Автоматический бэкап (ежедневный)*", parse_mode="Markdown")
        for path, caption in [
            (cfg.db_file,      "🗄 База данных"),
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

_WIDGET_FILE = "/home/vpnuser/vpn_bot/data/widget.json"

# Трекеры скорости SE (локальный)
_last_net_io   = None
_last_net_time = 0.0

# Трекеры скорости RF (удалённый)
_last_rf_recv  = 0
_last_rf_sent  = 0
_last_rf_time  = 0.0


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
    """Собирает текст виджета с реальными скоростями SE и RF."""
    global _last_net_io, _last_net_time, _last_rf_recv, _last_rf_sent, _last_rf_time
    from vpn_engine import get_rf_metrics

    now = time.time()

    # ── SE (локальный) ──────────────────────────────────────────────────────
    cpu_se  = psutil.cpu_percent(interval=None)
    ram_se  = psutil.virtual_memory().percent
    uptime_se = str(
        datetime.now() - datetime.fromtimestamp(psutil.boot_time())
    ).split(".")[0]

    curr_net = psutil.net_io_counters()
    if _last_net_io is not None and (now - _last_net_time) > 0:
        dt = now - _last_net_time
        dl_se = (curr_net.bytes_recv - _last_net_io.bytes_recv) / dt
        ul_se = (curr_net.bytes_sent - _last_net_io.bytes_sent) / dt
    else:
        dl_se = ul_se = 0.0
    _last_net_io   = curr_net
    _last_net_time = now

    # ── RF (удалённый) ──────────────────────────────────────────────────────
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

        rf_status = "`Online`"
        rf_cpu    = f"`{rf['cpu']:.1f}%`"
        rf_ram    = f"`{rf['ram']:.1f}%`"
        rf_uptime = f"`{rf['uptime']}`"
        rf_speed  = f"⬇️ `{_fmt_speed(dl_rf)}` | ⬆️ `{_fmt_speed(ul_rf)}`"
    else:
        rf_status = "`Offline`"
        rf_cpu = rf_ram = rf_uptime = "N/A"
        rf_speed = "❌ Данные недоступны"

    return (
        "📊 *KJZNNETx | Глобальный мониторинг*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🇸🇪 *Швеция (Main Node):*\n"
        f"🖥 CPU: `{cpu_se}%` | 💾 RAM: `{ram_se}%` | ⏱ `{uptime_se}`\n"
        f"⬇️ `{_fmt_speed(dl_se)}` | ⬆️ `{_fmt_speed(ul_se)}`\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🇷🇺 *Россия (RF Node):*\n"
        f"🚀 Status: {rf_status}\n"
        f"🖥 CPU: {rf_cpu} | 💾 RAM: {rf_ram} | ⏱ {rf_uptime}\n"
        f"{rf_speed}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 _Update:_ `{datetime.now().strftime('%H:%M:%S')}`"
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
    from database import get_billing_accounts, get_devices_by_tg_id
    now = datetime.now()
    accounts = get_billing_accounts()

    for account in accounts:
        tg_id      = account["tg_user_id"]
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
        devices   = get_devices_by_tg_id(tg_id)

        try:
            if days_left == cfg.billing_warn_days:
                bot.send_message(
                    tg_id,
                    f"⏳ *Внимание!* Через *{cfg.billing_warn_days} дня* истекает подписка.\n\n"
                    f"📅 Оплачено до: `{expire_dt.strftime('%d.%m.%Y')}`\n"
                    f"📱 Активных конфигураций: *{len(devices)}*\n\n"
                    f"Нажмите 💳 *Оплатить / Продлить* в меню бота.",
                    parse_mode="Markdown",
                )
            elif days_left == cfg.billing_critical_days:
                bot.send_message(
                    tg_id,
                    f"🚨 *Завтра последний день!*\n\n"
                    f"📅 Оплачено до: `{expire_dt.strftime('%d.%m.%Y')}`\n"
                    f"📱 Конфигураций: *{len(devices)}*\n\n"
                    f"Пришлите чек через 💳 *Оплатить / Продлить*.",
                    parse_mode="Markdown",
                )
            elif days_left < 0:
                # Применяем карцер ко ВСЕМ устройствам аккаунта
                punished = 0
                for d in devices:
                    if d["speed_limit"] != "punish" and d["protocol"]:
                        try:
                            apply_limit(d["ip_address"], "punish", d["protocol"])
                            update_user_field(d["ip_address"], "speed_limit", "punish")
                            punished += 1
                        except Exception as e:
                            logger.error(f"Карцер: ошибка для {d['ip_address']}: {e}")

                if punished > 0:
                    log_audit("AUTO_PUNISH", details=f"tg_id={tg_id}, devices={punished}, paid_until={paid_until}")
                    bot.send_message(
                        tg_id,
                        f"🛑 *Срок действия истёк.*\n\n"
                        f"Скорость ограничена до *1 Кбит/с* на всех конфигурациях.\n"
                        f"Чтобы восстановить доступ — оплатите и пришлите чек через 💳 *Оплатить / Продлить*.",
                        parse_mode="Markdown",
                    )
                    logger.info(f"Карцер применён: tg_id={tg_id}, устройств={punished}")
        except Exception as e:
            logger.error(f"check_billing: ошибка для tg_id={tg_id}: {e}")

    logger.info("Проверка биллинга завершена.")



# ─── Запуск фонового планировщика ────────────────────────────────────────────

def start_scheduler(bot: "telebot.TeleBot") -> None:
    """Запускает фоновый поток с планировщиком задач."""
    schedule.every(5).minutes.do(collect_metrics, bot)
    schedule.every().day.at(cfg.backup_time).do(auto_backup, bot)
    schedule.every().day.at(cfg.billing_check_time).do(check_billing, bot)
    schedule.every().day.at("09:00").do(morning_digest, bot)
    schedule.every(30).seconds.do(update_widget, bot)

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
