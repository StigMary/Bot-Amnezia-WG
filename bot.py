"""
bot.py — Точка входа Telegram-бота и фоновых служб.
Поддерживает режим Webhook (по умолчанию при запущенном webapp)
и режим Polling с экспоненциальным backoff (fallback).
"""

import contextlib
import os
import signal
import sys
import time
import telebot

import handlers.admin as admin_handler
import handlers.client as client_handler
from config import cfg, logger
from database import init_db, log_audit
from tasks import start_scheduler
from vpn_engine import auto_apply_qos

_lock_file = None

def check_single_instance():
    global _lock_file
    try:
        import fcntl
        _lock_file = open(cfg.pid_file, "a+")
        try:
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.error("Бот уже запущен (flock). Выход.")
            sys.exit(1)
        _lock_file.seek(0)
        _lock_file.truncate()
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
    except Exception as e:
        logger.warning(f"Single-instance check warning: {e}")

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

def main():
    check_single_instance()
    init_db()

    bot = telebot.TeleBot(cfg.token, parse_mode=None, threaded=True)
    admin_handler.register(bot)
    client_handler.register(bot)

    with contextlib.suppress(Exception):
        from telebot.types import BotCommand
        bot.set_my_commands([
            BotCommand("start", "Панель управления"),
            BotCommand("backup", "Скачать бэкап базы данных"),
        ])

    # Сигналы завершения
    signal.signal(signal.SIGTERM, lambda s, f: graceful_shutdown(bot, s, f))
    signal.signal(signal.SIGINT, lambda s, f: graceful_shutdown(bot, s, f))

    # Проверяем режим работы: Webhook vs Polling
    use_webhook = os.getenv("USE_WEBHOOK", "true").lower() in ("true", "1", "yes")
    webhook_base = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

    if use_webhook and webhook_base:
        full_webhook = f"{webhook_base}/webhook/telegram"
        try:
            current_info = bot.get_webhook_info()
            if current_info.url != full_webhook:
                logger.info(f"Обновление Webhook на: {full_webhook}")
                bot.remove_webhook()
                time.sleep(0.5)
                bot.set_webhook(url=full_webhook, max_connections=40)
            else:
                logger.info(f"Webhook уже активен: {full_webhook}")

            log_audit("BOT_START", details="Mode: Webhook Monitor")
            logger.info("VPN Bot v3.0 запущен в режиме монитора Webhook.")

            # В режиме Webhook обновления обрабатывает webapp (uvicorn).
            # Процесс bot.py следит за состоянием и планировщиком при необходимости.
            while True:
                time.sleep(60)
        except Exception as e:
            logger.error(f"Ошибка регистрации Webhook: {e}. Переход в Polling...")

    # Fallback / Ручной режим: Polling с надежным Exponential Backoff
    logger.info("Запуск в режиме Long Polling с защитой от сбоев Telegram...")
    with contextlib.suppress(Exception):
        bot.remove_webhook()

    start_scheduler(bot)
    with contextlib.suppress(Exception):
        auto_apply_qos()

    log_audit("BOT_START", details="Mode: Polling")

    backoff = 5
    max_backoff = 300
    while True:
        try:
            bot.polling(none_stop=False, timeout=30, long_polling_timeout=25)
            backoff = 5
        except Exception as e:
            logger.error(f"Polling error ({e}). Ожидание {backoff}с перед повтором...")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

if __name__ == "__main__":
    main()
