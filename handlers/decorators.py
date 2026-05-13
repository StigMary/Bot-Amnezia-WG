"""
handlers/decorators.py — Декораторы безопасности.
admin_only, admin_only_callback, group_member_only, rate_limit.
"""
import time
from functools import wraps
from collections import defaultdict

import telebot

from config import cfg, logger

_rate_limits: dict = defaultdict(float)

# Кэш проверок участия в группе: {uid: (is_member, expires_ts)}
_group_member_cache: dict = {}
_GROUP_CACHE_TTL = 300  # 5 минут


# ─── Admin ───────────────────────────────────────────────────────────────────

def admin_only(func):
    """Пропускает обработчик только для ADMIN_ID (message)."""
    @wraps(func)
    def wrapper(message: telebot.types.Message, *args, **kwargs):
        if message.from_user.id != cfg.admin_id:
            return
        return func(message, *args, **kwargs)
    return wrapper


def admin_only_callback(func):
    """Пропускает callback только для ADMIN_ID."""
    @wraps(func)
    def wrapper(call: telebot.types.CallbackQuery, *args, **kwargs):
        if call.from_user.id != cfg.admin_id:
            # Получаем бот-инстанс через атрибут, пробрасываемый при регистрации
            try:
                call.bot.answer_callback_query(call.id, "⛔ Нет доступа.", show_alert=True)
            except Exception:
                pass
            return
        return func(call, *args, **kwargs)
    return wrapper


# ─── Group member ─────────────────────────────────────────────────────────────

def group_member_only(bot: telebot.TeleBot):
    """
    Фабрика декоратора: проверяет, что пользователь состоит в закрытой группе.
    Использование:
        @group_member_only(bot)
        def handler(message): ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(message: telebot.types.Message, *args, **kwargs):
            if cfg.group_chat_id == 0:
                # Если GROUP_CHAT_ID не задан — пропускаем проверку
                logger.warning("GROUP_CHAT_ID не задан, проверка группы пропущена.")
                return func(message, *args, **kwargs)

            uid = message.from_user.id
            # Админ всегда проходит
            if uid == cfg.admin_id:
                return func(message, *args, **kwargs)

            now = time.time()
            cached = _group_member_cache.get(uid)
            if cached and cached[1] > now:
                is_member = cached[0]
            else:
                is_member = False
                try:
                    member = bot.get_chat_member(cfg.group_chat_id, uid)
                    if member.status in ("member", "administrator", "creator"):
                        is_member = True
                except Exception as e:
                    logger.warning(f"group_member_only: ошибка проверки uid={uid}: {e}")
                # Кэшируем только положительный результат надолго,
                # отказ — на короткий промежуток (вдруг пользователь только-только вступил).
                ttl = _GROUP_CACHE_TTL if is_member else 30
                _group_member_cache[uid] = (is_member, now + ttl)

            if is_member:
                return func(message, *args, **kwargs)
            else:
                bot.reply_to(
                    message,
                    "⛔ Доступ запрещён.\nВы не состоите в закрытой группе.",
                )

        return wrapper
    return decorator


# ─── Rate limit ──────────────────────────────────────────────────────────────

def rate_limit(seconds: int = 3):
    """Ограничивает частоту вызовов для каждого пользователя."""
    def decorator(func):
        @wraps(func)
        def wrapper(message_or_call, *args, **kwargs):
            uid = getattr(getattr(message_or_call, "from_user", None), "id", 0)
            key = f"{func.__name__}:{uid}"
            now = time.time()
            if now - _rate_limits[key] < seconds:
                return
            _rate_limits[key] = now
            return func(message_or_call, *args, **kwargs)
        return wrapper
    return decorator
