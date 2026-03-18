from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Integer, String, Text, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    is_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    # Optional: raw text the user typed (for debugging / UX confirmation)
    raw_input: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
