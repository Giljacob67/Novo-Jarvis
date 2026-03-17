from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


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
