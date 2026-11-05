"""
webapp/schemas.py — Pydantic схемы валидации данных для FastAPI эндпоинтов.
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel


class SpeedPayload(BaseModel):
    speed: str


class RenamePayload(BaseModel):
    alias: str


class ExtendPayload(BaseModel):
    days: int = 0
    date: str = ""
    lifetime: bool = False


class BindPayload(BaseModel):
    tg_id: Optional[int] = None
    target_ip: Optional[str] = None


class BulkActionPayload(BaseModel):
    action: str
    ips: List[str]
    data: Dict[str, Any] = {}


class RegisterPayload(BaseModel):
    alias: str
    tg_id: str = ""
    days: int = 30


class BroadcastPayload(BaseModel):
    text: str


class TicketReplyPayload(BaseModel):
    text: str


class CreateUserPayload(BaseModel):
    alias: str
    server_name: str
    server_alias: str
