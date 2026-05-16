"""
bot.py — Главная точка входа.
Инициализация, регистрация хэндлеров, запуск планировщика, graceful shutdown.
"""

import contextlib
import os
import signal
import sys

try:
    import fcntl  # POSIX

    _HAS_FCNTL = True
except ImportError:
    fcntl = None
    _HAS_FCNTL = False

try:
    import msvcrt  # Windows

    _HAS_MSVCRT = True
except ImportError:
    msvcrt = None
    _HAS_MSVCRT = False

import telebot

import handlers.admin as admin_handler
import handlers.client as client_handler
from config import cfg, logger
from database import init_db, log_audit
from tasks import start_scheduler
from vpn_engine import auto_apply_qos

# ─── Защита от двойного запуска ──────────────────────────────────────────────

_lock_file = None


def check_single_instance():
    """
    Кросс-платформенная защита от двойного запуска.
    POSIX: fcntl.flock; Windows: msvcrt.locking; иначе — мягкая проверка PID-файла.
    """
    global _lock_file
    try:
        _lock_file = open(cfg.pid_file, "a+")
    except OSError as e:
        logger.warning(f"Не удалось открыть pid-файл {cfg.pid_file}: {e}. Пропускаем lock.")
        return

    if _HAS_FCNTL:
        try:
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.error("Бот уже запущен (flock). Выход.")
            sys.exit(1)
    elif _HAS_MSVCRT:
        try:
            _lock_file.seek(0)
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            logger.error("Бот уже запущен (msvcrt). Выход.")
            sys.exit(1)
    else:
        logger.warning("Нет fcntl/msvcrt — single-instance защита отключена.")

    try:
        _lock_file.seek(0)
        _lock_file.truncate()
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
    except Exception as e:
        logger.warning(f"Не удалось записать PID в {cfg.pid_file}: {e}")


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
    signal.signal(signal.SIGINT, lambda s, f: graceful_shutdown(bot, s, f))

    log_audit("BOT_START")
    logger.info("VPN Bot v3.0 запущен!")

    # Основной цикл с экспоненциальным backoff
    import time

    backoff = 5
    max_backoff = 300  # 5 минут
    while True:
        try:
            bot.polling(none_stop=True, timeout=90)
            backoff = 5  # сбрасываем при штатном выходе
        except Exception as e:
            logger.error(f"Сбой polling, перезапуск через {backoff} сек: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    main()
