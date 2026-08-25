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
    _add_missing_columns()


def _add_missing_columns():
    """
    create_all() only creates missing TABLES — it never alters a table that
    already exists, so adding a new column to an existing model (as opposed
    to a whole new table like OrderItem) needs its own handling here. There's
    no migration framework in this project (deliberately, for a single-
    business MVP — see models.py's own docstring), so this does the smallest
    thing that's actually safe: inspect what columns really exist in the
    live database and ALTER TABLE for exactly the ones missing. A no-op on a
    fresh database, since create_all() already created every current column.
    """
    # pyrefly: ignore [missing-import]
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "customers" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("customers")}
    statements = []
    if "last_inbound_at" not in existing:
        statements.append("ALTER TABLE customers ADD COLUMN last_inbound_at TIMESTAMP")
    if "opted_out" not in existing:
        statements.append("ALTER TABLE customers ADD COLUMN opted_out BOOLEAN DEFAULT FALSE")
    if not statements:
        return
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
