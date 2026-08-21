"""
SQLAlchemy engine/session setup.

Uses SQLite by default (zero setup, one file on disk) — the right default for
a single-business MVP. DATABASE_URL is read from settings, so switching to
Postgres later (e.g. postgresql+psycopg://...) is a one-line config change,
not a rewrite: all the models and queries below are database-agnostic SQLAlchemy.
"""
# pyrefly: ignore [missing-import]
from sqlalchemy import create_engine
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import get_settings

settings = get_settings()

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — yields a session, always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    # Imported here (not at module top) to avoid a circular import with models.py
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
