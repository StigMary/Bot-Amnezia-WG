"""
bot.py — Главная точка входа.
Инициализация, регистрация хэндлеров, запуск планировщика, graceful shutdown.
"""
import os
import sys
import signal
import contextlib
import fcntl

import telebot

from config import cfg, logger
from database import init_db, log_audit
from vpn_engine import auto_apply_qos
from tasks import start_scheduler
import handlers.admin as admin_handler
import handlers.client as client_handler


# ─── Защита от двойного запуска ──────────────────────────────────────────────

_lock_file = None

def check_single_instance():
    global _lock_file
    _lock_file = open(cfg.pid_file, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
    except IOError:
        logger.error("Бот уже запущен (flock). Выход.")
        sys.exit(1)


# ─── Graceful shutdown ────────────────────────────────────────────────────────

def graceful_shutdown(bot: telebot.TeleBot, signum, frame):
    logger.info(f"Получен сигнал {signum}. Завершение...")
    log_audit("BOT_SHUTDOWN", details=f"signal={signum}")
    with contextlib.suppress(Exception):
        bot.stop_polling()
    global _lock_file
    if _lock_file:
        with contextlib.suppress(Exception):
            _lock_file.close()
    with contextlib.suppress(OSError):
        os.remove(cfg.pid_file)
    logger.info("Бот остановлен.")
    sys.exit(0)


# ─── Точка входа ─────────────────────────────────────────────────────────────

def main():
    check_single_instance()
    init_db()

    bot = telebot.TeleBot(cfg.token, parse_mode=None)

    # Регистрация хэндлеров:
    # admin ПЕРВЫМ — его /start с @admin_only перехватит команду раньше client
    admin_handler.register(bot)
    client_handler.register(bot)

    # Запуск планировщика
    start_scheduler(bot)

    # Авто-применение QoS из БД
    try:
        auto_apply_qos()
    except Exception as e:
        logger.error(f"Ошибка авто-QoS: {e}")

    # Сигналы завершения
    signal.signal(signal.SIGTERM, lambda s, f: graceful_shutdown(bot, s, f))
    signal.signal(signal.SIGINT,  lambda s, f: graceful_shutdown(bot, s, f))

    log_audit("BOT_START")
    logger.info("VPN Bot v3.0 запущен!")

    # Основной цикл
    while True:
        try:
            bot.polling(none_stop=True, timeout=90)
        except Exception as e:
            logger.error(f"Сбой polling, перезапуск через 5 сек: {e}")
            import time; time.sleep(5)


if __name__ == "__main__":
    main()
