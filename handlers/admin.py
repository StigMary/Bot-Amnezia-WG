"""
handlers/admin.py — Административная панель (ЦУП).

UX-улучшения v2:
  1. Кнопка «🔍 Поиск» в главном меню + inline-поиск без команды
  2. Фильтры в списке абонентов (Все / Просрочены / Истекают / Без TG)
  3. Онлайн-статус и last_seen прямо в карточке абонента
  4. Подтверждение перед рестартом SE/RU
  5. Фильтр по серверу в «Все пользователи» (Все / SE / RU)
  6. Текущая дата подписки при выборе продления
  7. Выбор сервера для графика нагрузки (SE — исторический, RU — снимок)
  8. Фильтры журнала аудита (все / скорость / продления / удаления)
  9. Возврат к карточке абонента после смены скорости (_render_user_card)
"""

import contextlib
import os
import subprocess

import matplotlib
import psutil

matplotlib.use("Agg")
from datetime import datetime

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUser,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config import cfg, logger
from database import (
    bind_tg_to_ip,
    delete_user,
    extend_paid_until,
    extend_paid_until_for_tg,
    get_all_users,
    get_audit_log,
    get_devices_by_tg_id,
    get_user_by_ip,
    log_audit,
    update_user_field,
)
from handlers.decorators import admin_only, admin_only_callback, rate_limit
from vpn_engine import (
    apply_limit,
    delete_peer,
    get_ru_metrics,
    get_ru_ping,
    get_vpn_stats,
    sanitize_alias,
    validate_vpn_ip,
)

# {admin_id: ip} — пендинг привязки: какой IP ждёт контакт
_bind_pending: dict = {}


# --- Markdown v1 escape helper (защита от Bad Request: can't parse entities) ---
def _md_escape(text):
    """Экранирует спецсимволы Telegram Markdown v1: _ * ` ["""
    if text is None:
        return ""
    s = str(text)
    for ch in ("\\", "_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


PAGE_SIZE = 10


def _split(text: str, max_len: int = 4000):
    """Splits long text into chunks for Telegram."""
    chunks = []
    while len(text) > max_len:
        split_at = text.rfind('\n', 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:]
    if text:
        chunks.append(text)
    return chunks


def _send_users_page(bot, chat_id, page, edit_msg_id=None, flt='all'):
    """Sends paginated list of users with filter support."""
    from vpn_engine import get_vpn_stats
    from datetime import datetime
    stats = get_vpn_stats()
    now = datetime.now()

    # Apply filter
    if flt == 'expired':
        filtered = []
        for u in stats:
            pd = u.get('paid_until')
            if pd:
                try:
                    exp = datetime.fromisoformat(pd)
                    if exp.year < 2099 and exp < now:
                        filtered.append(u)
                except Exception:
                    pass
    elif flt == 'expiring':
        filtered = []
        for u in stats:
            pd = u.get('paid_until')
            if pd:
                try:
                    exp = datetime.fromisoformat(pd)
                    days_left = (exp - now).days
                    if 0 <= days_left <= 5:
                        filtered.append(u)
                except Exception:
                    pass
    elif flt == 'notg':
        filtered = [u for u in stats if not u.get('tg_user_id')]
    elif flt == 'se':
        filtered = [u for u in stats if 'SE' in u.get('server_alias', '')]
    elif flt == 'ru':
        filtered = [u for u in stats if 'RU' in u.get('server_alias', '')]
    else:
        filtered = stats

    total = len(filtered)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = filtered[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    # Build message
    filter_names = {
        'all': '🌐 Все',
        'expired': '🔴 Просрочены',
        'expiring': '🟡 Истекают',
        'notg': '📵 Без TG',
        'se': '🇸🇪 SE',
        'ru': '🇷🇺 RU',
    }
    lines = [f"👥 *Абоненты* | {filter_names.get(flt, flt)} | Стр. {page+1}/{total_pages} ({total} всего)\n"]
    for u in chunk:
        flag = '🇸🇪' if 'SE' in u.get('server_alias', '') else '🇷🇺'
        alias = _md_escape(u.get('db_alias', u.get('ip', '?')))
        pd = u.get('paid_until', '')
        date_str = ''
        if pd:
            try:
                exp = datetime.fromisoformat(pd)
                if exp.year >= 2099:
                    date_str = ' ♾'
                else:
                    days = (exp - now).days
                    date_str = f' ({days}д)'
            except Exception:
                pass
        lines.append(f"{u['online_emoji']} {flag} {alias}{date_str}")

    text = '\n'.join(lines)

    # Build navigation keyboard
    markup = InlineKeyboardMarkup(row_width=3)
    # Filter buttons
    markup.add(
        InlineKeyboardButton('🌐', callback_data=f'filter|all'),
        InlineKeyboardButton('🔴', callback_data=f'filter|expired'),
        InlineKeyboardButton('🟡', callback_data=f'filter|expiring'),
        InlineKeyboardButton('📵', callback_data=f'filter|notg'),
        InlineKeyboardButton('🇸🇪', callback_data=f'filter|se'),
        InlineKeyboardButton('🇷🇺', callback_data=f'filter|ru'),
    )
    # Page navigation
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('◀️', callback_data=f'page|{page-1}|{flt}'))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton('▶️', callback_data=f'page|{page+1}|{flt}'))
    if nav:
        markup.add(*nav)

    if edit_msg_id:
        try:
            bot.edit_message_text(text, chat_id, edit_msg_id, reply_markup=markup, parse_mode='Markdown')
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')


def register(bot: telebot.TeleBot):

# ── /start — админка ────────────────────────────────────────────────────
    @bot.message_handler(commands=["start"], func=lambda m: m.from_user.id == cfg.admin_id)
    def cmd_start_admin(message):
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.row(KeyboardButton("📡 Серверы"), KeyboardButton("🛠 Сервис"))
        markup.row(KeyboardButton("🔍 Поиск"))

        stats = get_vpn_stats()
        _total2 = len(stats)
        _online2 = sum(1 for s in stats if s.get("online_emoji", "") in ("🟢", "🟡"))

        _cpu_se2 = psutil.cpu_percent(interval=None)
        _ram_se2 = psutil.virtual_memory().percent

        _rf2 = get_ru_metrics()
        if _rf2:
            _ru_status2 = f"🖥 {float(_rf2.get('cpu', 0)):.0f}%  💾 {float(_rf2.get('ram', 0)):.0f}%"
        else:
            _ru_status2 = "❌ недоступен"

        import datetime as _dt2
        _now2 = _dt2.datetime.now().strftime("%d.%m.%Y  %H:%M")
        _dash2 = (
            "👑 *KJZNNETx Admin Panel*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 Онлайн: *{_online2}*  |  👥 Всего: *{_total2}*\n"
            f"🇸🇪 SE: 🖥 {_cpu_se2:.0f}%  💾 {_ram_se2:.0f}%\n"
            f"🇷🇺 RU: {_ru_status2}\n"
            f"🕐 {_now2}"
        )

        import time as _t
        import os as _os
        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp, WebAppInfo

        _wh_url = _os.getenv("WEBHOOK_URL", "https://accomplished-travelers-acids-jaguar.trycloudflare.com").strip().rstrip("/")
        _app_url = f"{_wh_url}?t={int(_t.time())}"

        inline_markup = InlineKeyboardMarkup()
        inline_markup.add(InlineKeyboardButton("🌐 Открыть Web-Панель", web_app=WebAppInfo(url=_app_url)))

        try:
            bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=MenuButtonWebApp(type="web_app", text="Web-Панель", web_app=WebAppInfo(url=_app_url))
            )
        except Exception:
            pass

        bot.reply_to(message, _dash2, reply_markup=markup, parse_mode="Markdown")
        bot.send_message(message.chat.id, "👇 Нажмите кнопку ниже для запуска Mini App:", reply_markup=inline_markup)

    @bot.message_handler(commands=["link"])
    @admin_only
    def cmd_link_configs(message):
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(
                message,
                "ℹ️ <b>Использование:</b> <code>/link &lt;IP_КОНФИГА&gt; &lt;IP_ОСНОВНОГО_АБОНЕНТА&gt;</code>\n\n"
                "<i>Пример:</i> <code>/link 10.8.1.32 10.8.1.5</code> — привяжет конфиг <code>10.8.1.32</code> к аккаунту абонента <code>10.8.1.5</code> (скопирует TG ID и подписку).",
                parse_mode="HTML"
            )
            return
        source_ip, target_ip = parts[1].strip(), parts[2].strip()
        if not validate_vpn_ip(source_ip) or not validate_vpn_ip(target_ip):
            bot.reply_to(message, "⛔ Один из IP невалиден.")
            return
        
        target_user = get_user_by_ip(target_ip)
        if not target_user:
            bot.reply_to(message, f"⛔ Основной абонент с IP <code>{target_ip}</code> не найден в БД.", parse_mode="HTML")
            return
        if not target_user["tg_user_id"]:
            bot.reply_to(message, f"⛔ У абонента <code>{target_ip}</code> ({target_user['alias']}) не привязан TG ID. Сначала привяжите его через <code>/bind</code>.", parse_mode="HTML")
            return
            
        tg_id = target_user["tg_user_id"]
        bind_tg_to_ip(source_ip, tg_id)
        if target_user["paid_until"]:
            update_user_field(source_ip, "paid_until", target_user["paid_until"])
            
        source_user = get_user_by_ip(source_ip)
        src_name = source_user["alias"] if source_user else source_ip
        tgt_name = target_user["alias"] or target_ip
        
        log_audit("LINK_CONFIGS", target_ip=source_ip, details=f"linked_to={target_ip},tg_id={tg_id}", admin_id=message.from_user.id)
        bot.reply_to(
            message,
            f"✅ <b>Конфиги успешно объединены!</b>\n\n"
            f"📱 Конфиг: <b>{src_name}</b> (<code>{source_ip}</code>)\n"
            f"👤 Привязан к аккаунту: <b>{tgt_name}</b> (<code>{target_ip}</code> | TG ID: <code>{tg_id}</code>)\n"
            f"📅 Подписка синхронизирована: <code>{target_user['paid_until'] or 'Безлимит'}</code>",
            parse_mode="HTML"
        )

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
    
    # ── /sync — Синхронизация БД с WireGuard/AmneziaWG ───────────────────────
    @bot.message_handler(commands=["sync"])
    @admin_only
    def cmd_sync_db(message):
        from vpn_engine import sync_db_with_vpn_peers, auto_apply_qos
        msg = bot.reply_to(message, "⏳ Выполняю синхронизацию БД с серверами VPN...")
        res = sync_db_with_vpn_peers()
        auto_apply_qos()
        log_audit("SYNC_DB", details=f"added={res['added']},cleaned={res['cleaned']}", admin_id=message.from_user.id)
        bot.edit_message_text(
            f"✅ <b>Синхронизация завершена!</b>\n\n"
            f"📥 Добавлено новых пиров: <b>{res['added']}</b>\n"
            f"🗑 Удалено фантомных записей: <b>{res['cleaned']}</b>\n"
            f"👥 Всего активных абонентов: <b>{res['total_active']}</b>",
            chat_id=message.chat.id,
            message_id=msg.message_id,
            parse_mode="HTML"
        )

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
        msg = f"🔍 Найдено: {len(results)}\n\n"
        for u in results[:15]:
            flag = "🇸🇪" if "SE" in u["server_alias"] else "🇷🇺"
            msg += f"{u['online_emoji']} {flag} {u['db_alias']} ({u['ip']})\n"
        msg += "\n_Подробное управление доступно в Web-панели._"
        bot.reply_to(message, msg, parse_mode="Markdown")

    # ── /widget ───────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["widget"])
    @admin_only
    def cmd_widget(message):
        from tasks import widget_save

        msg = bot.send_message(message.chat.id, "⏳ Запуск мониторинга...")
        bot._widget_chat_id = message.chat.id
        bot._widget_msg_id = msg.message_id
        widget_save(message.chat.id, msg.message_id)
        bot.pin_chat_message(message.chat.id, msg.message_id)
        bot.reply_to(message, "✅ Виджет создан. Обновление каждые 30 сек.")

    # ── Текстовые команды (4 кнопки главного меню) ───────────────────────────
    @bot.message_handler(func=lambda m: m.text in ("📡 Серверы", "🛠 Сервис"))
    @admin_only
    @rate_limit(2)
    def handle_menu(message):
        text = message.text

        if text == "📡 Серверы":
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("🖥 Статус", callback_data="menu_status"),
                InlineKeyboardButton("📈 Нагрузка", callback_data="menu_analysis"),
                InlineKeyboardButton("🩺 Пинг (МСК)", callback_data="menu_ping"),
            )
            markup.add(
                InlineKeyboardButton("🔄 Рестарт SE", callback_data="restart_ask|SE"),
                InlineKeyboardButton("🔄 Рестарт RU", callback_data="restart_ask|RU"),
            )
            bot.send_message(message.chat.id, "📡 *Серверы*", reply_markup=markup, parse_mode="Markdown")

        elif text == "🛠 Сервис":
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(
                InlineKeyboardButton("📋 Журнал действий", callback_data="menu_journal"),
                InlineKeyboardButton("💾 Бэкап сейчас", callback_data="menu_backup"),
                InlineKeyboardButton("🔔 Дайджест", callback_data="menu_digest"),
            )
            bot.send_message(message.chat.id, "🛠 *Сервис*", reply_markup=markup, parse_mode="Markdown")

    # ── #6: Кнопка «🔍 Поиск» — интерактивный поиск без /search ─────────────
    @bot.message_handler(func=lambda m: m.text == "🔍 Поиск" and m.from_user.id == cfg.admin_id)
    @admin_only
    def btn_search_admin(message):
        msg = bot.send_message(message.chat.id, "🔍 Введите имя или часть IP для поиска:")
        bot.register_next_step_handler(msg, _do_search_step)

    def _do_search_step(message):
        if message.from_user.id != cfg.admin_id:
            return
        query = (message.text or "").strip().lower()
        if not query:
            bot.reply_to(message, "⚠️ Пустой запрос.")
            return
        stats = get_vpn_stats()
        results = [s for s in stats if query in s["db_alias"].lower() or query in s["ip"]]
        if not results:
            bot.reply_to(message, f"🔍 Ничего не найдено по «{sanitize_alias(query)}».")
            return
        msg = f"🔍 Найдено: {len(results)}\n\n"
        for u in results[:15]:
            flag = "🇸🇪" if "SE" in u["server_alias"] else "🇷🇺"
            msg += f"{u['online_emoji']} {flag} {u['db_alias']} ({u['ip']})\n"
        msg += "\n_Подробное управление доступно в Web-панели._"
        bot.reply_to(message, msg, parse_mode="Markdown")

    # ── Inline-обработчики подменю ────────────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("menu_"))
    @admin_only_callback
    def cb_menu(call):
        action = call.data
        bot.answer_callback_query(call.id)

        if action == "menu_status":
            w = bot.send_message(call.message.chat.id, "⏳ Снимаю телеметрию...")
            cpu = psutil.cpu_percent(1)
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage("/").percent

            def _pbar(pct, width=8):
                filled = int(round(pct / 100 * width))
                bar = "█" * filled + "░" * (width - filled)
                dot = "🔴" if pct > 80 else ("🟡" if pct > 60 else "🟢")
                return f"{dot} `{bar}` {pct:.0f}%"

            result = (
                "📡 *Статус серверов*\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🇸🇪 *SE Server* (Main Node)\n"
                f"🖥 CPU:  {_pbar(cpu)}\n"
                f"💾 RAM:  {_pbar(ram)}\n"
                f"💽 Disk: {_pbar(disk)}\n"
            )
            ru = get_ru_metrics()
            if ru:
                result += (
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "🇷🇺 *RU Server* (Node)\n"
                    f"🖥 CPU:  {_pbar(float(ru['cpu']))}\n"
                    f"💾 RAM:  {_pbar(float(ru['ram']))}\n"
                    f"⏱ Uptime: `{ru['uptime']}`"
                )
            else:
                result += "━━━━━━━━━━━━━━━━━━━━\n🇷🇺 *RU Server*\n❌ Недоступен"
            bot.edit_message_text(result, call.message.chat.id, w.message_id, parse_mode="Markdown")

        elif action == "menu_analysis":
            # #8: выбор сервера для графика
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("🇸🇪 SE — График (24ч)", callback_data="analysis_chart|SE"),
                InlineKeyboardButton("🇷🇺 RU — Снимок", callback_data="analysis_chart|RU"),
            )
            bot.send_message(
                call.message.chat.id,
                "📊 *Нагрузка — выберите сервер:*",
                reply_markup=markup,
                parse_mode="Markdown",
            )

        elif action == "menu_ping":
            bot.send_message(call.message.chat.id, "🩺 Измеряю пинг с МСК...")
            pings = get_ru_ping()
            if len(pings) == 5:

                def _ping_bar(v):
                    if not v or v == "timeout":
                        return "🔴 ░░░░░░░░  timeout"
                    try:
                        ms = float(v)
                        bars = min(8, max(1, int(ms / 25)))
                        dot = "🟢" if ms < 50 else ("🟡" if ms < 120 else "🔴")
                        return f"{dot} {'█' * bars}{'░' * (8 - bars)}  {ms:.0f} мс"
                    except Exception:
                        return f"⚪ {v}"

                t = (
                    "🩺 *Пинг с 🇷🇺 Москвы:*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"🌍 Google        {_ping_bar(pings[0])}\n"
                    f"🛡 Cloudflare   {_ping_bar(pings[1])}\n"
                    f"🇷🇺 Yandex       {_ping_bar(pings[2])}\n"
                    f"✈️ Telegram      {_ping_bar(pings[3])}\n"
                    f"📸 Instagram    {_ping_bar(pings[4])}"
                )
                bot.send_message(call.message.chat.id, t, parse_mode="Markdown")
            else:
                bot.send_message(call.message.chat.id, "❌ Нет ответа от RU-сервера.")

        elif action == "menu_journal":
            # #9: фильтры журнала аудита
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("🌐 Все действия", callback_data="journal|all"),
                InlineKeyboardButton("💀 Скорость/Карцер", callback_data="journal|SET_SPEED"),
            )
            markup.add(
                InlineKeyboardButton("📅 Продления", callback_data="journal|EXTEND"),
                InlineKeyboardButton("🗑 Удаления", callback_data="journal|DELETE_PEER"),
            )
            bot.send_message(
                call.message.chat.id,
                "📋 *Журнал действий — выберите фильтр:*",
                reply_markup=markup,
                parse_mode="Markdown",
            )

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

def _daily_analysis():
    try:
        from database import get_server_history
        rows = get_server_history("SE", 24)
        if not rows:
            return "❌ Мало данных для анализа SE."
        peak_cpu = max(rows, key=lambda x: x.get("cpu", 0))
        peak_ram = max(rows, key=lambda x: x.get("ram", 0))
        peak_time = datetime.fromtimestamp(peak_cpu["timestamp"]).strftime("%d.%m %H:%M")
        return (
            "📈 *Анализ SE за 24ч:*\n"
            f"🔥 Пик CPU: `{peak_cpu.get('cpu', 0):.1f}%` ({peak_time})\n"
            f"💾 Пик RAM: `{peak_ram.get('ram', 0):.1f}%`\n"
            f"📊 Замеров: {len(rows)}"
        )
    except Exception as e:
        logger.error(f"Ошибка анализа SE: {e}")
        return "❌ Ошибка при формировании анализа."


def _generate_chart():
    import tempfile
    try:
        from database import get_server_history
        rows = get_server_history("SE", 24)
        if len(rows) < 2:
            return None
        times = [datetime.fromtimestamp(r["timestamp"]) for r in rows]
        cpus = [float(r.get("cpu", 0)) for r in rows]
        rams = [float(r.get("ram", 0)) for r in rows]

        fig, ax = plt.subplots(figsize=(9, 4.5), dpi=120)
        ax.plot(times, cpus, label="CPU (%)", color="#ff5722", linewidth=1.8, marker="o", markersize=3)
        ax.plot(times, rams, label="RAM (%)", color="#2196f3", linewidth=1.8, marker="s", markersize=3)
        ax.set_title("Нагрузка SE Server (последние 24ч)", fontsize=13, fontweight="bold", pad=10)
        ax.set_ylim(0, 105)
        ax.set_ylabel("Использование (%)", fontsize=10)
        ax.legend(loc="upper left")
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        fig.autofmt_xdate()
        fig.tight_layout()

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        fig.savefig(tmp.name)
        plt.close(fig)
        return tmp.name
    except Exception as e:
        logger.error(f"Ошибка генерации графика SE: {e}")
        return None


    # ── #8: График / Нагрузка — обработчик выбора сервера ────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("analysis_chart|"))
    @admin_only_callback
    def cb_analysis_chart(call):
        bot.answer_callback_query(call.id)
        side = call.data.split("|")[1]
        if side == "SE":
            bot.send_message(call.message.chat.id, "📊 Рисую график SE...")
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
        elif side == "RU":
            ru = get_ru_metrics()
            if ru:
                recv_mb = ru.get("net_recv", 0) // 1024 // 1024
                sent_mb = ru.get("net_sent", 0) // 1024 // 1024
                msg = (
                    f"🇷🇺 *RU Server — Текущий снимок:*\n\n"
                    f"🖥 CPU: `{ru['cpu']:.1f}%`\n"
                    f"💾 RAM: `{ru['ram']:.1f}%`\n"
                    f"⏱ Uptime: `{ru['uptime']}`\n"
                    f"⬇️ Всего получено: `{recv_mb:,} МБ`\n"
                    f"⬆️ Всего отправлено: `{sent_mb:,} МБ`\n\n"
                    f"_⚠️ Исторических данных RU нет — только текущий снимок._"
                )
                bot.send_message(call.message.chat.id, msg, parse_mode="Markdown")
            else:
                bot.send_message(call.message.chat.id, "❌ RU сервер недоступен.")

    # ── #9: Журнал аудита с фильтром по типу действий ────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("journal|"))
    @admin_only_callback
    def cb_journal_filter(call):
        bot.answer_callback_query(call.id)
        action_filter = call.data.split("|")[1]

        rows = get_audit_log(50)  # берём больше, т.к. будем фильтровать
        if action_filter != "all":
            rows = [r for r in rows if r["action"] == action_filter]
        rows = rows[:15]

        if not rows:
            return bot.send_message(call.message.chat.id, "📋 Нет записей по данному фильтру.")

        filter_labels = {
            "all": "🌐 Все действия",
            "SET_SPEED": "💀 Изменения скорости",
            "EXTEND": "📅 Продления подписок",
            "DELETE_PEER": "🗑 Удаления профилей",
        }
        label = filter_labels.get(action_filter, action_filter)
        _icons = {
            "SET_SPEED": "⚡", "EXTEND": "📅", "DELETE_PEER": "🗑",
            "BIND": "🔗", "UNBIND": "🔓", "RENAME": "✏️",
            "BROADCAST": "📢", "BACKUP": "💾", "BOT_START": "🚀",
            "BOT_SHUTDOWN": "🛑", "AUTO_PUNISH": "💀",
        }
        _names = {
            "SET_SPEED": "Изменил скорость", "EXTEND": "Продлил подписку",
            "DELETE_PEER": "Удалил профиль", "BIND": "Привязал TG",
            "UNBIND": "Отвязал TG", "RENAME": "Переименовал",
            "BROADCAST": "Рассылка", "BACKUP": "Создал бэкап",
            "BOT_START": "Бот запущен", "BOT_SHUTDOWN": "Бот остановлен",
            "AUTO_PUNISH": "Авто-карцер",
        }
        msg = f"📋 *{label}:*\n━━━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            ts_raw = r["timestamp"] or ""
            ts_short = ts_raw[5:16] if len(ts_raw) >= 16 else ts_raw
            act_key = r["action"] or ""
            icon = _icons.get(act_key, "🔹")
            act_name = _names.get(act_key, _md_escape(act_key))
            ip_s = f"\n   📍 `{r['target_ip']}`" if r["target_ip"] else ""
            det_s = f" — {_md_escape(r['details'])}" if r["details"] else ""
            msg += f"{icon} *{act_name}*   `{ts_short}`{ip_s}{det_s}\n\n"
        for chunk in _split(msg):
            try:
                bot.send_message(call.message.chat.id, chunk, parse_mode="Markdown")
            except Exception as e:
                logger_log = __import__("logging").getLogger("vpn_bot")
                logger_log.warning("Markdown parse failed in cb_journal_filter: %s", e)
                bot.send_message(call.message.chat.id, chunk)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    @bot.callback_query_handler(func=lambda c: c.data == "cancel_action")
    @admin_only_callback
    def cb_cancel(call):
        bot.answer_callback_query(call.id)
        bot.edit_message_text("❌ Действие отменено.", call.message.chat.id, call.message.message_id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("page|"))
    @admin_only_callback
    def cb_page(call):
        bot.answer_callback_query(call.id)
        parts = call.data.split("|")
        page = int(parts[1])
        flt = parts[2] if len(parts) > 2 else "all"
        _send_users_page(bot, call.message.chat.id, page, call.message.message_id, flt)

    # ── #2: Фильтры по списку абонентов ──────────────────────────────────────
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
            parse_mode="Markdown",
        )

    # ── #4: Экран подтверждения рестарта ─────────────────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("restart_ask|"))
    @admin_only_callback
    def cb_restart_ask(call):
        bot.answer_callback_query(call.id)
        side = call.data.split("|")[1]
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(f"⚠️ Да, перезапустить {side}!", callback_data=f"restart_confirm|{side}"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_action"),
        )
        bot.edit_message_text(
            f"⚠️ *Вы уверены?*\n\n"
            f"Перезапуск *{side}* отключит всех клиентов этого сервера на *10–30 секунд*.\n\n"
            f"Продолжить?",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown",
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("restart_confirm|"))
    @admin_only_callback
    @rate_limit(30)
    def cb_restart(call):
        bot.answer_callback_query(call.id)
        side = call.data.split("|")[1]
        bot.edit_message_text(f"🔄 Перезапускаю {side}...", call.message.chat.id, call.message.message_id)
        if side == "SE":
            local_names = list(dict.fromkeys(
                c["name"] for c in cfg.vpn_containers
                if c["host"] == "local" and c.get("name")
            ))
            subprocess.run(["docker", "restart", *local_names], capture_output=True, timeout=30)
            from vpn_engine import auto_apply_qos
            auto_apply_qos()
            bot.edit_message_text("✅ SE перезапущен и QoS восстановлен!", call.message.chat.id, call.message.message_id)
            log_audit("RESTART_SE", admin_id=call.from_user.id)
        elif side == "RU":
            ru = next((c for c in cfg.vpn_containers if "RU" in c["alias"]), None)
            if ru:
                try:
                    subprocess.run(
                        [
                            "ssh",
                            "-p",
                            str(ru["port"]),
                            "-o",
                            "StrictHostKeyChecking=yes",
                            "-o",
                            f"UserKnownHostsFile={os.path.expanduser('~/.ssh/known_hosts')}",
                            ru["host"],
                            "sudo",
                            "docker",
                            "restart",
                            ru["name"],
                        ],
                        capture_output=True,
                        timeout=25,
                    )
                    from vpn_engine import auto_apply_qos
                    auto_apply_qos()
                    bot.edit_message_text("✅ RU перезапущен и QoS восстановлен!", call.message.chat.id, call.message.message_id)
                    log_audit("RESTART_RU", admin_id=call.from_user.id)
                except Exception as e:
                    logger.error(f"Рестарт RU: {e}")
                    bot.edit_message_text("❌ Ошибка рестарта RU.", call.message.chat.id, call.message.message_id)
