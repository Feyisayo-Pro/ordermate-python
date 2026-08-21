"""
Unit tests for the owner weekly digest (app/digest.py) — pure aggregation +
formatting, tested directly against a db session rather than through the
webhook flow (there's no customer-facing surface to exercise here).
"""
from datetime import datetime, timedelta, timezone

from app import models
from app.config import get_settings
from app.digest import DigestStats, compute_weekly_stats, format_digest_message, send_weekly_digest

PHONE = "2348000000001"


def _make_customer(db_session, phone=PHONE):
    customer = models.Customer(phone=phone, name="Test")
    db_session.add(customer)
    db_session.flush()
    return customer


def _make_order(db_session, customer, ref, total_ngn, days_ago, status="Confirmed"):
    order = models.Order(
        order_ref=ref,
        customer_id=customer.id,
        item="Jollof Rice + Chicken",
        quantity=1,
        delivery_address="Test address",
        total_ngn=total_ngn,
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db_session.add(order)
    db_session.flush()
    return order


def _make_feedback(db_session, order, rating, days_ago):
    feedback = models.Feedback(
        order_id=order.id,
        rating=rating,
        raw_message="test",
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db_session.add(feedback)
    db_session.flush()
    return feedback


def test_compute_weekly_stats_only_counts_orders_within_the_window(db_session):
    customer = _make_customer(db_session)
    _make_order(db_session, customer, "A-1", 3500, days_ago=2)   # in window
    _make_order(db_session, customer, "A-2", 4000, days_ago=3)   # in window
    _make_order(db_session, customer, "A-3", 4500, days_ago=10)  # outside window
    db_session.commit()

    stats = compute_weekly_stats(db_session)

    assert stats.order_count == 2
    assert stats.revenue_ngn == 7500


def test_compute_weekly_stats_average_rating_and_low_rating_count(db_session):
    customer = _make_customer(db_session)
    order1 = _make_order(db_session, customer, "A-1", 3500, days_ago=1, status="Delivered")
    order2 = _make_order(db_session, customer, "A-2", 4000, days_ago=1, status="Delivered")
    order3 = _make_order(db_session, customer, "A-3", 4500, days_ago=1, status="Delivered")
    _make_feedback(db_session, order1, rating=5, days_ago=1)  # in window, not low
    _make_feedback(db_session, order2, rating=2, days_ago=1)  # in window, low
    _make_feedback(db_session, order3, rating=1, days_ago=10)  # outside window, excluded entirely
    db_session.commit()

    stats = compute_weekly_stats(db_session)

    assert stats.average_rating == 3.5  # only the two in-window ratings (5, 2)
    assert stats.low_rating_count == 1  # only the in-window "2" counts


def test_compute_weekly_stats_with_no_activity_this_week(db_session):
    stats = compute_weekly_stats(db_session)
    assert stats.order_count == 0
    assert stats.revenue_ngn == 0
    assert stats.average_rating is None
    assert stats.low_rating_count == 0


def test_format_digest_message_includes_key_numbers():
    stats = DigestStats(
        period_start=datetime(2026, 8, 10, tzinfo=timezone.utc),
        period_end=datetime(2026, 8, 17, tzinfo=timezone.utc),
        order_count=42,
        revenue_ngn=186000,
        average_rating=4.6,
        low_rating_count=2,
    )
    message = format_digest_message(stats)
    assert "42 orders" in message
    assert "₦186,000" in message
    assert "4.6" in message
    assert "2 low rating" in message


def test_format_digest_message_handles_no_ratings_and_no_low_ratings():
    stats = DigestStats(
        period_start=datetime(2026, 8, 10, tzinfo=timezone.utc),
        period_end=datetime(2026, 8, 17, tzinfo=timezone.utc),
        order_count=0,
        revenue_ngn=0,
        average_rating=None,
        low_rating_count=0,
    )
    message = format_digest_message(stats)
    assert "no ratings received" in message
    assert "No low ratings" in message


def test_send_weekly_digest_skips_when_owner_number_not_configured(db_session, caplog):
    settings = get_settings()
    assert settings.OWNER_WHATSAPP_NUMBER == ""
    with caplog.at_level("WARNING"):
        send_weekly_digest(db_session)
    assert any("OWNER_WHATSAPP_NUMBER" in r.message for r in caplog.records)


def test_send_weekly_digest_sends_when_owner_number_configured(db_session, caplog):
    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = "2348099999999"
    try:
        with caplog.at_level("INFO"):
            send_weekly_digest(db_session)
        assert any("2348099999999" in r.message and "Weekly summary" in r.message for r in caplog.records)
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""
