"""
Database models.

Deliberately small: Customer, Conversation (tracks where a customer is mid-flow),
Order, and Feedback. No auth/tenant tables — this is the single-business MVP
scope. If you later need multi-tenant, add a `business_slug` column to each
table and a Business table; the query patterns below don't have to change much.
"""
from datetime import datetime, timezone

# pyrefly: ignore [missing-import]
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Both used by broadcast messaging (app/admin.py): last_inbound_at decides
    # whether a free-text broadcast can legally reach this customer at all —
    # WhatsApp only allows free-form messages within 24h of their last
    # message; outside that window only a pre-approved template message
    # works, which this app doesn't send. opted_out lets a customer stop
    # promotional messages (see conversation.py's opt-out check) without
    # blocking order-related messages, which are a different message
    # category under WhatsApp's own policy.
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opted_out: Mapped[bool] = mapped_column(default=False)

    conversation: Mapped["Conversation"] = relationship(back_populates="customer", uselist=False)
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Conversation(Base):
    """
    Tracks exactly one thing: what state is this customer's chat in right now.

    state is one of: "idle", "ordering", "awaiting_feedback".
    draft_order is a JSON string holding whatever order fields have been
    captured so far while state == "ordering" (item, quantity, address —
    filled in incrementally as the customer answers).
    """
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="idle")
    draft_order: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    customer: Mapped["Customer"] = relationship(back_populates="conversation")


class Order(Base):
    """
    item/quantity are kept as a denormalized summary (not the source of truth
    once OrderItem exists) so every pre-cart-feature row already in production
    keeps displaying correctly with zero migration: for a single-item order
    they hold the exact item name/quantity (unchanged from before carts
    existed); for a real multi-item cart they hold a joined summary string
    and the total item count. OrderItem below is the real per-line-item data
    for anything that needs to price or display a cart accurately.
    """
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_ref: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    item: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    delivery_address: Mapped[str] = mapped_column(String(300), default="")
    total_ngn: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="Confirmed")  # Confirmed | Delivered
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="orders")
    feedback: Mapped["Feedback"] = relationship(back_populates="order", uselist=False)
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", order_by="OrderItem.id")


class OrderItem(Base):
    """One line item within an Order's cart — see Order's docstring."""
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    item: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    quantity_unit: Mapped[str] = mapped_column(String(32), default="plates")
    unit_price_ngn: Mapped[int] = mapped_column(Integer, default=0)
    subtotal_ngn: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped["Order"] = relationship(back_populates="items")


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    rating: Mapped[int] = mapped_column(Integer)
    raw_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    order: Mapped["Order"] = relationship(back_populates="feedback")
