import os
import sys

# Установка переменных окружения для тестового режима
os.environ.setdefault("BOT_TOKEN", "123456789:TEST_BOT_TOKEN_ABCDEF")
os.environ.setdefault("ADMIN_ID", "123456")
os.environ.setdefault("ADMIN_API_KEY", "test_admin_api_key_12345678901234567890")
os.environ.setdefault("WEBHOOK_SECRET", "test_webhook_secret_key_1234567890")
os.environ.setdefault("NODE_1", "Test Server|local|22|root|test-container|awg0")
os.environ.setdefault("DB_FILE", "/tmp/test_vpn_bot.db" if os.name != "nt" else "test_vpn_bot.db")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from database import init_db
init_db()
