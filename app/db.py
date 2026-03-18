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


# ---------------------------------------------------------------------------
# Safe incremental migrations
# Each entry: (table, column, column_definition)
# Uses ADD COLUMN IF NOT EXISTS — idempotent, safe to run on every startup.
# Add new columns here whenever a feature branch introduces schema changes.
# ---------------------------------------------------------------------------
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # feat/history-summarization
    ("conversations",       "summary_text",           "TEXT"),
    ("conversations",       "summarized_up_to_id",    "INTEGER"),
    ("messages",            "is_archived",             "BOOLEAN NOT NULL DEFAULT FALSE"),
    # feat/semantic-memory-search
    ("memory_items",        "embedding_json",          "TEXT"),
    # feat/multiturn-intent
    ("conversation_states", "pending_question",        "TEXT"),
    ("conversation_states", "open_commitments_json",   "TEXT"),
    # feat/user-profile
    ("user_profiles",       "profile_json",            "TEXT NOT NULL DEFAULT '{}'"),
    ("user_profiles",       "updated_at",              "TIMESTAMP"),
]


def _is_postgresql() -> bool:
    return settings.jarvis_database_url.startswith("postgresql")


def run_safe_migrations() -> None:
    """Apply incremental ALTER TABLE ... ADD COLUMN IF NOT EXISTS migrations.

    Safe to run on every startup:
    - PostgreSQL: uses native ADD COLUMN IF NOT EXISTS (no-op if column exists)
    - SQLite: checks information_schema equivalent (pragma) before altering
    Each migration is logged at INFO level so Railway logs show what ran.
    """
    if not _is_postgresql():
        logger.info("Skipping column migrations on non-PostgreSQL database (SQLite dev mode).")
        return

    with engine.connect() as conn:
        for table, column, col_def in _COLUMN_MIGRATIONS:
            try:
                stmt = text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_def};"
                )
                conn.execute(stmt)
                conn.commit()
                logger.info("Migration OK: %s.%s (%s)", table, column, col_def)
            except Exception as exc:
                conn.rollback()
                logger.warning(
                    "Migration skipped for %s.%s — %s", table, column, exc
                )


def init_db() -> None:
    import app.models  # noqa: F401 — ensure all models are registered
    Base.metadata.create_all(bind=engine)
    run_safe_migrations()
