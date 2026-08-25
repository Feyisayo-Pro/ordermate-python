"""
Background scheduling for delayed feedback requests, abandoned-order
nudges, and the weekly digest.

Uses APScheduler with a SQLAlchemy-backed job store (the same database as
the rest of the app), so scheduled jobs survive a process restart or
redeploy — previously (in-memory job store) a redeploy silently dropped
every pending feedback request and abandoned-order nudge with no error or
warning, which is a real risk once order volume isn't tiny.

Consequence of a persistent job store: APScheduler pickles each job to
store it, so every scheduled function must be a plain module-level function
with picklable arguments — NOT a local closure capturing outer variables,
which is what this file used before (and can't be pickled). That's why each
schedule_* function below hands the scheduler a module-level function plus
its arguments, instead of defining the job inline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

# pyrefly: ignore [missing-import]
from apscheduler.schedulers.background import BackgroundScheduler
# pyrefly: ignore [missing-import]
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from app import database
from app.config import get_settings

log = logging.getLogger("ordermate.scheduler")
settings = get_settings()

scheduler = BackgroundScheduler()


def start_scheduler():
    if not scheduler.running:
        # Read database.engine here (call time), not at module import time —
        # tests override database.engine to point at an isolated in-memory
        # engine, and that override happens after this module is first
        # imported, so binding the job store eagerly at import time would
        # pick up the wrong engine (see tests/conftest.py).
        scheduler.configure(jobstores={"default": SQLAlchemyJobStore(engine=database.engine)})
        scheduler.start()
    _ensure_weekly_digest_job()


def _run_weekly_digest_job():
    from app.digest import send_weekly_digest  # local import avoids a circular import

    db = database.SessionLocal()
    try:
        send_weekly_digest(db)
    except Exception:
        log.exception("Failed to send weekly digest")
    finally:
        db.close()


def _ensure_weekly_digest_job():
    scheduler.add_job(
        _run_weekly_digest_job, "cron",
        day_of_week=settings.WEEKLY_DIGEST_DAY, hour=settings.WEEKLY_DIGEST_HOUR, minute=0,
        id="weekly-digest", replace_existing=True,
    )


def _run_feedback_request_job(order_id: int):
    from app.conversation import send_feedback_request  # local import avoids a circular import

    db = database.SessionLocal()
    try:
        send_feedback_request(db, order_id)
    except Exception:
        log.exception("Failed to send scheduled feedback request for order_id=%s", order_id)
    finally:
        db.close()


def schedule_feedback_request(order_id: int):
    run_date = datetime.now(timezone.utc) + timedelta(seconds=settings.FEEDBACK_DELAY_SECONDS)
    scheduler.add_job(
        _run_feedback_request_job, "date", args=[order_id], run_date=run_date,
        id=f"feedback-{order_id}", replace_existing=True,
    )


def _run_abandoned_order_nudge_job(customer_id: int):
    from app.conversation import send_abandoned_order_nudge  # local import avoids a circular import

    db = database.SessionLocal()
    try:
        send_abandoned_order_nudge(db, customer_id)
    except Exception:
        log.exception("Failed to send abandoned-order nudge for customer_id=%s", customer_id)
    finally:
        db.close()


def schedule_abandoned_order_nudge(customer_id: int):
    """
    Called on every turn a customer is still mid-order (see conversation.py).
    replace_existing=True on the customer-scoped job id means each call simply
    reschedules the same job — so a customer who keeps replying never gets
    nudged, and one who goes quiet gets nudged exactly once for that silence.
    """
    run_date = datetime.now(timezone.utc) + timedelta(seconds=settings.ABANDONED_ORDER_NUDGE_SECONDS)
    scheduler.add_job(
        _run_abandoned_order_nudge_job, "date", args=[customer_id], run_date=run_date,
        id=f"abandoned-{customer_id}", replace_existing=True,
    )


def cancel_abandoned_order_nudge(customer_id: int):
    """Best-effort: called once an order completes so a stale nudge doesn't fire.
    Not load-bearing for correctness — send_abandoned_order_nudge re-checks the
    conversation state itself — this just avoids a wasted timer firing."""
    # pyrefly: ignore [missing-import]
    from apscheduler.jobstores.base import JobLookupError

    try:
        scheduler.remove_job(f"abandoned-{customer_id}")
    except JobLookupError:
        pass
