"""
db/connection.py — Потокобезопасный пул постоянных SQLite соединений с WAL-режимом.
"""
import contextlib
import sqlite3
import threading
import time
from typing import Generator

from config import cfg, logger

_local = threading.local()
_WAL_INITIALIZED = False
_INIT_LOCK = threading.Lock()


def _get_thread_conn() -> sqlite3.Connection:
    """Возвращает постоянное соединение для текущего потока, инициализируя его при первом вызове."""
    global _WAL_INITIALIZED
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            _local.conn = None

    conn = sqlite3.connect(cfg.db_file, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row

    with _INIT_LOCK:
        if not _WAL_INITIALIZED:
            with contextlib.suppress(Exception):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            _WAL_INITIALIZED = True

    with contextlib.suppress(Exception):
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA cache_size=-8000")  # 8 MB оперативного кэша
        conn.execute("PRAGMA temp_store=MEMORY")

    _local.conn = conn
    return conn


@contextlib.contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    """Потокобезопасное соединение с автокоммитом / роллбэком.

    Использует thread-local кэширование: файл базы на диске не закрывается
    и не открывается заново на каждый чих, экономя системные ресурсы и дескрипторы ОС.
    При блокировках базы автоматически повторяет попытку (до 3 раз с экспоненциальной задержкой).
    """
    conn = _get_thread_conn()
    max_retries = 3

    for attempt in range(max_retries):
        try:
            yield conn
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            conn.rollback()
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                time.sleep(0.05 * (2 ** attempt))
                continue
            raise
        except Exception:
            conn.rollback()
            raise


def close_thread_conn() -> None:
    """Закрывает соединение текущего потока."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()
        _local.conn = None
