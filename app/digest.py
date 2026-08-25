"""
Owner-facing weekly digest.

Aggregates the last 7 days of activity into a short WhatsApp summary sent to
OWNER_WHATSAPP_NUMBER — one combined message covering every business (see
app/businesses.py), broken down per business, since this MVP round made the
admin side deliberately single-operator ("one shared login, sees everything")
rather than per-tenant. Scheduled as a recurring cron job (see
app/scheduler.py::start_scheduler); this module only computes and formats.

Kept separate from conversation.py deliberately: this is business reporting
over existing data, not per-customer state-machine logic, so it doesn't
belong in either of those modules.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# pyrefly: ignore [missing-import]
from sqlalchemy import func
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.whatsapp_client import send_message

log = logging.getLogger("ordermate.digest")
settings = get_settings()

DIGEST_WINDOW_DAYS = 7
LOW_RATING_THRESHOLD = 3  # ratings <= this are "low" — matches conversation.py's rating >= 4 review-request cutoff


@dataclass
class BusinessDigestStats:
    business_name: str
    vertical: str  # "food_ordering" | "appointment_booking" — decides wording (orders vs bookings)
    count: int
    revenue_ngn: int
    average_rating: float | None  # appointment_booking has no feedback loop yet — always None there
    low_rating_count: int


@dataclass
class DigestStats:
    period_start: datetime
    period_end: datetime
    businesses: list[BusinessDigestStats]


def _compute_business_stats(db: Session, business: models.Business, period_start: datetime, period_end: datetime) -> BusinessDigestStats:
    if business.vertical == "appointment_booking":
        count, revenue_ngn = db.query(
            func.count(models.Appointment.id), func.coalesce(func.sum(models.Appointment.price_ngn), 0)
        ).filter(
            models.Appointment.business_id == business.id,
            models.Appointment.created_at >= period_start,
            models.Appointment.created_at < period_end,
        ).one()
        return BusinessDigestStats(business.display_name, business.vertical, count, revenue_ngn, None, 0)

    count, revenue_ngn = db.query(
        func.count(models.Order.id), func.coalesce(func.sum(models.Order.total_ngn), 0)
    ).filter(
        models.Order.business_id == business.id,
        models.Order.created_at >= period_start,
        models.Order.created_at < period_end,
    ).one()

    average_rating = db.query(func.avg(models.Feedback.rating)).join(models.Order).filter(
        models.Order.business_id == business.id,
        models.Feedback.created_at >= period_start,
        models.Feedback.created_at < period_end,
    ).scalar()

    low_rating_count = db.query(func.count(models.Feedback.id)).join(models.Order).filter(
        models.Order.business_id == business.id,
        models.Feedback.created_at >= period_start,
        models.Feedback.created_at < period_end,
        models.Feedback.rating <= LOW_RATING_THRESHOLD,
    ).scalar()

    return BusinessDigestStats(
        business.display_name, business.vertical, count, revenue_ngn,
        float(average_rating) if average_rating is not None else None, low_rating_count,
    )


def compute_weekly_stats(db: Session, now: datetime | None = None) -> DigestStats:
    period_end = now or datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=DIGEST_WINDOW_DAYS)
    all_businesses = db.query(models.Business).order_by(models.Business.id).all()
    business_stats = [_compute_business_stats(db, b, period_start, period_end) for b in all_businesses]
    return DigestStats(period_start=period_start, period_end=period_end, businesses=business_stats)


def _format_business_line(stats: BusinessDigestStats) -> str:
    noun = "bookings" if stats.vertical == "appointment_booking" else "orders"
    line = f"{stats.business_name}: {stats.count} {noun} · ₦{stats.revenue_ngn:,} revenue"
    if stats.average_rating is not None:
        line += f" · {stats.average_rating:.1f}⭐"
    if stats.low_rating_count:
        line += f" · 👀 {stats.low_rating_count} low rating(s)"
    return line


def format_digest_message(stats: DigestStats) -> str:
    date_range = f"{stats.period_start:%b %d} – {stats.period_end:%b %d}"
    lines = [f"📊 Weekly summary ({date_range})"]
    lines.extend(_format_business_line(b) for b in stats.businesses)
    return "\n".join(lines)


def send_weekly_digest(db: Session) -> None:
    if not settings.OWNER_WHATSAPP_NUMBER:
        log.warning("OWNER_WHATSAPP_NUMBER not configured — skipping weekly digest")
        return
    stats = compute_weekly_stats(db)
    send_message(settings.OWNER_WHATSAPP_NUMBER, format_digest_message(stats))
