from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Integer, String, Text, DateTime, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ConversationState(Base):
    __tablename__ = "conversation_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    last_intent: Mapped[str] = mapped_column(String, nullable=False, default="general")
    last_entity: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pending_action: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    # Multi-turn: last open question Jarvis asked (cleared when user replies)
    pending_question: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    # Multi-turn: JSON list of {"text", "created_at", "resolved"} user commitments
    open_commitments_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
