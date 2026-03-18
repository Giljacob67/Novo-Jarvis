import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.memory_item import MemoryItem

logger = logging.getLogger(__name__)


def save_memory(
    db: Session,
    user_id: str,
    content: str,
    category: str = "general",
    source: str = "user",
    embedding: Optional[list] = None,
) -> MemoryItem:
    kwargs: dict = dict(
        user_id=user_id,
        content=content,
        category=category,
        source=source,
    )
    # Only set embedding when the column is declared on the model (semantic_memory_enabled=True)
    if embedding is not None and settings.semantic_memory_enabled:
        kwargs["embedding"] = embedding
    item = MemoryItem(**kwargs)
    db.add(item)
    db.commit()
    db.refresh(item)
    logger.info("Saved memory id=%s for user=%s category=%s", item.id, user_id, category)
    return item


def list_memories(
    db: Session,
    user_id: str,
    limit: int = 10,
) -> list[MemoryItem]:
    return (
        db.query(MemoryItem)
        .filter(MemoryItem.user_id == user_id, MemoryItem.is_active == True)
        .order_by(MemoryItem.created_at.desc())
        .limit(limit)
        .all()
    )


def search_memories(
    db: Session,
    user_id: str,
    query: str,
    limit: int = 10,
) -> list[MemoryItem]:
    """SQL LIKE search — kept for backward compatibility and non-semantic contexts."""
    return (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.is_active == True,
            MemoryItem.content.ilike(f"%{query}%"),
        )
        .order_by(MemoryItem.created_at.desc())
        .limit(limit)
        .all()
    )


def search_memories_semantic(
    db: Session,
    user_id: str,
    query_embedding: list[float],
    limit: int = 5,
) -> list[MemoryItem]:
    """Vector similarity search using pgvector cosine distance.

    Returns memories ordered by relevance to the query embedding.
    Skips records without embeddings (backward compatible with old entries).
    Requires semantic_memory_enabled=True and pgvector installed.
    """
    from pgvector.sqlalchemy import Vector
    from sqlalchemy import text

    # Use raw SQL for the cosine distance operator (<=>)
    # This avoids SQLAlchemy ORM limitations with custom operators
    sql = text("""
        SELECT id FROM memory_items
        WHERE user_id = :user_id
          AND is_active = true
          AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:embedding AS vector)
        LIMIT :limit
    """)
    rows = db.execute(
        sql,
        {
            "user_id": user_id,
            "embedding": str(query_embedding),
            "limit": limit,
        },
    ).fetchall()

    if not rows:
        return []

    ids = [row[0] for row in rows]
    # Fetch full ORM objects in the same order
    items_by_id = {
        item.id: item
        for item in db.query(MemoryItem).filter(MemoryItem.id.in_(ids)).all()
    }
    return [items_by_id[i] for i in ids if i in items_by_id]


def recall_memories(
    db: Session,
    user_id: str,
    query_embedding: Optional[list[float]] = None,
    limit: int = 10,
) -> list[MemoryItem]:
    """Unified memory retrieval entry point.

    Uses semantic vector search when enabled and an embedding is provided.
    Falls back to recency-based listing otherwise (preserves existing behaviour).
    """
    if settings.semantic_memory_enabled and query_embedding is not None:
        try:
            return search_memories_semantic(db, user_id, query_embedding, limit)
        except Exception as e:
            logger.warning("semantic memory search failed, falling back to recency: %s", e)
    return list_memories(db, user_id, limit)


VALID_CATEGORIES = [
    "general", "profile", "preference", "project", "contact",
    "routine", "decision", "followup", "voice_preference",
    # Added for auto-extraction
    "compromisso", "preferência", "projeto", "contato", "decisão", "informação_pessoal",
]


def get_memories_by_context(
    db: Session,
    user_id: str,
    categories: list[str],
    limit: int = 20,
) -> list[MemoryItem]:
    return (
        db.query(MemoryItem)
        .filter(
            MemoryItem.user_id == user_id,
            MemoryItem.is_active == True,
            MemoryItem.category.in_(categories),
        )
        .order_by(MemoryItem.created_at.desc())
        .limit(limit)
        .all()
    )
