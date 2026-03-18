from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Integer, String, Text, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db import Base

# pgvector import — conditional to support SQLite dev environments
try:
    from pgvector.sqlalchemy import Vector as _Vector
    _PGVECTOR_AVAILABLE = True
except ImportError:
    _PGVECTOR_AVAILABLE = False
    _Vector = None


class MemoryItem(Base):
    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String, nullable=False, default="general")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    # Semantic embedding — only declared when pgvector is available and enabled.
    # The column is created via ALTER TABLE in db._setup_pgvector(), not by create_all(),
    # so it's safe for existing databases. Nullable for backward compatibility.
    if _PGVECTOR_AVAILABLE and settings.semantic_memory_enabled:
        embedding: Mapped[Optional[list]] = mapped_column(
            _Vector(settings.embedding_dimensions), nullable=True
        )
