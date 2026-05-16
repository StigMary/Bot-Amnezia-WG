"""
handlers/client.py — Клиентское меню (v4.0 — «Единый баланс»).

Архитектура:
 - Подписка привязана к tg_user_id, а не к IP.
 - Несколько устройств одного аккаунта имеют ОДНУ дату paid_until.
 - При продлении обновляются ВСЕ устройства аккаунта синхронно.
 - IP-адреса клиенту не показываются — только «Конфигурация #X».
"""
from datetime import datetime

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

from config import cfg, logger
from database import (
    get_user_by_tg_id, get_devices_by_tg_id, get_all_users,
    extend_paid_until_for_tg, update_user_field, log_audit, bind_tg_to_ip,
)
from vpn_engine import apply_limit
from handlers.decorators import group_member_only, admin_only_callback, rate_limit


# ─── Reply-клавиатура клиента (2 кнопки внизу экрана) ─────────────────────────

_CLIENT_KEYBOARD = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
_CLIENT_KEYBOARD.add(
    KeyboardButton("👤 Профиль")
)

def _device_name_display(ip: str, client_alias: str) -> str:
    """Формирует красивое имя устройства."""
    if not client_alias:
        return f"Конфигурация #{ip.split('.')[-1]}"
    return f"{client_alias} (#{ip.split('.')[-1]})"

def _account_status_text(tg_user_id: int, username: str) -> str:
    """Формирует текст дашборда (версия с Устройствами)."""
    devices = get_devices_by_tg_id(tg_user_id)
    if not devices:
        return "⚠️ Профиль не найден. Обратитесь к администратору."

    paid_until   = devices[0]["paid_until"]
    any_punished = any(d["speed_limit"] == "punish" for d in devices)

    if paid_until:
        try:
            exp = datetime.fromisoformat(paid_until)
            if exp.year >= 2099:
                status_net = "🟢 Активен (Бессрочный)"
                date_line  = ""
            else:
                days_left = (exp - datetime.now()).days
                exp_str   = exp.strftime("%d.%m.%Y")
                if days_left > 0:
                    status_net = "🔴 Ограничен" if any_punished else "🟢 Активен"
                    date_line  = f"📅 Оплачено до: {exp_str} (Осталось: {days_left} дн.)"
                else:
                    status_net = "🔴 Просрочен"
                    date_line  = f"📅 Истекло: {exp_str} (Просрочено {abs(days_left)} дн. назад)"
        except ValueError:
            status_net = "⚠️ Ошибка"
            date_line  = ""
    else:
        status_net = "❓ Не задана"
        date_line  = ""

    dev_lines = "\n".join(
        f"🔹 {_device_name_display(d['ip_address'], d['client_alias'])}"
        for d in devices
    )

    lines = [
        "🛡 <b>Личный кабинет KJZNNETx</b>",
        "━" * 20,
        f"👤 Аккаунт: {username}",
        f"🟢 Статус сети: {status_net}",
    ]
    if date_line:
        lines.append(date_line)
    lines += [
        "",
        "📱 <b>Ваши устройства:</b>",
        dev_lines,
        "━" * 20,
        "👇 Выберите действие в меню ниже:",
    ]
    return "\n".join(lines)

def _get_inline_menu():
    """Новая сетка кнопок."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("💳 Оплатить / Продлить", callback_data="client_pay"))
    markup.add(
        InlineKeyboardButton("➕ Запросить устр-во", callback_data="client_req_device"),
        InlineKeyboardButton("✏️ Переименовать", callback_data="client_rename_list")
    )
    markup.add(
        InlineKeyboardButton("🛠 Инструкция", callback_data="client_howto"),
        InlineKeyboardButton("❓ Поддержка", callback_data="client_support")
    )
    return markup

# ─── Пул ожидающих привязки клиентов {tg_id: {name, username}} ──────────────
# Заполняется когда незарегистрированный пользователь пишет /start.
# Администратор видит этот список при привязке IP → TG ID.
_pending_pool: dict = {}

# ─── Активные тикеты поддержки {tg_id: True} ────────────────────────────────
_active_tickets: dict = {}


def _device_name(ip: str) -> str:
    """Короткое имя устройства по IP (для сообщений привязки)."""
    return f"Конфигурация #{ip.split('.')[-1]}" if ip else "Устройство"

# ─── Регистрация хэндлеров ────────────────────────────────────────────────────

def register(bot: telebot.TeleBot):

    _gmo = group_member_only(bot)

    # ── /start — Дашборд ──────────────────────────────────────────────────────
    @bot.message_handler(commands=["start"])
    @_gmo
    def cmd_start_client(message):
        # Если пишут в группе — удаляем сообщение (чтобы не мусорили) и игнорируем
        if message.chat.type != "private":
            try:
                from config import logger
                logger.warning(f"NEW GROUP ID DETECTED: {message.chat.id}")
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            return

        if message.from_user.id == cfg.admin_id:
            return  # admin.py обрабатывает первым

        uid = message.from_user.id
        username = f"@{message.from_user.username}" if message.from_user.username \
                   else message.from_user.first_name or "Клиент"
        user = get_user_by_tg_id(uid)

        if not user:
            _pending_pool[uid] = {
                "name":     f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip(),
                "username": f"@{message.from_user.username}" if message.from_user.username else f"ID:{uid}",
            }
            bot.send_message(
                message.chat.id,
                "👋 <b>Привет!</b>\n\n"
                "Ваш профиль ещё не привязан.\n"
                "Обратитесь к администратору — как только он настроит доступ, "
                "напишите /start снова.",
                parse_mode="HTML",
                reply_markup=_CLIENT_KEYBOARD,
            )
            return

        text = _account_status_text(uid, username)
        
        # Отправляем системное сообщение для обновления Reply-клавиатуры
        bot.send_message(
            message.chat.id,
            "👋 Добро пожаловать! Главное меню обновлено.",
            reply_markup=_CLIENT_KEYBOARD,
        )
        # Сразу показываем дашборд с inline-кнопками
        bot.send_message(
            message.chat.id,
            text,
            reply_markup=_get_inline_menu(),
            parse_mode="HTML",
        )

    # ── Кнопка «👤 Профиль» ──────────────────────────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "👤 Профиль")
    @_gmo
    @rate_limit(3)
    def btn_profile(message):
        if message.chat.type != "private":
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            return

        uid = message.from_user.id
        username = f"@{message.from_user.username}" if message.from_user.username \
                   else message.from_user.first_name or "Клиент"
        user = get_user_by_tg_id(uid)
        
        if not user:
            bot.send_message(message.chat.id, "⛔ Профиль не найден. Напишите /start.")
            return

        text = _account_status_text(uid, username)
        bot.send_message(
            message.chat.id,
            text,
            reply_markup=_get_inline_menu(),
            parse_mode="HTML",
        )

    # ── Кнопка «💳 Оплатить / Продлить» (inline) ─────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "client_pay")
    def cb_client_pay(call):
        bot.answer_callback_query(call.id)
        uid = call.from_user.id
        if not get_user_by_tg_id(uid):
            bot.send_message(call.message.chat.id, "⛔ Профиль не найден. Напишите /start.")
            return


        msg = bot.send_message(
            call.message.chat.id,
            "💳 <b>Оплата подписки</b>\n\n"
            f"🏦 Банк: Т-Банк\n"
            f"📱 Номер: <code>{cfg.payment_details}</code>\n\n"
            f"💰 Стоимость: <code>{cfg.payment_amount}</code>\n\n"
            "После оплаты пришлите <b>фото чека</b> прямо в этот чат.\n"
            "<i>Текстовые сообщения не принимаются — только фото или скриншот.</i>\n"
            "<i>(Или нажмите «👤 Профиль» для отмены)</i>",
            parse_mode="HTML",
        )
        bot.register_next_step_handler(msg, _receive_receipt, uid)

    # ── Кнопка «🛠 Инструкция по подключению» (inline) ──────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "client_howto")
    def cb_client_howto(call):
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🛠 <b>Инструкция по подключению:</b>\n\n"
            "1️⃣ Установите приложение AmneziaVPN\n"
            "   🍏 iOS: <a href='https://apps.apple.com/us/app/amneziavpn/id1600529900'>AmneziaVPN</a> для всех | <a href='https://apps.apple.com/ru/app/defaultvpn/id6744725017'>Default VPN</a> для РУ\n"
            "   🤖 Android: <a href='https://play.google.com/store/apps/details?id=org.amnezia.vpn'>Google Play</a> для всех | <a href='https://github.com/amnezia-vpn/amnezia-client/releases/tag/4.8.15.4'>GitHub (APK)</a>\n"
            "   💻 ПК: <a href='https://amnezia.org/'>Windows / macOS</a>\n\n"
            "2️⃣ Откройте приложение → «➕» → «Импорт из файла или QR»\n\n"
            "3️⃣ Используйте конфиг, выданный администратором\n\n"
            "4️⃣ Нажмите «Подключить» — готово!\n\n"
            "❓ Если возникли вопросы — обратитесь к администратору.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


    # ── Приём фото чека ───────────────────────────────────────────────────────
    def _receive_receipt(message, tg_user_id: int):
        # Если клиент передумал и нажал "Профиль" или /start
        if message.text in ["👤 Профиль", "/start"]:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            btn_profile(message)
            return

        # Защита: клиент прислал текст вместо фото
        if not message.photo and not message.document:
            msg = bot.reply_to(
                message,
                "⚠️ Пожалуйста, пришлите именно *фото* или скриншот чека (не текст).\n"
                "<i>(Или нажмите «👤 Профиль» для отмены)</i>",
                parse_mode="HTML",
            )
            bot.register_next_step_handler(msg, _receive_receipt, tg_user_id)
            return

        uid   = message.from_user.id
        name  = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
        uname = f"@{message.from_user.username}" if message.from_user.username else f"ID:{uid}"

        # Собираем сводку по аккаунту для администратора
        devices  = get_devices_by_tg_id(tg_user_id)
        dev_count = len(devices)
        paid_until = devices[0]["paid_until"] if devices else "—"
        try:
            exp_str = datetime.fromisoformat(paid_until).strftime("%d.%m.%Y") if paid_until else "—"
        except Exception:
            exp_str = paid_until or "—"

        caption = (
            f"🧾 <b>Новый чек на оплату</b>\n\n"
            f"👤 {name} ({uname})\n"
            f"🆔 TG ID: <code>{tg_user_id}</code>\n"
            f"📱 Устройств: <b>{dev_count}</b>\n"
            f"📅 Текущий срок: <code>{exp_str}</code>"
        )

        # Кнопки для администратора
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton(
                "✅ Подтвердить +1 месяц",
                callback_data=f"pay_ok|{tg_user_id}|30"
            ),
            InlineKeyboardButton(
                "✅ Подтвердить +3 месяца",
                callback_data=f"pay_ok|{tg_user_id}|90"
            ),
            InlineKeyboardButton(
                "❌ Отклонить",
                callback_data=f"pay_no|{tg_user_id}"
            ),
        )

        try:
            if message.photo:
                bot.send_photo(cfg.admin_id, message.photo[-1].file_id,
                               caption=caption, reply_markup=markup, parse_mode="HTML")
            else:
                bot.send_document(cfg.admin_id, message.document.file_id,
                                  caption=caption, reply_markup=markup, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка пересылки чека: {e}")

        bot.reply_to(
            message,
            "✅ <b>Чек успешно отправлен!</b>\n\n"
            "Администратор проверит его в ближайшее время.\n"
            "Обычно это занимает не более нескольких часов.",
            parse_mode="HTML",
        )
        log_audit("CHECK_SENT", details=f"tg_id={tg_user_id}, devices={dev_count}")

    # ── Администратор: подтвердить оплату ────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("pay_ok|"))
    @admin_only_callback
    def cb_pay_ok(call):
        parts = call.data.split("|")
        tg_user_id = int(parts[1])
        days       = int(parts[2])

        # Продлеваем ВСЕМ устройствам аккаунта
        new_date_iso = extend_paid_until_for_tg(tg_user_id, days)
        exp = datetime.fromisoformat(new_date_iso)

        # Снимаем карцер с каждого устройства
        devices = get_devices_by_tg_id(tg_user_id)
        released = 0
        for d in devices:
            if d["speed_limit"] == "punish" and d["protocol"]:
                try:
                    apply_limit(d["ip_address"], "max", d["protocol"])
                    update_user_field(d["ip_address"], "speed_limit", "max")
                    released += 1
                except Exception as e:
                    logger.warning(f"Ошибка снятия карцера {d['ip_address']}: {e}")

        log_audit("PAY_CONFIRM", details=f"tg_id={tg_user_id}, days={days}, devices={len(devices)}, released={released}",
                  admin_id=call.from_user.id)

        # Удаляем кнопки у сообщения (чтобы не нажали дважды)
        bot.edit_message_caption(
            f"✅ *Оплата подтверждена*\n\n"
            f"🆔 TG ID: `{tg_user_id}`\n"
            f"📱 Устройств обновлено: *{len(devices)}*\n"
            f"📅 Подписка до: `{exp.strftime('%d.%m.%Y')}`"
            + (f"\n⚡ Карцер снят: {released} уст." if released else ""),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=None,  # убираем кнопки
        )

        # Пуш клиенту
        try:
            bot.send_message(
                tg_user_id,
                f"🎉 *Оплата подтверждена!*\n\n"
                f"Подписка продлена до *{exp.strftime('%d.%m.%Y')}*.\n"
                f"Все ваши конфигурации активны. Приятного пользования!",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить клиента tg_id={tg_user_id}: {e}")

    # ── Администратор: отклонить оплату ───────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("pay_no|"))
    @admin_only_callback
    def cb_pay_no(call):
        tg_user_id = int(call.data.split("|")[1])

        log_audit("PAY_REJECT", details=f"tg_id={tg_user_id}", admin_id=call.from_user.id)

        # Убираем кнопки
        bot.edit_message_caption(
            f"❌ *Оплата отклонена*\n🆔 TG ID: `{tg_user_id}`",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=None,
        )

        try:
            bot.send_message(
                tg_user_id,
                "❌ К сожалению, ваш чек не был принят.\n\n"
                "Возможно, сумма или реквизиты не совпадают.\n"
                "Свяжитесь с администратором или попробуйте снова через /start.",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить клиента tg_id={tg_user_id}: {e}")

    # ── Привязка (сторона администратора) ────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("bind_pick|"))
    @admin_only_callback
    def cb_bind_pick(call):
        ip = call.data.split("|")[1]
        if not _pending_pool:
            bot.answer_callback_query(
                call.id,
                "👥 Никто ещё не писал /start боту.\n"
                "Попросите клиента написать /start — он появится здесь.",
                show_alert=True,
            )
            return

        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup(row_width=1)
        for tg_id, info in sorted(_pending_pool.items(), key=lambda x: x[1]["name"]):
            label = f"{info['name'].strip() or '—'}  {info['username']}"
            markup.add(InlineKeyboardButton(
                f"👤 {label}",
                callback_data=f"bind_confirm|{ip}|{tg_id}",
            ))
        markup.add(InlineKeyboardButton("❌ Отмена", callback_data="bind_pick_cancel"))

        bot.edit_message_text(
            f"🔗 Выберите клиента для `{_device_name(ip)}`:\n"
            f"_(список тех, кто написал /start боту)_",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown",
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bind_confirm|"))
    @admin_only_callback
    def cb_bind_confirm(call):
        _, ip, tg_id_str = call.data.split("|")
        tg_id = int(tg_id_str)

        bind_tg_to_ip(ip, tg_id)
        _pending_pool.pop(tg_id, None)
        log_audit("BIND", target_ip=ip, details=f"tg_id={tg_id}", admin_id=call.from_user.id)

        bot.edit_message_text(
            f"✅ *Привязано!*\n{_device_name(ip)} → TG ID `{tg_id}`",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
        )
        try:
            bot.send_message(
                tg_id,
                "🎉 *Готово!* Ваш VPN-профиль привязан.\n"
                "Напишите /start чтобы увидеть статус подписки.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить клиента tg_id={tg_id}: {e}")

    @bot.callback_query_handler(func=lambda c: c.data == "bind_pick_cancel")
    @admin_only_callback
    def cb_bind_pick_cancel(call):
        bot.edit_message_text("❌ Привязка отменена.", call.message.chat.id, call.message.message_id)

    # ── Запрос нового устройства ─────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "client_req_device")
    def cb_req_device(call):
        uid = call.from_user.id
        devices = get_devices_by_tg_id(uid)
        
        if len(devices) >= 2:
            bot.answer_callback_query(call.id, "❌ У вас уже есть максимально доступные 2 конфигурации!", show_alert=True)
            return

        bot.answer_callback_query(call.id, "Заявка отправлена!")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💬 Ответить (Отправить конфиг)", callback_data=f"admin_reply_ticket|{call.from_user.id}"))

        bot.send_message(
            cfg.admin_id,
            f"📩 <b>Новая заявка на устройство!</b>\n\n"
            f"👤 Клиент: @{call.from_user.username or call.from_user.first_name}\n"
            f"🆔 TG ID: <code>{call.from_user.id}</code>\n\n"
            f"<i>(Сгенерируйте конфиг и отправьте клиенту вручную)</i>",
            parse_mode="HTML",
            reply_markup=markup
        )
        bot.send_message(
            call.message.chat.id,
            "✅ <b>Заявка принята!</b>\n\n"
            "⚠️ <i>Обратите внимание: на один аккаунт допускается максимум <b>две конфигурации (два устройства)</b>.</i>\n\n"
            "Администратор подготовит файл конфигурации для нового устройства и пришлет его вам в этот чат.",
            parse_mode="HTML"
        )

    # ── Переименование устройства ─────────────────────────────────────────────
    from vpn_engine import sanitize_alias

    @bot.callback_query_handler(func=lambda c: c.data == "client_rename_list")
    def cb_rename_list(call):
        uid = call.from_user.id
        devices = get_devices_by_tg_id(uid)
        
        if not devices:
            return bot.answer_callback_query(call.id, "У вас нет активных устройств.", show_alert=True)
            
        if len(devices) == 1:
            # Если устройство одно, сразу просим имя
            ip = devices[0]["ip_address"]
            _ask_new_name(bot, call.message.chat.id, ip)
        else:
            # Если несколько, даем выбрать
            markup = InlineKeyboardMarkup(row_width=1)
            for d in devices:
                name = _device_name_display(d['ip_address'], d['client_alias'])
                markup.add(InlineKeyboardButton(name, callback_data=f"client_rename_do|{d['ip_address']}"))
            bot.edit_message_text(
                "✏️ Выберите устройство для переименования:",
                call.message.chat.id, call.message.message_id, reply_markup=markup
            )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("client_rename_do|"))
    def cb_rename_do(call):
        ip = call.data.split("|")[1]
        bot.answer_callback_query(call.id)
        _ask_new_name(bot, call.message.chat.id, ip)

    def _ask_new_name(bot_instance, chat_id, ip):
        msg = bot_instance.send_message(
            chat_id,
            "✏️ <b>Введите новое название для устройства</b>\n"
            "<i>(Например: Мой iPhone, Рабочий ноут, Роутер)</i>:",
            parse_mode="HTML"
        )
        bot_instance.register_next_step_handler(msg, _process_rename, ip)

    def _process_rename(message, ip):
        if message.text == "👤 Профиль" or not message.text:
            return # Выход, если юзер передумал и нажал кнопку профиля
            
        clean_name = sanitize_alias(message.text)
        update_user_field(ip, "client_alias", clean_name)
        log_audit("CLIENT_RENAME", target_ip=ip, details=f"new_client_alias={clean_name}")

        bot.send_message(message.chat.id, f"✅ Устройство переименовано в <b>{clean_name}</b>!", parse_mode="HTML")
        # Вызываем дашборд заново, чтобы обновить текст
        cmd_start_client(message)


    # ── Поддержка (Helpdesk) — ТИКЕТ-СИСТЕМА ─────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "client_support")
    def cb_support(call):
        uid = call.from_user.id
        bot.answer_callback_query(call.id)

        if uid in _active_tickets:
            # Тикет уже открыт — показываем это
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("✅ Проблема решена", callback_data="client_close_ticket"))
            bot.send_message(
                call.message.chat.id,
                "🎟 <b>У вас уже есть открытый тикет</b>\n\n"
                "Просто пишите свои сообщения прямо сюда — администратор их видит.\n"
                "Если вопрос решён — нажмите кнопку ниже.",
                parse_mode="HTML",
                reply_markup=markup
            )
            return

        # Открываем новый тикет
        _active_tickets[uid] = True
        uname = f"@{call.from_user.username}" if call.from_user.username else call.from_user.first_name

        # Кнопка закрытия тикета для админа
        admin_markup = InlineKeyboardMarkup()
        admin_markup.add(InlineKeyboardButton("❌ Закрыть тикет", callback_data=f"admin_close_ticket|{uid}"))
        admin_markup.add(InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_ticket|{uid}"))

        bot.send_message(
            cfg.admin_id,
            f"🎟️ <b>Новый тикет поддержки открыт!</b>\n\n"
            f"👤 Клиент: {uname}\n"
            f"🆔 TG ID: <code>{uid}</code>\n\n"
            f"<i>Сообщения клиента будут пересылаться сюда автоматически, пока тикет открыт.</i>",
            parse_mode="HTML",
            reply_markup=admin_markup
        )

        # Кнопка закрытия для клиента
        client_markup = InlineKeyboardMarkup()
        client_markup.add(InlineKeyboardButton("✅ Проблема решена", callback_data="client_close_ticket"))

        bot.send_message(
            call.message.chat.id,
            "🎟 <b>Тикет открыт!</b>\n\n"
            "Пишите свои сообщения прямо в этот чат — администратор всё видит.\n"
            "📎 Можно прикрепить фото или скриншот.\n\n"
            "<i>Когда ваша проблема будет решена — нажмите «✅ Проблема решена».</i>",
            parse_mode="HTML",
            reply_markup=client_markup
        )
        log_audit("SUPPORT_TICKET_OPEN", details=f"tg_id={uid}")

    # ── Закрытие тикета клиентом ──────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data == "client_close_ticket")
    def cb_client_close_ticket(call):
        uid = call.from_user.id
        bot.answer_callback_query(call.id)
        _active_tickets.pop(uid, None)

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(
            call.message.chat.id,
            "✅ <b>Тикет закрыт!</b>\n\nРады, что смогли помочь. Если возникнут ещё вопросы — всегда можете обратиться! 😊",
            parse_mode="HTML"
        )
        bot.send_message(
            cfg.admin_id,
            f"✅ <b>Тикет закрыт клиентом</b> (ID: <code>{uid}</code>). Проблема решена.",
            parse_mode="HTML"
        )
        log_audit("SUPPORT_TICKET_CLOSE", details=f"tg_id={uid}, closed_by=client")

    # ── Закрытие тикета админом ───────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("admin_close_ticket|"))
    def cb_admin_close_ticket(call):
        if call.from_user.id != cfg.admin_id:
            return
        bot.answer_callback_query(call.id)
        uid = int(call.data.split("|")[1])
        _active_tickets.pop(uid, None)

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(
            uid,
            "🎟️ <b>Ваш тикет закрыт администратором.</b>\n\n"
            "Надеемся, ваш вопрос решён! Если возникнут ещё вопросы — обращайтесь в поддержку через кнопку «❓ Поддержка». 😊",
            parse_mode="HTML"
        )
        bot.send_message(call.message.chat.id, f"✅ Тикет ID <code>{uid}</code> закрыт.", parse_mode="HTML")
        log_audit("SUPPORT_TICKET_CLOSE", details=f"tg_id={uid}, closed_by=admin")

    # ── Перехват сообщений клиента для открытого тикета ───────────────────────
    @bot.message_handler(func=lambda m: m.chat.type == "private" and m.from_user.id in _active_tickets
                         and m.text not in ["👤 Профиль", "/start"] and m.from_user.id != cfg.admin_id,
                         content_types=["text", "photo", "document"])
    def msg_ticket_relay(message):
        uid = message.from_user.id
        uname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name

        admin_markup = InlineKeyboardMarkup()
        admin_markup.add(InlineKeyboardButton("❌ Закрыть тикет", callback_data=f"admin_close_ticket|{uid}"))
        admin_markup.add(InlineKeyboardButton("💬 Ответить", callback_data=f"admin_reply_ticket|{uid}"))

        header = (
            f"🎟 <b>Тикет | {uname}</b> (<code>{uid}</code>)\n"
        )
        try:
            if message.text:
                bot.send_message(cfg.admin_id, f"{header}\n💬 {message.text}", parse_mode="HTML", reply_markup=admin_markup)
            elif message.photo:
                bot.send_photo(cfg.admin_id, message.photo[-1].file_id,
                               caption=f"{header}\n💬 {message.caption or ''}",
                               parse_mode="HTML", reply_markup=admin_markup)
            elif message.document:
                bot.send_document(cfg.admin_id, message.document.file_id,
                                  caption=f"{header}\n💬 {message.caption or ''}",
                                  parse_mode="HTML", reply_markup=admin_markup)
        except Exception as e:
            logger.error(f"Ошибка пересылки сообщения тикета: {e}")



    @bot.callback_query_handler(func=lambda c: c.data.startswith("admin_reply_ticket|"))
    def cb_admin_reply_ticket(call):
        if call.from_user.id != cfg.admin_id:
            return
        bot.answer_callback_query(call.id)
        tg_id = call.data.split("|")[1]
        msg = bot.send_message(
            call.message.chat.id,
            f"✍️ <b>Введите ответ для пользователя</b> <code>{tg_id}</code>\n"
            "<i>Сообщение будет отправлено ему от лица бота. (Или нажмите \"👤 Профиль\" для отмены)</i>",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, _process_admin_reply, tg_id)

    def _process_admin_reply(message, tg_id):
        if message.text == "👤 Профиль":
            return
        if not message.text and not message.document and not message.photo:
            return
            
        import re
        def extract_ip(text: str):
            match = re.search(r"Address\s*=\s*([0-9\.]+)(?:/[0-9]+)?", text)
            if match:
                return match.group(1)
            return None

        tg_id_int = int(tg_id)
        found_ip = None

        try:
            if message.text:
                found_ip = extract_ip(message.text)
                bot.send_message(
                    tg_id_int,
                    f"📩 <b>Сообщение от администратора:</b>\n\n{message.text}",
                    parse_mode="HTML"
                )
            elif message.document:
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                try:
                    text = downloaded_file.decode("utf-8")
                    found_ip = extract_ip(text)
                except Exception:
                    pass
                bot.send_document(
                    tg_id_int,
                    message.document.file_id,
                    caption=f"📩 <b>Файл от администратора:</b>\n\n{message.caption or ''}",
                    parse_mode="HTML"
                )
            elif message.photo:
                bot.send_photo(
                    tg_id_int,
                    message.photo[-1].file_id,
                    caption=f"📩 <b>Фото от администратора:</b>\n\n{message.caption or ''}",
                    parse_mode="HTML"
                )

            if found_ip:
                bind_tg_to_ip(found_ip, tg_id_int)
                devices = get_devices_by_tg_id(tg_id_int)
                main_dev = next((d for d in devices if d['ip_address'] != found_ip and d['paid_until']), None)
                if main_dev:
                    update_user_field(found_ip, "paid_until", main_dev['paid_until'])
                    alias_base = main_dev['alias'] or "Пользователь"
                    update_user_field(found_ip, "alias", f"{alias_base} (Доп)")
                
                bot.send_message(message.chat.id, f"✅ Ответ отправлен и IP `{found_ip}` автоматически привязан как дополнительное устройство!", parse_mode="Markdown")
                # Уведомляем клиента, чтобы он обновил дашборд
                bot.send_message(tg_id_int, "🎉 Новая конфигурация привязана к вашему профилю!\nНажмите /start чтобы увидеть её.")
            else:
                bot.send_message(message.chat.id, "✅ Ответ успешно отправлен!")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка отправки: {e}")
