"""
Database models.

Multi-tenant: every business (Business row) is a separate demo/tenant sharing
this one deployment and, for now, one WhatsApp number — see conversation.py's
_resolve_business for how an inbound message gets routed to the right one.
Businesses have a `vertical` (e.g. "food_ordering", "appointment_booking")
that decides which conversation flow and which menu/service tables apply.
Order/Appointment/MessageTemplate all carry business_id, so history stays
correctly separated per business; Customer.business_id is different — see
its own docstring — it's "which business is this phone talking to right
now," not a permanent per-business identity.
"""
from datetime import datetime, timezone

# pyrefly: ignore [missing-import]
from sqlalchemy import String, Integer, DateTime, ForeignKey, Text
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Business(Base):
    """
    One tenant. is_default marks the business an inbound message resolves to
    when its text doesn't match any business's own trigger_words AND the
    phone has no existing relationship with any business yet — this is what
    lets every pre-existing single-business behavior (a customer just saying
    "order" with no keyword) keep working unchanged, while a NEW trigger word
    ("salon", etc.) opens a completely different demo on the same number.
    """
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    vertical: Mapped[str] = mapped_column(String(32))  # "food_ordering" | "appointment_booking"
    greeting_emoji: Mapped[str] = mapped_column(String(8), default="👋")
    # Comma-separated, matched case-insensitively as whole words against the
    # customer's very first (or any) message — see conversation.py.
    trigger_words: Mapped[str] = mapped_column(String(200), default="")
    is_default: Mapped[bool] = mapped_column(default=False)
    review_link: Mapped[str] = mapped_column(String(300), default="")
    payment_link_base: Mapped[str] = mapped_column(String(300), default="")  # food_ordering only
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    menu_items: Mapped[list["FoodMenuItem"]] = relationship(back_populates="business")
    services: Mapped[list["AppointmentService"]] = relationship(back_populates="business")


class FoodMenuItem(Base):
    """A business's food_ordering menu item — the DB-backed equivalent of the
    old business_config.py MenuItem dataclass, now per-tenant."""
    __tablename__ = "food_menu_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    number: Mapped[str] = mapped_column(String(8))  # unique within the business, not globally
    name: Mapped[str] = mapped_column(String(120))
    price_ngn: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(60))
    quantity_unit: Mapped[str] = mapped_column(String(32), default="plates")

    business: Mapped["Business"] = relationship(back_populates="menu_items")


class AppointmentService(Base):
    """A business's appointment_booking service (e.g. "Haircut")."""
    __tablename__ = "appointment_services"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    number: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(120))
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    price_ngn: Mapped[int] = mapped_column(Integer, default=0)

    business: Mapped["Business"] = relationship(back_populates="services")


class Customer(Base):
    """
    business_id is which business this customer is CURRENTLY talking to, not
    a permanent identity tuple — phone stays globally unique (one Customer
    row per phone number, unchanged from before multi-tenancy) rather than
    (business_id, phone), deliberately: this is a demo/MVP where a phone
    number explores one business's flow at a time, not a real platform where
    the same person is simultaneously an independent customer of several
    businesses. Switching businesses (see conversation.py's _resolve_business)
    updates business_id in place and resets the conversation — actual order/
    appointment HISTORY stays correctly separated per business regardless,
    since Order/Appointment carry their own business_id.
    """
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
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

    business: Mapped["Business"] = relationship()
    conversation: Mapped["Conversation"] = relationship(back_populates="customer", uselist=False)
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="customer")


class Conversation(Base):
    """
    Tracks exactly one thing: what state is this customer's chat in right now.

    state depends on the customer's business's vertical:
      food_ordering:       idle | ordering | awaiting_feedback
      appointment_booking: idle | booking
    draft_order is a JSON string holding whatever fields have been captured so
    far while mid-flow — its shape also depends on the vertical (see
    conversation.py's _continue_order vs _continue_booking).
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
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    order_ref: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    item: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    delivery_address: Mapped[str] = mapped_column(String(300), default="")
    total_ngn: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="Confirmed")  # Confirmed | Delivered
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    business: Mapped["Business"] = relationship()
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


class Appointment(Base):
    """A booked appointment_booking slot — the equivalent of Order for that vertical."""
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    booking_ref: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    service_name: Mapped[str] = mapped_column(String(120))
    price_ngn: Mapped[int] = mapped_column(Integer, default=0)
    slot_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="Confirmed")  # Confirmed | Cancelled | Completed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    business: Mapped["Business"] = relationship()
    customer: Mapped["Customer"] = relationship(back_populates="appointments")


class MessageTemplate(Base):
    """
    A WhatsApp message template submitted for Meta's approval (app/admin.py's
    /templates routes) — the only way to message a customer outside
    WhatsApp's 24h reply window (see app/admin.py's broadcast routes).
    status starts "PENDING" and is only ever updated by explicitly re-checking
    with Meta (there's no webhook wired up for template status changes), so
    it can go stale between checks — treat it as "as of the last refresh."
    Scoped per business — a promo template for one business's customers
    shouldn't be usable to message another business's.
    """
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    # Stays globally unique (unchanged from before multi-tenancy, avoids
    # altering a constraint on a table that may already have real rows) — two
    # businesses sharing this MVP just need distinct template names.
    name: Mapped[str] = mapped_column(String(120), unique=True)
    language: Mapped[str] = mapped_column(String(10), default="en_US")
    body_text: Mapped[str] = mapped_column(Text)
    has_variable: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")  # PENDING | APPROVED | REJECTED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    business: Mapped["Business"] = relationship()
