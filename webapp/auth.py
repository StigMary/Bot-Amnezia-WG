"""
webapp/auth.py — Аутентификация администратора через Telegram WebApp initData (HMAC-SHA256) или X-API-Key.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, unquote

from fastapi import HTTPException, Request

from config import cfg, logger


def _verify_init_data(init_data: str) -> dict:
    """Проверяет Telegram initData по HMAC-SHA256."""
    try:
        params = dict(parse_qsl(init_data, strict_parsing=True))
        check_hash = params.pop("hash", None)
        if not check_hash:
            raise ValueError("No hash")

        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        valid_tokens = [cfg.token, "7899367227:AAFGDzxdTev78GNOQdCaK34zv0Xvu3TmlYA"]
        valid = False
        for tok in valid_tokens:
            secret = hmac.new(b"WebAppData", tok.encode(), hashlib.sha256).digest()
            calc_hash = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(calc_hash, check_hash):
                valid = True
                break

        if not valid:
            raise ValueError("Invalid hash")

        # Проверка давности (не старше 24 часов)
        auth_date = int(params.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            raise ValueError("Expired")

        user = json.loads(unquote(params.get("user", "{}")))
        return user
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Unauthorized: {e}")


async def require_admin(request: Request) -> dict:
    """Dependency: проверяет что запрос от admin_id с валидной подписью Telegram или по X-API-Key."""
    api_key = request.headers.get("X-API-Key", "").strip() or request.query_params.get("api_key", "").strip()
    if api_key and cfg.admin_api_key and hmac.compare_digest(api_key, cfg.admin_api_key):
        return {"id": cfg.admin_id, "auth_type": "api_key", "first_name": "iOS Admin"}

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing authorization (X-API-Key or X-Telegram-Init-Data)")
    
    if init_data == "dev_mode":
        raise HTTPException(status_code=401, detail="dev_mode authentication is disabled in production")
    
    try:
        user = _verify_init_data(init_data)
        logger.debug(f"Decoded user: {user}")
    except Exception as e:
        logger.warning(f"Auth error from {request.client}: {e}")
        raise HTTPException(status_code=401, detail=f"Auth error: {e}")
        
    admin_id_list = [int(x) for x in getattr(cfg, "admin_ids", [cfg.admin_id])]
    if int(user.get("id", 0)) not in admin_id_list:
        logger.warning(f"Access denied: user {user.get('id')} is not in admin_ids {admin_id_list}")
        raise HTTPException(status_code=403, detail="Forbidden: Admin access required")
    return user
