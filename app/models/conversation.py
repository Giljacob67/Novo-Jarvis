from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Integer, String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    # Rolling summary of archived messages — injected as context when non-null.
    # Updated automatically when the conversation exceeds HISTORY_SUMMARY_TRIGGER.
    summary_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    # ID of the last Message that was included in the current summary.
    # Messages with id <= this value are archived and excluded from the live window.
    summarized_up_to_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
