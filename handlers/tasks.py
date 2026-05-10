"""
tasks.py — Фоновый планировщик.
Сбор метрик, авто-бэкап, ежедневная проверка биллинга, утренний дайджест.
"""
import os
import csv
import time
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

        # Проверка аномалий (не чаще 1 раза в 10 мин)
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
    """Отправляет ежедневный бэкап БД и метрик админу."""
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
    """
    Ежедневный утренний отчёт (09:00) — отправляется только если есть проблемы.
    Показывает: просроченных, истекающих сегодня/завтра, непривязанных.
    """
    try:
        now = datetime.now()
        all_users = get_all_users()

        expired, expiring, unbound = [], [], []

        for ip, data in all_users.items():
            alias = data.get("alias") or ip
            tg_id = data.get("tg_user_id")
            paid_until = data.get("paid_until")

            if not tg_id:
                unbound.append(f"  ⚫ {alias} ({ip})")
                continue

            if not paid_until or paid_until == "":
                continue

            try:
                exp = datetime.fromisoformat(paid_until)
                if exp.year >= 2099:
                    continue   # бессрочные — пропускаем
                days_left = (exp - now).days
                if days_left < 0:
                    expired.append(f"  🔴 {alias} — просрочен {abs(days_left)} дн.")
                elif days_left <= 2:
                    expiring.append(f"  🟡 {alias} — истекает через {days_left} дн. ({exp.strftime('%d.%m')})")
            except Exception:
                pass

        if not expired and not expiring and not unbound:
            return   # Всё в порядке — молчим

        lines = [f"📋 *Утренний дайджест* {now.strftime('%d.%m.%Y')}\n"]
        if expired:
            lines.append(f"🔴 *Просрочено ({len(expired)}):*")
            lines.extend(expired)
            lines.append("")
        if expiring:
            lines.append(f"🟡 *Истекают скоро ({len(expiring)}):*")
            lines.extend(expiring)
            lines.append("")
        if unbound:
            lines.append(f"⚫ *Без TG ({len(unbound)}):*")
            lines.extend(unbound)

        bot.send_message(cfg.admin_id, "\n".join(lines), parse_mode="Markdown")
        logger.info("Утренний дайджест отправлен.")
    except Exception as e:
        logger.error(f"Ошибка утреннего дайджеста: {e}")


# ─── Биллинг: ежедневная проверка ────────────────────────────────────────────

def check_billing(bot: "telebot.TeleBot") -> None:
    """
    Ежедневный воркер (запускается в cfg.billing_check_time).
    Логика:
      • осталось cfg.billing_warn_days дней  → напоминание
      • осталось cfg.billing_critical_days д → «завтра последний день»
      • просрочено                           → карцер + уведомление
    """
    logger.info("Запуск ежедневной проверки биллинга...")
    now = datetime.now()
    users = get_billing_users()

    for user in users:
        ip           = user["ip_address"]
        tg_id        = user["tg_user_id"]
        paid_until   = user["paid_until"]
        speed_limit  = user["speed_limit"]
        protocol     = user["protocol"]

        if not tg_id or not paid_until:
            continue

        try:
            expire_dt = datetime.fromisoformat(paid_until)
        except ValueError:
            logger.warning(f"check_billing: невалидная дата для {ip}: '{paid_until}'")
            continue

        # Бессрочные — пропускаем
        if expire_dt.year >= 2099:
            continue

        delta = expire_dt - now
        days_left = delta.days

        try:
            if days_left == cfg.billing_warn_days:
                bot.send_message(
                    tg_id,
                    f"⏳ *Внимание!* Через *{cfg.billing_warn_days} дня* заканчивается оплата VPN.\n\n"
                    f"Оплачено до: `{expire_dt.strftime('%d.%m.%Y')}`\n"
                    f"Нажмите /start → «💳 Оплатить/Продлить», чтобы не потерять доступ.",
                    parse_mode="Markdown",
                )
                logger.info(f"Предупреждение -3 дня отправлено tg_id={tg_id} (ip={ip})")

            elif days_left == cfg.billing_critical_days:
                bot.send_message(
                    tg_id,
                    f"🚨 *Завтра последний день!*\n\n"
                    f"Оплачено до: `{expire_dt.strftime('%d.%m.%Y')}`\n"
                    f"Пришлите чек об оплате через /start → «💳 Оплатить», чтобы не потерять скорость.",
                    parse_mode="Markdown",
                )
                logger.info(f"Предупреждение -1 день отправлено tg_id={tg_id} (ip={ip})")

            elif days_left < 0 and speed_limit != "punish":
                if protocol:
                    apply_limit(ip, "punish", protocol)
                update_user_field(ip, "speed_limit", "punish")
                log_audit("AUTO_PUNISH", target_ip=ip, details=f"paid_until={paid_until}")
                bot.send_message(
                    tg_id,
                    f"🛑 *Срок действия истёк.*\n\n"
                    f"Скорость ограничена до *1 Кбит/с*.\n"
                    f"Оплатите тариф и пришлите чек через /start → «💳 Оплатить» "
                    f"для автоматического восстановления скорости.",
                    parse_mode="Markdown",
                )
                logger.info(f"Карцер применён для ip={ip}, tg_id={tg_id}")

        except Exception as e:
            logger.error(f"check_billing: ошибка для ip={ip}, tg_id={tg_id}: {e}")

    logger.info("Проверка биллинга завершена.")


# ─── Запуск фонового планировщика ────────────────────────────────────────────

def start_scheduler(bot: "telebot.TeleBot") -> None:
    """Запускает фоновый поток с планировщиком задач."""
    schedule.every(5).minutes.do(collect_metrics, bot)
    schedule.every().day.at(cfg.backup_time).do(auto_backup, bot)
    schedule.every().day.at(cfg.billing_check_time).do(check_billing, bot)
    schedule.every().day.at("09:00").do(morning_digest, bot)

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

import os
import csv
import time
import threading
import contextlib
import schedule
import psutil
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from config import cfg, logger
from database import log_audit, get_billing_users, update_user_field
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

        # Проверка аномалий (не чаще 1 раза в 10 мин)
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
    """Отправляет ежедневный бэкап БД и метрик админу."""
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


# ─── Биллинг: ежедневная проверка ────────────────────────────────────────────

def check_billing(bot: "telebot.TeleBot") -> None:
    """
    Ежедневный воркер (запускается в cfg.billing_check_time).
    Логика:
      • осталось cfg.billing_warn_days дней  → напоминание
      • осталось cfg.billing_critical_days д → «завтра последний день»
      • просрочено                           → карцер + уведомление
    """
    logger.info("Запуск ежедневной проверки биллинга...")
    now = datetime.now()
    users = get_billing_users()

    for user in users:
        ip           = user["ip_address"]
        tg_id        = user["tg_user_id"]
        paid_until   = user["paid_until"]
        speed_limit  = user["speed_limit"]
        protocol     = user["protocol"]

        if not tg_id or not paid_until:
            continue

        try:
            expire_dt = datetime.fromisoformat(paid_until)
        except ValueError:
            logger.warning(f"check_billing: невалидная дата для {ip}: '{paid_until}'")
            continue

        delta = expire_dt - now
        days_left = delta.days

        try:
            # ── 3 дня до конца ──────────────────────────────────────────
            if days_left == cfg.billing_warn_days:
                bot.send_message(
                    tg_id,
                    f"⏳ *Внимание!* Через *{cfg.billing_warn_days} дня* заканчивается оплата VPN.\n\n"
                    f"Оплачено до: `{expire_dt.strftime('%d.%m.%Y')}`\n"
                    f"Нажмите /start → «💳 Оплатить/Продлить», чтобы не потерять доступ.",
                    parse_mode="Markdown",
                )
                logger.info(f"Предупреждение -3 дня отправлено tg_id={tg_id} (ip={ip})")

            # ── 1 день до конца ─────────────────────────────────────────
            elif days_left == cfg.billing_critical_days:
                bot.send_message(
                    tg_id,
                    f"🚨 *Завтра последний день!*\n\n"
                    f"Оплачено до: `{expire_dt.strftime('%d.%m.%Y')}`\n"
                    f"Пришлите чек об оплате через /start → «💳 Оплатить», чтобы не потерять скорость.",
                    parse_mode="Markdown",
                )
                logger.info(f"Предупреждение -1 день отправлено tg_id={tg_id} (ip={ip})")

            # ── Просрочено ───────────────────────────────────────────────
            elif days_left < 0 and speed_limit != "punish":
                if protocol:
                    apply_limit(ip, "punish", protocol)
                update_user_field(ip, "speed_limit", "punish")
                log_audit("AUTO_PUNISH", target_ip=ip, details=f"paid_until={paid_until}")

                bot.send_message(
                    tg_id,
                    f"🛑 *Срок действия истёк.*\n\n"
                    f"Скорость ограничена до *1 Кбит/с*.\n"
                    f"Оплатите тариф и пришлите чек через /start → «💳 Оплатить» "
                    f"для автоматического восстановления скорости.",
                    parse_mode="Markdown",
                )
                logger.info(f"Карцер применён для ip={ip}, tg_id={tg_id}")

        except Exception as e:
            logger.error(f"check_billing: ошибка для ip={ip}, tg_id={tg_id}: {e}")

    logger.info("Проверка биллинга завершена.")


# ─── Запуск фонового планировщика ────────────────────────────────────────────

def start_scheduler(bot: "telebot.TeleBot") -> None:
    """Запускает фоновый поток с планировщиком задач."""
    schedule.every(5).minutes.do(collect_metrics, bot)
    schedule.every().day.at(cfg.backup_time).do(auto_backup, bot)
    schedule.every().day.at(cfg.billing_check_time).do(check_billing, bot)

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
