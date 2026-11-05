"""
webapp/routers — Модульные роутеры FastAPI для WebApp админ-панели.
"""
from .dashboard import router as dashboard_router
from .subscribers import router as subscribers_router
from .waiting_room import router as waiting_room_router
from .servers import router as servers_router
from .settings import router as settings_router
from .tickets import router as tickets_router
from .broadcast import router as broadcast_router

__all__ = [
    "dashboard_router",
    "subscribers_router",
    "waiting_room_router",
    "servers_router",
    "settings_router",
    "tickets_router",
    "broadcast_router",
]
