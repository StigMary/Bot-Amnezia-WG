"""
handlers/admin.py — Административная панель (ЦУП).
"""
import os
import csv
import sqlite3
import tempfile
import contextlib
import subprocess
import psutil
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

import telebot
from telebot.types import (
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
    KeyboardButton, KeyboardButtonRequestUser,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# {admin_id: ip} — пендинг привязки: какой IP ждёт контакт
_bind_pending: dict = {}

from config import cfg, logger
from database import (
    log_audit, get_audit_log, get_all_users, update_user_field,
    delete_user, bind_tg_to_ip, get_user_by_ip, extend_paid_until
)
from vpn_engine import (
    get_vpn_stats, apply_limit, delete_peer,
    validate_vpn_ip, sanitize_alias, get_rf_ping,
    get_rf_metrics, run_vpn_cmd
)
from handlers.decorators import admin_only, admin_only_callback, rate_limit


def register(bot: telebot.TeleBot):

    # ── /start — админка ────────────────────────────────────────────────────
    @bot.message_handler(commands=["start"], func=lambda m: m.from_user.id == cfg.admin_id)
    def cmd_start_admin(message):
        if message.chat.type != "private":
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            return
            
        # ── Панель администратора ──────────────────────────────────────
        markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add("👥 Клиенты", "⚡ Скорость")
        markup.add("📡 Серверы", "🛠 Сервис")
        bot.reply_to(message, "👑 KJZNNETx Admin", reply_markup=markup)


    # ── /bind <IP> <TG_ID> ───────────────────────────────────────────────────
    @bot.message_handler(commands=["bind"])
    @admin_only
    def cmd_bind(message):
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(message, "ℹ️ Использование: `/bind <IP> <TG_ID>`", parse_mode="Markdown")
            return
        ip, tg_id_str = parts[1], parts[2]
        if not validate_vpn_ip(ip):
            bot.reply_to(message, "⛔ Невалидный VPN IP.")
            return
        try:
            tg_id = int(tg_id_str)
        except ValueError:
            bot.reply_to(message, "⛔ TG_ID должен быть числом.")
            return
        bind_tg_to_ip(ip, tg_id)
        log_audit("BIND", target_ip=ip, details=f"tg_id={tg_id}", admin_id=message.from_user.id)
        bot.reply_to(message, f"✅ IP `{ip}` привязан к TG ID `{tg_id}`.", parse_mode="Markdown")

    # ── /backup ───────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["backup"])
    @admin_only
    @rate_limit(30)
    def cmd_backup(message):
        bot.send_message(message.chat.id, "📦 Собираю бэкапы...")
        try:
            for path, caption in [(cfg.db_file, "🗄 БД"), (cfg.metrics_file, "📈 Метрики")]:
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        bot.send_document(message.chat.id, f, caption=caption)
            log_audit("BACKUP", admin_id=message.from_user.id)
        except Exception as e:
            logger.error(f"Ошибка бэкапа: {e}")
            bot.send_message(message.chat.id, "❌ Ошибка. Подробности в логах.")

    # ── /search ───────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["search"])
    @admin_only
    def cmd_search(message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "ℹ️ `/search <имя или IP>`", parse_mode="Markdown")
            return
        query = parts[1].strip().lower()
        stats = get_vpn_stats()
        results = [s for s in stats if query in s["db_alias"].lower() or query in s["ip"]]
        if not results:
            bot.reply_to(message, f"🔍 Ничего не найдено по «{sanitize_alias(query)}».")
            return
        markup = InlineKeyboardMarkup(row_width=1)
        for u in results[:15]:
            flag = "🇸🇪" if "SE" in u["server_alias"] else "🇷🇺"
            markup.add(InlineKeyboardButton(
                f"{u['online_emoji']} {flag} {u['db_alias']} ({u['ip']})",
                callback_data=f"ip|{u['ip']}|{u['server_alias']}"
            ))
        bot.reply_to(message, f"🔍 Найдено: {len(results)}", reply_markup=markup)

    # ── /widget ───────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["widget"])
    @admin_only
    def cmd_widget(message):
        from tasks import widget_save
        msg = bot.send_message(message.chat.id, "⏳ Запуск мониторинга...")
        bot._widget_chat_id = message.chat.id
        bot._widget_msg_id  = msg.message_id
        widget_save(message.chat.id, msg.message_id)
        bot.pin_chat_message(message.chat.id, msg.message_id)
        bot.reply_to(message, "✅ Виджет создан. Обновление каждые 30 сек.")

    # ── Текстовые команды (4 кнопки главного меню) ───────────────────────────
    @bot.message_handler(func=lambda m: m.text in (
        "👥 Клиенты", "⚡ Скорость", "📡 Серверы", "🛠 Сервис"
    ))
    @admin_only
    @rate_limit(2)
    def handle_menu(message):
        text = message.text

        if text == "👥 Клиенты":
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(
                InlineKeyboardButton("📋 Все пользователи", callback_data="menu_all_users"),
                InlineKeyboardButton("📢 Рассылка",         callback_data="menu_broadcast"),
                InlineKeyboardButton("📁 Экспорт CSV",      callback_data="menu_export"),
            )
            bot.send_message(message.chat.id, "👥 *Клиенты*", reply_markup=markup, parse_mode="Markdown")

        elif text == "⚡ Скорость":
            _send_users_page(bot, message.chat.id, 0)

        elif text == "📡 Серверы":
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("🖥 Статус",      callback_data="menu_status"),
                InlineKeyboardButton("📈 Нагрузка",    callback_data="menu_analysis"),
                InlineKeyboardButton("🩺 Пинг (МСК)", callback_data="menu_ping"),
            )
            markup.add(
                InlineKeyboardButton("🔄 Рестарт SE", callback_data="restart_confirm|SE"),
                InlineKeyboardButton("🔄 Рестарт RF", callback_data="restart_confirm|RF"),
            )
            bot.send_message(message.chat.id, "📡 *Серверы*", reply_markup=markup, parse_mode="Markdown")

        elif text == "🛠 Сервис":
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(
                InlineKeyboardButton("📋 Журнал действий", callback_data="menu_journal"),
                InlineKeyboardButton("💾 Бэкап сейчас",    callback_data="menu_backup"),
                InlineKeyboardButton("🔔 Дайджест",        callback_data="menu_digest"),
            )
            bot.send_message(message.chat.id, "🛠 *Сервис*", reply_markup=markup, parse_mode="Markdown")

    # ── Inline-обработчики подменю ────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("menu_"))
    @admin_only_callback
    def cb_menu(call):
        action = call.data
        bot.answer_callback_query(call.id)

        if action == "menu_all_users":
            w = bot.send_message(call.message.chat.id, "⏳ Опрашиваю серверы...")
            stats = get_vpn_stats()
            bot.delete_message(call.message.chat.id, w.message_id)
            if not stats:
                return bot.send_message(call.message.chat.id, "❌ Нет данных.")
            msg = f"🌍 *Глобальная сеть ({len(stats)} шт):*\n\n"
            for s in stats:
                sp = "🚀 Макс." if s["speed_limit"] == "max" else f"⏱ {s['speed_limit']} Мб/с"
                msg += (f"{s['online_emoji']} *{s['db_alias']}* [{s['server_alias']}]\n"
                        f"📱 `{s['ip']}` | ⚙️ {sp}\n"
                        f"👁 Был: {s['last_seen']}\n"
                        f"⬇️ {s['downloaded']} | ⬆️ {s['uploaded']}\n" + "➖"*10 + "\n")
            for chunk in _split(msg):
                bot.send_message(call.message.chat.id, chunk, parse_mode="Markdown")

        elif action == "menu_broadcast":
            msg = bot.send_message(
                call.message.chat.id,
                "📢 *Рассылка*\nНапишите сообщение:",
                parse_mode="Markdown"
            )
            bot.register_next_step_handler(msg, _do_broadcast)

        elif action == "menu_export":
            _export_csv(bot, call.message.chat.id)

        elif action == "menu_status":
            w = bot.send_message(call.message.chat.id, "⏳ Снимаю телеметрию...")
            cpu = psutil.cpu_percent(1)
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage("/").percent
            result = f"🇸🇪 *SE Server:*\n🖥 CPU: {cpu}%\n💾 RAM: {ram}%\n💽 Disk: {disk}%\n\n"
            rf = get_rf_metrics()
            if rf:
                result += (f"🇷🇺 *RF Server:*\n🖥 CPU: {rf['cpu']:.1f}%\n"
                           f"💾 RAM: {rf['ram']:.1f}%\n⏱ {rf['uptime']}")
            else:
                result += "🇷🇺 *RF Server:*\n❌ Недоступен"
            bot.edit_message_text(result, call.message.chat.id, w.message_id, parse_mode="Markdown")

        elif action == "menu_analysis":
            bot.send_message(call.message.chat.id, "📊 Рисую график...")
            report = _daily_analysis()
            chart = _generate_chart()
            if chart:
                try:
                    with open(chart, "rb") as p:
                        bot.send_photo(call.message.chat.id, p, caption=report, parse_mode="Markdown")
                finally:
                    with contextlib.suppress(OSError):
                        os.remove(chart)
            else:
                bot.send_message(call.message.chat.id, report, parse_mode="Markdown")

        elif action == "menu_ping":
            bot.send_message(call.message.chat.id, "🩺 Измеряю пинг с МСК...")
            pings = get_rf_ping()
            if len(pings) == 5:
                def fmt(v): return f"✅ {v} мс" if v != "timeout" else "❌ timeout"
                t = (f"🩺 *Пинг (МСК):*\n\n"
                     f"🌍 Google: {fmt(pings[0])}\n"
                     f"🛡 Cloudflare: {fmt(pings[1])}\n"
                     f"🇷🇺 Yandex: {fmt(pings[2])}\n"
                     f"✈️ Telegram: {fmt(pings[3])}\n"
                     f"📸 Instagram: {fmt(pings[4])}")
                bot.send_message(call.message.chat.id, t, parse_mode="Markdown")
            else:
                bot.send_message(call.message.chat.id, "❌ Нет ответа от РФ-сервера.")

        elif action == "menu_journal":
            rows = get_audit_log(15)
            if not rows:
                return bot.send_message(call.message.chat.id, "📋 Журнал пуст.")
            msg = "📋 *Последние действия:*\n\n"
            for r in rows:
                ip_s = f" `{r['target_ip']}`" if r["target_ip"] else ""
                det_s = f" — {r['details']}" if r["details"] else ""
                msg += f"🕐 {r['timestamp']}\n🔹 {r['action']}{ip_s}{det_s}\n\n"
            for chunk in _split(msg):
                bot.send_message(call.message.chat.id, chunk, parse_mode="Markdown")

        elif action == "menu_backup":
            bot.send_message(call.message.chat.id, "📦 Собираю бэкап...")
            for path, cap in [(cfg.db_file, "🗄 БД"), (cfg.metrics_file, "📈 Метрики")]:
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        bot.send_document(call.message.chat.id, f, caption=cap)
            log_audit("BACKUP", admin_id=call.from_user.id)

        elif action == "menu_digest":
            from tasks import morning_digest
            morning_digest(bot)
            bot.send_message(call.message.chat.id, "✅ Дайджест отправлен.")

    def _do_broadcast(message):
        if message.from_user.id != cfg.admin_id:
            return
        if not message.text or message.text.strip() == "":
            bot.reply_to(message, "❌ Пустое сообщение. Отмена.")
            return

        # -- Защита от случайной рассылки при попытке отмены --
        txt = message.text.strip().lower() if message.text else ''
        abort_kws = ['клиент', 'скорость', 'сервер', 'сервис', 'отмена', 'cancel', '/cancel']
        if any(kw in txt for kw in abort_kws):
            bot.reply_to(message, '🚫 Рассылка отменена.')
            return
        all_users = get_all_users()
        unique_tg_ids = set()
        for data in all_users.values():
            tid = data.get("tg_user_id")
            if tid:
                unique_tg_ids.add(tid)

        sent, failed = 0, 0
        for tg_id in unique_tg_ids:
            try:
                bot.send_message(tg_id, f"📢 {message.text}")
                sent += 1
            except Exception:
                failed += 1
        log_audit("BROADCAST", details=f"sent={sent}, failed={failed}", admin_id=message.from_user.id)
        bot.reply_to(message, f"✅ Рассылка завершена: отправлено *{sent}*, ошибка *{failed}*.",
                     parse_mode="Markdown")

    def _export_csv(bot, chat_id):
        import io
        all_users = get_all_users()
        output = io.StringIO()
        w = csv.writer(output)
        w.writerow(["IP", "Имя", "TG ID", "Протокол", "Оплачено до", "Скорость"])
        for ip, d in sorted(all_users.items()):
            paid = d.get("paid_until") or ""
            if paid and len(paid) >= 10:
                try:
                    from datetime import datetime as _dt
                    exp = _dt.fromisoformat(paid)
                    paid = "Бессрочно" if exp.year >= 2099 else exp.strftime("%d.%m.%Y")
                except Exception:
                    pass
            w.writerow([
                ip,
                d.get("alias") or "—",
                d.get("tg_user_id") or "—",
                d.get("protocol") or "—",
                paid or "—",
                d.get("speed_limit") or "max",
            ])
        output.seek(0)
        from datetime import datetime as _dt
        fname = f"vpn_export_{_dt.now().strftime('%d%m%Y')}.csv"
        bot.send_document(chat_id, (fname, output.read().encode("utf-8-sig")),
                          caption=f"📁 Экспорт клиентов: {len(all_users)} записей")



    # ── Callbacks ─────────────────────────────────────────────────────────────

    @bot.callback_query_handler(func=lambda c: c.data == "cancel_action")
    @admin_only_callback
    def cb_cancel(call):
        bot.edit_message_text("❌ Действие отменено.", call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("page|"))
    @admin_only_callback
    def cb_page(call):
        parts = call.data.split("|")
        page   = int(parts[1])
        flt    = parts[2] if len(parts) > 2 else "all"
        _send_users_page(bot, call.message.chat.id, page, call.message.message_id, flt)

    # ── Фильтры по списку ────────────────────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("filter|"))
    @admin_only_callback
    def cb_filter(call):
        flt = call.data.split("|")[1]
        bot.answer_callback_query(call.id)
        _send_users_page(bot, call.message.chat.id, 0, call.message.message_id, flt)

    # ── Статистика клиента ─────────────────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("client_stats|"))
    @admin_only_callback
    def cb_client_stats(call):
        ip = call.data.split("|")[1]
        stats = get_vpn_stats()
        user_stat = next((s for s in stats if s["ip"] == ip), None)
        if not user_stat:
            bot.answer_callback_query(call.id, "❌ Данных нет.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"📊 *Статистика*: {user_stat['db_alias']}\n"
            f"🌍 IP: `{ip}`\n"
            f"⏬ Скачано: {user_stat['downloaded']}\n"
            f"⏫ Отправлено: {user_stat['uploaded']}\n"
            f"👁 Последний вход: {user_stat['last_seen']}",
            parse_mode="Markdown"
        )


    @bot.callback_query_handler(func=lambda c: c.data.startswith("restart_confirm|"))
    @admin_only_callback
    @rate_limit(30)
    def cb_restart(call):
        side = call.data.split("|")[1]
        bot.edit_message_text(f"🔄 Перезапускаю {side}...", call.message.chat.id, call.message.message_id)
        if side == "SE":
            subprocess.run(["docker", "restart", "amnezia-awg", "amnezia-awg2"],
                           capture_output=True, timeout=30)
            bot.edit_message_text("✅ SE перезапущен!", call.message.chat.id, call.message.message_id)
            log_audit("RESTART_SE", admin_id=call.from_user.id)
        elif side == "RF":
            rf = next((c for c in cfg.vpn_containers if "RF" in c["alias"]), None)
            if rf:
                try:
                    subprocess.run([
                        "ssh", "-p", str(rf["port"]),
                        "-o", "StrictHostKeyChecking=yes",
                        "-o", f"UserKnownHostsFile=/home/vpnuser/.ssh/known_hosts",
                        rf["host"], "sudo", "docker", "restart", "amnezia-awg2"
                    ], capture_output=True, timeout=25)
                    bot.edit_message_text("✅ RF перезапущен!", call.message.chat.id, call.message.message_id)
                    log_audit("RESTART_RF", admin_id=call.from_user.id)
                except Exception as e:
                    logger.error(f"Рестарт RF: {e}")
                    bot.edit_message_text("❌ Ошибка рестарта RF.", call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ip|"))
    @admin_only_callback
    def cb_ip(call):
        _, ip, server_alias = call.data.split("|")
        if not validate_vpn_ip(ip):
            return bot.answer_callback_query(call.id, "⛔ Невалидный IP!", show_alert=True)
        user = get_user_by_ip(ip)
        db_alias   = user["alias"]       if user else "Неизвестно"
        curr_speed = user["speed_limit"] if user else "max"
        tg_id      = user["tg_user_id"]  if user else None

        tg_status = f"🔗 TG: `{tg_id}`" if tg_id else "❌ TG: не привязан"
        bind_label = "✅ Привязан" if tg_id else "🔗 Привязать TG"

        # Дата оплаты
        paid_until = user["paid_until"] if user else None
        if paid_until:
            try:
                from datetime import datetime as _dt
                exp = _dt.fromisoformat(paid_until)
                days_left = (exp - _dt.now()).days
                paid_str = f"📅 Оплачено до: `{exp.strftime('%d.%m.%Y')}` (дней: {days_left})"
            except Exception:
                paid_str = "📅 Оплата: ошибка"
        else:
            paid_str = "📅 Оплата: не задана"

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🐌 10",  callback_data=f"set|{ip}|{server_alias}|10"),
            InlineKeyboardButton("🚶 30",  callback_data=f"set|{ip}|{server_alias}|30"),
            InlineKeyboardButton("🏃 50",  callback_data=f"set|{ip}|{server_alias}|50"),
            InlineKeyboardButton("🚀 100", callback_data=f"set|{ip}|{server_alias}|100"),
        )
        markup.add(InlineKeyboardButton("💀 Карцер (1 Кбит/с)",  callback_data=f"set|{ip}|{server_alias}|punish"))
        markup.add(InlineKeyboardButton("🔥 Снять лимит (Max)",  callback_data=f"set|{ip}|{server_alias}|max"))
        markup.add(InlineKeyboardButton("✏️ Переименовать",       callback_data=f"rename|{ip}"))
        if tg_id:
            markup.add(
                InlineKeyboardButton(bind_label,        callback_data=f"bind_pick|{ip}"),
                InlineKeyboardButton("🔓 Отвязать TG", callback_data=f"unbind|{ip}"),
            )
        else:
            markup.add(InlineKeyboardButton(bind_label, callback_data=f"bind_pick|{ip}"))
        markup.add(InlineKeyboardButton("📅 Продлить подписку",     callback_data=f"extend|{ip}"))
        markup.add(InlineKeyboardButton("❌ Удалить профиль",       callback_data=f"del|{ip}|{server_alias}"))
        markup.add(InlineKeyboardButton("⬅️ К списку",              callback_data="page|0"))
        bot.edit_message_text(
            f"Управление: *{db_alias}* (`{ip}`)"
            f"\nСервер: {server_alias} | Скорость: {curr_speed}"
            f"\n{tg_status}"
            f"\n{paid_str}",
            call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode="Markdown"
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("extend|"))
    @admin_only_callback
    def cb_extend(call):
        """Shows duration options for extending paid_until."""
        ip = call.data.split("|")[1]
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("+1 месяц",   callback_data=f"extend_confirm|{ip}|30"),
            InlineKeyboardButton("+3 месяца",  callback_data=f"extend_confirm|{ip}|90"),
            InlineKeyboardButton("+6 месяцев", callback_data=f"extend_confirm|{ip}|180"),
            InlineKeyboardButton("+1 год",     callback_data=f"extend_confirm|{ip}|365"),
        )
        markup.add(
            InlineKeyboardButton("🗓 До 28-го числа", callback_data=f"extend_confirm|{ip}|set28")
        )
        markup.add(
            InlineKeyboardButton("♾️ Бессрочно", callback_data=f"extend_confirm|{ip}|lifetime"),
            InlineKeyboardButton("🗑 Сбросить", callback_data=f"extend_confirm|{ip}|reset")
        )
        markup.add(InlineKeyboardButton("❌ Отмена", callback_data="page|0"))
        bot.edit_message_text(
            f"📅 Сколько продлить IP `{ip}`?\n"
            f"_(Дни прибавляются к текущей дате. Либо выберите точную установку)_",
            call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode="Markdown"
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("extend_confirm|"))
    @admin_only_callback
    def cb_extend_confirm(call):
        _, ip, days_str = call.data.split("|")

        if days_str == "set28":
            from datetime import datetime as _dt
            now = _dt.now()
            # Устанавливаем 28-е число текущего месяца
            target_date = now.replace(day=28, hour=0, minute=0, second=0, microsecond=0)
            if target_date < now:
                # Если уже прошло 28 число, ставим 28 следующего месяца
                import calendar
                days_in_month = calendar.monthrange(now.year, now.month)[1]
                target_date = target_date + __import__("datetime").timedelta(days=days_in_month)
                target_date = target_date.replace(day=28)
                
            iso = target_date.strftime("%Y-%m-%dT%H:%M:%S")
            update_user_field(ip, "paid_until", iso)
            log_audit("EXTEND", target_ip=ip, details=f"set28 -> {iso}", admin_id=call.from_user.id)
            bot.edit_message_text(
                f"🗓 Дата оплаты установлена на **28 число** ({target_date.strftime('%d.%m.%Y')})!\nIP: `{ip}`",
                call.message.chat.id, call.message.message_id, parse_mode="Markdown"
            )
            return

        if days_str == "reset":
            update_user_field(ip, "paid_until", None)
            log_audit("EXTEND", target_ip=ip, details="reset", admin_id=call.from_user.id)
            bot.edit_message_text(
                f"🗑 Дата оплаты сброшена (отменена)!\nIP: `{ip}`",
                call.message.chat.id, call.message.message_id, parse_mode="Markdown"
            )
            return

        if days_str == "lifetime":
            from datetime import datetime as _dt
            # Бессрочно = дата в далёком будущем
            update_user_field(ip, "paid_until", "2099-12-31")
            log_audit("EXTEND", target_ip=ip, details="lifetime", admin_id=call.from_user.id)
            user = get_user_by_ip(ip)
            if user and user["tg_user_id"]:
                try:
                    bot.send_message(
                        user["tg_user_id"],
                        "♾️ *Бессрочный доступ!*\nВаш VPN активирован бессрочно.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            bot.edit_message_text(
                f"✅ Бессрочный доступ установлен!\nIP: `{ip}`",
                call.message.chat.id, call.message.message_id, parse_mode="Markdown"
            )
            return

        days = int(days_str)
        new_date = extend_paid_until(ip, days)
        from datetime import datetime as _dt
        exp = _dt.fromisoformat(new_date)
        log_audit("EXTEND", target_ip=ip, details=f"days={days}", admin_id=call.from_user.id)

        # Уведомляем клиента, если привязан
        user = get_user_by_ip(ip)
        if user and user["tg_user_id"]:
            try:
                bot.send_message(
                    user["tg_user_id"],
                    f"🎉 *Оплата подтверждена!*\n\n"
                    f"VPN продлён до *{exp.strftime('%d.%m.%Y')}*.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        bot.edit_message_text(
            f"✅ Подписка продлена!\n"
            f"IP: `{ip}`\n+{days} дней\n"
            f"📅 До: *{exp.strftime('%d.%m.%Y')}*",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown"
        )

    # ── Отвязать TG от профиля ────────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("unbind|"))
    @admin_only_callback
    def cb_unbind(call):
        ip   = call.data.split("|")[1]
        user = get_user_by_ip(ip)
        if not user or not user["tg_user_id"]:
            bot.answer_callback_query(call.id, "⚠️ TG и так не привязан.", show_alert=True)
            return
        tg_id = user["tg_user_id"]
        update_user_field(ip, "tg_user_id", None)
        log_audit("UNBIND", target_ip=ip, details=f"tg_id={tg_id}", admin_id=call.from_user.id)
        bot.answer_callback_query(call.id, f"✅ TG ID {tg_id} отвязан!", show_alert=True)
        bot.edit_message_text(
            f"🔓 *TG отвязан*\n\nIP: `{ip}`\nTG ID был: `{tg_id}`\n\n"
            f"Теперь клиент может быть привязан к другому аккаунту.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bind_pick|"))
    @admin_only_callback
    @rate_limit(5)
    def cb_bind_pick(call):
        """Отправляет нативную Telegram-кнопку выбора контакта."""
        ip = call.data.split("|")[1]
        _bind_pending[call.from_user.id] = ip
        bot.answer_callback_query(call.id)

        # Reply-keyboard с нативным picker-ом контактов
        markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(KeyboardButton(
            "👤 Выбрать пользователя",
            request_user=KeyboardButtonRequestUser(request_id=1)
        ))
        markup.add(KeyboardButton("❌ Отмена"))

        bot.send_message(
            call.message.chat.id,
            f"🔗 Выберите пользователя для IP `{ip}` из списка контактов:",
            reply_markup=markup,
            parse_mode="Markdown"
        )

    @bot.message_handler(content_types=["users_shared"])
    @admin_only
    def handle_user_shared(message):
        """Получает TG ID выбранного контакта (Bot API 7.0+: users_shared)."""
        ip = _bind_pending.pop(message.from_user.id, None)
        if not ip:
            return

        try:
            # Bot API 7.0+: message.users_shared.users — список
            tg_id = message.users_shared.users[0].user_id
        except Exception as e:
            logger.error(f"Ошибка чтения users_shared: {e}")
            bot.send_message(message.chat.id, "❌ Не удалось получить TG ID.",
                             reply_markup=ReplyKeyboardRemove())
            return
        bind_tg_to_ip(ip, tg_id)
        log_audit("BIND", target_ip=ip, details=f"tg_id={tg_id}", admin_id=message.from_user.id)

        # Убираем reply-keyboard
        bot.send_message(
            message.chat.id,
            f"✅ *Привязано!*\nIP `{ip}` → TG ID `{tg_id}`",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        # Уведомления клиенту отключены по просьбе администратора (тихая привязка)
        # try:
        #     bot.send_message(
        #         tg_id,
        #         "🎉 *Ваш VPN-профиль привязан!*\n"
        #         "Напишите /start чтобы увидеть статус подписки.",
        #         parse_mode="Markdown"
        #     )
        # except Exception as e:
        #     logger.warning(f"Не удалось уведомить клиента {tg_id}: {e}")

    @bot.message_handler(func=lambda m: m.text == "❌ Отмена" and m.from_user.id == cfg.admin_id)
    def handle_bind_cancel(message):
        _bind_pending.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, "❌ Привязка отменена.",
                         reply_markup=ReplyKeyboardRemove())

    @bot.callback_query_handler(func=lambda c: c.data.startswith("set|"))
    @admin_only_callback
    def cb_set_speed(call):
        _, ip, server_alias, speed = call.data.split("|")
        if not validate_vpn_ip(ip):
            return bot.answer_callback_query(call.id, "⛔ Невалидный IP!", show_alert=True)
        if speed not in cfg.allowed_speeds:
            return bot.answer_callback_query(call.id, "⛔ Невалидная скорость!", show_alert=True)
        apply_limit(ip, speed, server_alias)
        update_user_field(ip, "speed_limit", speed)
        label = {"max": "Максимум", "punish": "💀 Карцер"}.get(speed, f"{speed} Мбит/с")
        bot.edit_message_text(f"✅ `{ip}` → *{label}*", call.message.chat.id, call.message.message_id,
                              parse_mode="Markdown")
        log_audit("SET_SPEED", ip, f"speed={speed}, server={server_alias}", call.from_user.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("rename|"))
    @admin_only_callback
    def cb_rename(call):
        ip = call.data.split("|")[1]
        if not validate_vpn_ip(ip):
            return bot.answer_callback_query(call.id, "⛔ Невалидный IP!", show_alert=True)
        msg = bot.send_message(call.message.chat.id, f"✏️ Новое имя для `{ip}`:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, _do_rename, ip)

    def _do_rename(message, ip):
        if message.from_user.id != cfg.admin_id:
            return
        clean = sanitize_alias(message.text)
        update_user_field(ip, "alias", clean)
        bot.reply_to(message, f"✅ Имя сохранено: *{clean}*", parse_mode="Markdown")
        log_audit("RENAME", ip, f"alias={clean}", message.from_user.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("del|"))
    @admin_only_callback
    def cb_del_confirm(call):
        _, ip, server_alias = call.data.split("|")
        if not validate_vpn_ip(ip):
            return bot.answer_callback_query(call.id, "⛔ Невалидный IP!", show_alert=True)
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("⚠️ ДА, удалить!", callback_data=f"del_confirm|{ip}|{server_alias}"),
            InlineKeyboardButton("Отмена", callback_data="page|0")
        )
        bot.edit_message_text(f"⚠️ *Удалить* `{ip}` с {server_alias}? Необратимо!",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=markup, parse_mode="Markdown")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("del_confirm|"))
    @admin_only_callback
    def cb_del_execute(call):
        _, ip, server_alias = call.data.split("|")
        if not validate_vpn_ip(ip):
            return bot.answer_callback_query(call.id, "⛔ Невалидный IP!", show_alert=True)
        delete_peer(ip, server_alias)
        delete_user(ip)
        bot.edit_message_text(f"🗑 `{ip}` удалён.", call.message.chat.id, call.message.message_id,
                              parse_mode="Markdown")
        log_audit("DELETE_PEER", ip, f"server={server_alias}", call.from_user.id)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _split(text: str, size: int = 4000):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size:
            chunks.append(cur.strip())
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur.strip())
    return chunks


def _send_users_page(bot, chat_id, page, message_id=None, flt="all"):
    all_users = get_vpn_stats()
    from datetime import datetime as _dt2
    from database import get_all_users as _gu
    _db = _gu()
    now2 = _dt2.now()
    if flt == "expired":
        def _chk(u):
            d = _db.get(u["ip"], {}).get("paid_until")
            if not d: return False
            try:
                e = _dt2.fromisoformat(d)
                return e.year < 2099 and (e - now2).days < 0
            except: return False
        users = [u for u in all_users if _chk(u)]
    elif flt == "expiring":
        def _chk(u):
            d = _db.get(u["ip"], {}).get("paid_until")
            if not d: return False
            try:
                e = _dt2.fromisoformat(d)
                dl = (e - now2).days
                return e.year < 2099 and 0 <= dl <= 7
            except: return False
        users = [u for u in all_users if _chk(u)]
    elif flt == "unbound":
        users = [u for u in all_users if not _db.get(u["ip"], {}).get("tg_user_id")]
    else:
        users = all_users
    if not users:
        txt = "❌ Нет активных пользователей."
        if message_id:
            bot.edit_message_text(txt, chat_id, message_id)
        else:
            bot.send_message(chat_id, txt)
        return

    per = cfg.users_per_page
    total = (len(users) + per - 1) // per
    page = max(0, min(page, total - 1))

    markup = InlineKeyboardMarkup(row_width=1)
    for u in users[page * per:(page + 1) * per]:
        sp = "Макс" if u["speed_limit"] == "max" else f"{u['speed_limit']} Мб/с"
        flag = "🇸🇪" if "SE" in u["server_alias"] else "🇷🇺"
        markup.add(InlineKeyboardButton(
            f"{u['online_emoji']} {flag} {u['db_alias']} [{sp}]",
            callback_data=f"ip|{u['ip']}|{u['server_alias']}"
        ))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"page|{page-1}"))
    nav.append(InlineKeyboardButton("🔄", callback_data=f"page|{page}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"page|{page+1}"))
    markup.row(*nav)

    txt = f"🎛 *Абоненты (стр. {page+1}/{total})*\n🟢 онлайн  🟡 недавно  🔴 оффлайн"
    try:
        if message_id:
            bot.edit_message_text(txt, chat_id, message_id, reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, txt, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        pass


def _daily_analysis():
    if not os.path.isfile(cfg.metrics_file):
        return "❌ Нет данных."
    try:
        with open(cfg.metrics_file, encoding="utf-8") as f:
            data = list(csv.reader(f))[1:][-288:]
        if not data:
            return "❌ Мало данных."
        peak = max(data, key=lambda x: float(x[1]))
        return f"📈 *Анализ SE за 24ч:*\n🔥 Пик: {peak[0]}\n🖥 CPU: {peak[1]}%\n💾 RAM: {peak[2]}%"
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
        return "❌ Ошибка чтения метрик."


def _generate_chart():
    if not os.path.isfile(cfg.metrics_file):
        return None
    try:
        times, cpus, rams = [], [], []
        with open(cfg.metrics_file, encoding="utf-8") as f:
            for row in list(csv.reader(f))[1:][-288:]:
                fmt = "%Y-%m-%d %H:%M:%S" if len(row[0]) > 16 else "%Y-%m-%d %H:%M"
                times.append(datetime.strptime(row[0], fmt))
                cpus.append(float(row[1]))
                rams.append(float(row[2]))
        plt.figure(figsize=(10, 5))
        plt.plot(times, cpus, label="CPU (%)", color="green", marker="o", markersize=2)
        plt.plot(times, rams, label="RAM (%)", color="blue", marker="s", markersize=2)
        plt.title("Нагрузка SE (24ч)", fontsize=14)
        plt.legend(); plt.grid(True, linestyle="--", alpha=0.7)
        plt.gca().xaxis.set_major_locator(mdates.HourLocator(interval=2))
        plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        plt.gcf().autofmt_xdate(); plt.tight_layout()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        plt.savefig(tmp.name); plt.close()
        return tmp.name
    except Exception as e:
        logger.error(f"Ошибка графика: {e}")
        return None
