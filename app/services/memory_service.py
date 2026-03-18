import asyncio
import json
import logging
import math
from typing import Optional

from sqlalchemy.orm import Session

from app.models.memory_item import MemoryItem

logger = logging.getLogger(__name__)

_EMBEDDING_MODEL = "text-embedding-3-small"
_EMBEDDING_DIMS = 512  # truncated for storage efficiency


async def embed_text(text: str) -> Optional[list[float]]:
    """Return a semantic embedding vector for text via OpenAI.
    Returns None if API key is missing or call fails."""
    try:
        from app.config import settings
        from openai import AsyncOpenAI
        api_key = settings.openai_api_key
        if not api_key:
            return None
        client = AsyncOpenAI(api_key=api_key)
        response = await client.embeddings.create(
            model=_EMBEDDING_MODEL,
            input=text[:8000],  # guard against over-length inputs
            dimensions=_EMBEDDING_DIMS,
        )
        return response.data[0].embedding
    except Exception as exc:
        logger.warning("embed_text failed: %s", exc)
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _embed_and_store(memory_id: int) -> None:
    """Background: generate embedding for an existing MemoryItem and persist it."""
    try:
        from app.db import SessionLocal
        db = SessionLocal()
        try:
            item = db.query(MemoryItem).filter(MemoryItem.id == memory_id).first()
            if not item or item.embedding_json:
                return
            vec = await embed_text(item.content)
            if vec:
                item.embedding_json = json.dumps(vec)
                db.commit()
                logger.debug("Embedded memory id=%d", memory_id)
        finally:
            db.close()
    except Exception:
        logger.exception("_embed_and_store failed for memory_id=%d", memory_id)


def save_memory(
    db: Session,
    user_id: str,
    content: str,
    category: str = "general",
    source: str = "user",
) -> MemoryItem:
    item = MemoryItem(
        user_id=user_id,
        content=content,
        category=category,
        source=source,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    logger.info("Saved memory id=%s for user=%s category=%s", item.id, user_id, category)
    # Fire-and-forget: generate semantic embedding in background
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_embed_and_store(item.id))
    except RuntimeError:
        pass  # no event loop (e.g. tests / CLI) — skip embedding
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


async def search_memories_semantic(
    db: Session,
    user_id: str,
    query_text: str,
    limit: int = 10,
) -> list[MemoryItem]:
    """Return the most semantically relevant active memories for query_text.

    Strategy:
    - Embed the query with text-embedding-3-small
    - Score all active memories that have embedding_json via cosine similarity
    - Memories without embeddings are ranked last (recency order)
    - Falls back to list_memories() if embedding call fails
    """
    query_vec = await embed_text(query_text)
    if query_vec is None:
        # Graceful fallback: return most recent memories
        return list_memories(db, user_id, limit)

    all_items = (
        db.query(MemoryItem)
        .filter(MemoryItem.user_id == user_id, MemoryItem.is_active == True)  # noqa: E712
        .all()
    )

    scored: list[tuple[float, MemoryItem]] = []
    unscored: list[MemoryItem] = []

    for item in all_items:
        if item.embedding_json:
            try:
                item_vec = json.loads(item.embedding_json)
                sim = _cosine_similarity(query_vec, item_vec)
                scored.append((sim, item))
            except Exception:
                unscored.append(item)
        else:
            unscored.append(item)

    scored.sort(key=lambda t: t[0], reverse=True)
    ranked = [item for _, item in scored] + unscored
    return ranked[:limit]


VALID_CATEGORIES = [
    "general", "profile", "preference", "project", "contact",
    "routine", "decision", "followup", "voice_preference",
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
