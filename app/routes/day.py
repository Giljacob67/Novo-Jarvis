from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import get_db
from app.schemas.day import DayOverview
from app.services.assistant_service import get_real_or_mock_day_overview

router = APIRouter()


@router.get("/day", response_model=DayOverview)
async def get_day_overview(
    db: Session = Depends(get_db),
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
) -> DayOverview:
    from app.routes.telegram import _check_admin_key
    err = _check_admin_key(x_admin_key)
    if err:
        return err

    user_id = settings.telegram_allowed_user_id or "default"
    return await get_real_or_mock_day_overview(db, user_id)
