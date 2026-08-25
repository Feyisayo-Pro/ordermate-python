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

    # Multi-tenancy landed after this app already had real production data
    # under one implicit business — every pre-existing customers/orders/
    # message_templates row needs a business_id, and there's nothing to
    # backfill it FROM until the businesses themselves exist, so seeding runs
    # before the backfill step below.
    from app.businesses import seed_businesses, get_default_business
    db = SessionLocal()
    try:
        seed_businesses(db)
        default_business = get_default_business(db)
        _backfill_business_ids(default_business.id)
    finally:
        db.close()


def _add_missing_columns():
    """
    create_all() only creates missing TABLES — it never alters a table that
    already exists, so adding a new column to an existing model (as opposed
    to a whole new table like OrderItem/Business) needs its own handling
    here. There's no migration framework in this project (deliberately, for
    an MVP — see models.py's own docstring), so this does the smallest thing
    that's actually safe: inspect what columns really exist in the live
    database and ALTER TABLE for exactly the ones missing. A no-op on a
    fresh database, since create_all() already created every current column.

    business_id is deliberately added WITHOUT a NOT NULL constraint (even
    though the model treats it as always-present) — enforcing NOT NULL on an
    existing live table needs a constraint change this app has no safe way
    to make without a real migration framework; the one-time backfill in
    init_db() below achieves the same practical result without it.
    """
    # pyrefly: ignore [missing-import]
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    statements = []

    if "customers" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("customers")}
        if "last_inbound_at" not in existing:
            statements.append("ALTER TABLE customers ADD COLUMN last_inbound_at TIMESTAMP")
        if "opted_out" not in existing:
            statements.append("ALTER TABLE customers ADD COLUMN opted_out BOOLEAN DEFAULT FALSE")
        if "business_id" not in existing:
            statements.append("ALTER TABLE customers ADD COLUMN business_id INTEGER")

    if "orders" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("orders")}
        if "business_id" not in existing:
            statements.append("ALTER TABLE orders ADD COLUMN business_id INTEGER")

    if "message_templates" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("message_templates")}
        if "business_id" not in existing:
            statements.append("ALTER TABLE message_templates ADD COLUMN business_id INTEGER")

    if not statements:
        return
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def _backfill_business_ids(default_business_id: int):
    """Every customers/orders/message_templates row that predates multi-tenancy
    belongs to the one business that existed before it — the seeded default."""
    # pyrefly: ignore [missing-import]
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("UPDATE customers SET business_id = :id WHERE business_id IS NULL"), {"id": default_business_id})
        conn.execute(text("UPDATE orders SET business_id = :id WHERE business_id IS NULL"), {"id": default_business_id})
        conn.execute(text("UPDATE message_templates SET business_id = :id WHERE business_id IS NULL"), {"id": default_business_id})
