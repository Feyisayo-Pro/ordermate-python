"""
Seed data for every business (tenant) this deployment demos, plus the lookup
helpers conversation.py/ai.py use instead of the old single static
business_config.py::ACTIVE_BUSINESS constant.

Businesses are still defined in code here, not through a self-service
onboarding UI (out of scope for this MVP — see the "one shared admin login"
decision this multi-tenant round was built around), but the RUNTIME data is
DB-backed (Business, FoodMenuItem, AppointmentService rows), so the
conversation flow, admin dashboard, and broadcast/template features all work
against real per-tenant data rather than one hardcoded constant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Session

from app import models


@dataclass(frozen=True)
class _SeedMenuItem:
    number: str  # unique within the business, not globally
    name: str
    price_ngn: int
    category: str
    quantity_unit: str = "plates"


@dataclass(frozen=True)
class _SeedService:
    number: str  # unique within the business, not globally
    name: str
    duration_minutes: int
    price_ngn: int


@dataclass(frozen=True)
class _SeedBusiness:
    slug: str
    display_name: str
    vertical: str  # "food_ordering" | "appointment_booking"
    greeting_emoji: str
    trigger_words: str  # comma-separated
    is_default: bool
    review_link: str = ""
    payment_link_base: str = ""  # food_ordering only
    menu_items: tuple[_SeedMenuItem, ...] = field(default_factory=tuple)
    services: tuple[_SeedService, ...] = field(default_factory=tuple)


MAMAS_KITCHEN = _SeedBusiness(
    slug="mamas_kitchen",
    display_name="Mama's Kitchen",
    vertical="food_ordering",
    greeting_emoji="🍲",
    # is_default=True means this is also what ANY unrecognized message from a
    # brand-new phone number resolves to — the flagship demo, not something a
    # prospect needs to know a keyword for.
    trigger_words="restaurant,food,order food,mamas kitchen",
    is_default=True,
    review_link="https://g.page/mamaskitchen/review",
    payment_link_base="https://paystack.link/mk-",
    menu_items=(
        _SeedMenuItem(number="1", name="Jollof Rice + Chicken", price_ngn=3500, category="Mains"),
        _SeedMenuItem(number="2", name="Fried Rice + Turkey", price_ngn=4000, category="Mains"),
        _SeedMenuItem(number="3", name="Ofada + Beef", price_ngn=4500, category="Mains"),
        _SeedMenuItem(number="4", name="Chapman", price_ngn=1500, category="Drinks", quantity_unit="bottles"),
        _SeedMenuItem(number="5", name="Coca-Cola", price_ngn=500, category="Drinks", quantity_unit="bottles"),
    ),
)

GLOW_SALON = _SeedBusiness(
    slug="glow_salon",
    display_name="Glow Beauty Salon",
    vertical="appointment_booking",
    greeting_emoji="💇",
    trigger_words="salon,appointment,book,haircut,braiding",
    is_default=False,
    services=(
        _SeedService(number="1", name="Haircut", duration_minutes=45, price_ngn=5000),
        _SeedService(number="2", name="Braiding", duration_minutes=120, price_ngn=15000),
        _SeedService(number="3", name="Manicure", duration_minutes=30, price_ngn=4000),
        _SeedService(number="4", name="Facial", duration_minutes=60, price_ngn=8000),
    ),
)

ALL_SEED_BUSINESSES = (MAMAS_KITCHEN, GLOW_SALON)


def seed_businesses(db: Session) -> None:
    """
    Idempotent get-or-create for every business/menu-item/service defined
    above — safe to call on every startup. This is a one-time SEED, not a
    sync: editing a price/name above won't retroactively update an
    already-created row. Not needed for this MVP (no admin UI edits these
    yet); revisit if that changes.
    """
    for seed in ALL_SEED_BUSINESSES:
        business = db.query(models.Business).filter_by(slug=seed.slug).one_or_none()
        if business is not None:
            continue
        business = models.Business(
            slug=seed.slug,
            display_name=seed.display_name,
            vertical=seed.vertical,
            greeting_emoji=seed.greeting_emoji,
            trigger_words=seed.trigger_words,
            is_default=seed.is_default,
            review_link=seed.review_link,
            payment_link_base=seed.payment_link_base,
        )
        db.add(business)
        db.flush()
        for item in seed.menu_items:
            db.add(models.FoodMenuItem(
                business_id=business.id, number=item.number, name=item.name,
                price_ngn=item.price_ngn, category=item.category, quantity_unit=item.quantity_unit,
            ))
        for svc in seed.services:
            db.add(models.AppointmentService(
                business_id=business.id, number=svc.number, name=svc.name,
                duration_minutes=svc.duration_minutes, price_ngn=svc.price_ngn,
            ))
    db.commit()


def get_default_business(db: Session) -> models.Business:
    return db.query(models.Business).filter_by(is_default=True).one()


def get_business_by_trigger(db: Session, text: str) -> models.Business | None:
    """
    Matches text against every business's trigger_words as whole words,
    case-insensitive. Longer/more-specific matches aren't prioritized over
    shorter ones — with only two demo businesses and non-overlapping
    keywords this is fine; revisit if trigger words ever collide.
    """
    lowered = text.lower()
    for business in db.query(models.Business).all():
        words = [w.strip() for w in business.trigger_words.split(",") if w.strip()]
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
            return business
    return None


def menu_categories(db: Session, business_id: int) -> list[str]:
    """Preserves menu definition order, deduped — same contract as the old
    business_config.py-based helper conversation.py used to use directly."""
    items = db.query(models.FoodMenuItem).filter_by(business_id=business_id).order_by(models.FoodMenuItem.id).all()
    return list(dict.fromkeys(item.category for item in items))
