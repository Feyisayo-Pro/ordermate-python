"""
Background scheduling for delayed feedback requests.

Uses APScheduler with an in-memory job store. Good enough for a single-
instance MVP; the honest limitation (documented so you don't get surprised
by it later) is that scheduled jobs are lost if the process restarts before
they fire. For a pilot with one business and modest order volume that's an
acceptable trade — if you outgrow it, swap in APScheduler's SQLAlchemy job
store (a few lines) or a proper task queue (Celery/RQ + Redis) so jobs
survive restarts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app import database
from app.config import get_settings

log = logging.getLogger("ordermate.scheduler")
settings = get_settings()

scheduler = BackgroundScheduler()


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
    _ensure_weekly_digest_job()


def _ensure_weekly_digest_job():
    from app.digest import send_weekly_digest

    def _job():
        db = database.SessionLocal()
        try:
            send_weekly_digest(db)
        except Exception:
            log.exception("Failed to send weekly digest")
        finally:
            db.close()

    scheduler.add_job(
        _job, "cron",
        day_of_week=settings.WEEKLY_DIGEST_DAY, hour=settings.WEEKLY_DIGEST_HOUR, minute=0,
        id="weekly-digest", replace_existing=True,
    )


def schedule_feedback_request(order_id: int):
    from app.conversation import send_feedback_request  # local import avoids a circular import

    def _job():
        # Resolved via the module attribute (not a name imported at module
        # load time) so tests can point app.database.SessionLocal at an
        # isolated test database and have this background job pick it up —
        # `from app.database import SessionLocal` would have frozen the
        # reference to whatever engine was active at import time instead.
        db = database.SessionLocal()
        try:
            send_feedback_request(db, order_id)
        except Exception:
            log.exception("Failed to send scheduled feedback request for order_id=%s", order_id)
        finally:
            db.close()

    run_date = datetime.now(timezone.utc) + timedelta(seconds=settings.FEEDBACK_DELAY_SECONDS)
    scheduler.add_job(_job, "date", run_date=run_date, id=f"feedback-{order_id}", replace_existing=True)


def schedule_abandoned_order_nudge(customer_id: int):
    """
    Called on every turn a customer is still mid-order (see conversation.py).
    replace_existing=True on the customer-scoped job id means each call simply
    reschedules the same job — so a customer who keeps replying never gets
    nudged, and one who goes quiet gets nudged exactly once for that silence.
    """
    from app.conversation import send_abandoned_order_nudge  # local import avoids a circular import

    def _job():
        db = database.SessionLocal()
        try:
            send_abandoned_order_nudge(db, customer_id)
        except Exception:
            log.exception("Failed to send abandoned-order nudge for customer_id=%s", customer_id)
        finally:
            db.close()

    run_date = datetime.now(timezone.utc) + timedelta(seconds=settings.ABANDONED_ORDER_NUDGE_SECONDS)
    scheduler.add_job(_job, "date", run_date=run_date, id=f"abandoned-{customer_id}", replace_existing=True)


def cancel_abandoned_order_nudge(customer_id: int):
    """Best-effort: called once an order completes so a stale nudge doesn't fire.
    Not load-bearing for correctness — send_abandoned_order_nudge re-checks the
    conversation state itself — this just avoids a wasted timer firing."""
    from apscheduler.jobstores.base import JobLookupError

    try:
        scheduler.remove_job(f"abandoned-{customer_id}")
    except JobLookupError:
        pass
