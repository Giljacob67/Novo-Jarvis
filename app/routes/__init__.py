from fastapi import APIRouter

from app.routes.health import router as health_router
from app.routes.telegram import router as telegram_router
from app.routes.auth import router as auth_router
from app.routes.day import router as day_router
from app.routes.dashboard import router as dashboard_router

api_router = APIRouter()

api_router.include_router(health_router, tags=["health"])
api_router.include_router(telegram_router, prefix="/webhooks", tags=["webhooks"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(day_router, prefix="/me", tags=["me"])
api_router.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
