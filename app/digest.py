"""
Owner-facing weekly digest.

Aggregates the last 7 days of orders and feedback into a short WhatsApp summary
sent to OWNER_WHATSAPP_NUMBER — order volume, revenue, average rating, and how
many low ratings still need a follow-up. Scheduled as a recurring cron job (see
app/scheduler.py::start_scheduler); this module only computes and formats.

Kept separate from conversation.py deliberately: this is business reporting over
existing data, not per-customer state-machine logic, so it doesn't belong in
either of those modules.
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
from app.business_config import ACTIVE_BUSINESS
from app.config import get_settings
from app.whatsapp_client import send_message

log = logging.getLogger("ordermate.digest")
settings = get_settings()

DIGEST_WINDOW_DAYS = 7
LOW_RATING_THRESHOLD = 3  # ratings <= this are "low" — matches conversation.py's rating >= 4 review-request cutoff


@dataclass
class DigestStats:
    period_start: datetime
    period_end: datetime
    order_count: int
    revenue_ngn: int
    average_rating: float | None
    low_rating_count: int


def compute_weekly_stats(db: Session, now: datetime | None = None) -> DigestStats:
    period_end = now or datetime.now(timezone.utc)
    period_start = period_end - timedelta(days=DIGEST_WINDOW_DAYS)

    order_count, revenue_ngn = db.query(
        func.count(models.Order.id), func.coalesce(func.sum(models.Order.total_ngn), 0)
    ).filter(models.Order.created_at >= period_start, models.Order.created_at < period_end).one()

    average_rating = db.query(func.avg(models.Feedback.rating)).filter(
        models.Feedback.created_at >= period_start, models.Feedback.created_at < period_end
    ).scalar()

    low_rating_count = db.query(func.count(models.Feedback.id)).filter(
        models.Feedback.created_at >= period_start,
        models.Feedback.created_at < period_end,
        models.Feedback.rating <= LOW_RATING_THRESHOLD,
    ).scalar()

    return DigestStats(
        period_start=period_start,
        period_end=period_end,
        order_count=order_count,
        revenue_ngn=revenue_ngn,
        average_rating=float(average_rating) if average_rating is not None else None,
        low_rating_count=low_rating_count,
    )


def format_digest_message(stats: DigestStats) -> str:
    date_range = f"{stats.period_start:%b %d} – {stats.period_end:%b %d}"
    rating_line = (
        f"Average rating: {stats.average_rating:.1f}⭐"
        if stats.average_rating is not None
        else "Average rating: no ratings received this week"
    )
    follow_up_line = (
        f"👀 {stats.low_rating_count} low rating(s) may need a follow-up"
        if stats.low_rating_count
        else "🎉 No low ratings this week"
    )
    return (
        f"📊 Weekly summary for {ACTIVE_BUSINESS.display_name} ({date_range})\n"
        f"{stats.order_count} orders · ₦{stats.revenue_ngn:,} revenue\n"
        f"{rating_line}\n"
        f"{follow_up_line}"
    )


def send_weekly_digest(db: Session) -> None:
    if not settings.OWNER_WHATSAPP_NUMBER:
        log.warning("OWNER_WHATSAPP_NUMBER not configured — skipping weekly digest")
        return
    stats = compute_weekly_stats(db)
    send_message(settings.OWNER_WHATSAPP_NUMBER, format_digest_message(stats))
