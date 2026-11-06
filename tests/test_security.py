import os
import pytest
from fastapi.testclient import TestClient

import webapp.main as app_module
from webapp.utils import _server_tag

client = TestClient(app_module.app, raise_server_exceptions=False)


def test_path_traversal_blocked():
    """Проверка защиты от чтения файлов за пределами static/."""
    for path in ("/%2e%2e/%2e%2e/.env", "/../../.env", "/%2e%2e/.env"):
        response = client.get(path)
        assert "BOT_TOKEN" not in response.text
        assert response.headers.get("content-type", "").startswith("text/html")


def test_api_404_not_found():
    """Проверка, что несуществующие /api пути отдают 404, а не HTML."""
    response = client.get("/api/nonexistent_route")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_webhook_requires_secret():
    """Проверка аутентификации Telegram Webhook."""
    # Без секрета
    r_no_header = client.post("/webhook/telegram", json={"update_id": 1})
    assert r_no_header.status_code == 403

    # С неверным секретом
    r_wrong = client.post(
        "/webhook/telegram",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "invalid_secret"}
    )
    assert r_wrong.status_code == 403

    # С корректным секретом
    secret = os.environ.get("WEBHOOK_SECRET")
    r_valid = client.post(
        "/webhook/telegram",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": secret}
    )
    assert r_valid.status_code == 200


def test_api_requires_auth():
    """Проверка, что API требует авторизации."""
    from config import cfg

    # Запрос без авторизации
    r = client.get("/api/settings")
    assert r.status_code in (401, 403)

    # Запрос с неверным ключом
    r_bad = client.get("/api/settings", headers={"X-API-Key": "wrong_key"})
    assert r_bad.status_code in (401, 403)

    # Запрос с верным X-API-Key
    r_auth = client.get("/api/settings", headers={"X-API-Key": cfg.admin_api_key})
    assert r_auth.status_code == 200


def test_server_tag_isolation():
    """Проверка, что 'RU Server' не превращается в 'SE' из-за подстроки в SERVER."""
    assert _server_tag("🇷🇺 RU Server (Main)") == "RU"
    assert _server_tag("🇸🇪 SE Exit-Node") == "SE"
    assert _server_tag("🇩🇪 DE Server") == "DE"
    assert _server_tag("🇳🇱 NL Gateway") == "NL"
