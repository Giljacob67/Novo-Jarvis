import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


# Configuração do engine com pooling otimizado para produção
try:
    if settings.jarvis_database_url.startswith("postgresql"):
        engine = create_engine(
            settings.jarvis_database_url,
            pool_size=10,
            max_overflow=20,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )
    else:
        engine = create_engine(
            settings.jarvis_database_url,
            connect_args={"check_same_thread": False},
        )
except Exception as e:
    import logging
    logging.getLogger(__name__).error("CRITICAL: Failed to create database engine: %s", e)
    raise

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    import app.models  # noqa: F401 — ensure all models are registered
    Base.metadata.create_all(bind=engine)
    _setup_pgvector()


def _setup_pgvector() -> None:
    """Enable pgvector extension and add embedding column if needed.

    Idempotent — safe to call on every startup. No-op when
    semantic_memory_enabled=False (SQLite dev environment).
    """
    if not settings.semantic_memory_enabled:
        return
    if not settings.jarvis_database_url.startswith("postgresql"):
        logger.warning("semantic_memory_enabled=True but not using PostgreSQL — skipping pgvector setup")
        return

    with engine.begin() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            logger.info("pgvector extension enabled")
        except Exception as e:
            logger.error("Failed to enable pgvector extension: %s", e)
            raise

        try:
            conn.execute(text(
                f"ALTER TABLE memory_items "
                f"ADD COLUMN IF NOT EXISTS embedding vector({settings.embedding_dimensions})"
            ))
            logger.info("embedding column ensured on memory_items (dimensions=%d)", settings.embedding_dimensions)
        except Exception as e:
            logger.error("Failed to add embedding column: %s", e)
            raise
