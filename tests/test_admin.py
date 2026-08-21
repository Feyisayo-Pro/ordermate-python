"""
Tests for the admin dashboard (app/admin.py) — auth enforcement and the
order-status update flow, driven through the real HTTP endpoints.
"""
import pytest

from app import models
from app.config import get_settings

ADMIN_USER = "owner"
ADMIN_PASS = "test-admin-password"


@pytest.fixture
def admin_auth():
    """Configures ADMIN_USERNAME/ADMIN_PASSWORD for the duration of a test."""
    settings = get_settings()
    original = (settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD)
    settings.ADMIN_USERNAME = ADMIN_USER
    settings.ADMIN_PASSWORD = ADMIN_PASS
    yield (ADMIN_USER, ADMIN_PASS)
    settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD = original


def _make_order(db_session, order_ref="A-1111", status="Confirmed"):
    customer = models.Customer(phone="2348012345678", name="Tolu")
    db_session.add(customer)
    db_session.flush()
    db_session.add(models.Conversation(customer_id=customer.id, state="idle", draft_order="{}"))
    order = models.Order(
        order_ref=order_ref,
        customer_id=customer.id,
        item="Jollof Rice + Chicken",
        quantity=2,
        delivery_address="14 Adeyemi Street",
        total_ngn=7000,
        status=status,
    )
    db_session.add(order)
    db_session.commit()
    return order


def test_dashboard_is_disabled_when_admin_credentials_not_configured(client):
    # FastAPI's HTTPBasic dependency itself 401s before our code runs when NO
    # credentials are sent at all — sending *some* (unconfigured-side) creds
    # is what's needed to actually exercise our own "not configured" check.
    settings = get_settings()
    assert settings.ADMIN_USERNAME == ""
    resp = client.get("/admin/orders", auth=("anyone", "anything"))
    assert resp.status_code == 503


def test_dashboard_requires_credentials(client, admin_auth):
    resp = client.get("/admin/orders")
    assert resp.status_code == 401


def test_dashboard_rejects_wrong_credentials(client, admin_auth):
    resp = client.get("/admin/orders", auth=(ADMIN_USER, "wrong-password"))
    assert resp.status_code == 401


def test_dashboard_shows_orders_with_correct_credentials(client, admin_auth, db_session):
    _make_order(db_session)
    resp = client.get("/admin/orders", auth=admin_auth)
    assert resp.status_code == 200
    assert "A-1111" in resp.text
    assert "Jollof Rice + Chicken" in resp.text
    assert "14 Adeyemi Street" in resp.text


def test_dashboard_shows_empty_state_with_no_orders(client, admin_auth):
    resp = client.get("/admin/orders", auth=admin_auth)
    assert resp.status_code == 200
    assert "No orders yet" in resp.text


def test_updating_status_to_delivered_schedules_feedback(client, admin_auth, db_session):
    order = _make_order(db_session)
    resp = client.post(
        f"/admin/orders/{order.order_ref}/status",
        data={"new_status": "Delivered"},
        auth=admin_auth,
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db_session.refresh(order)
    assert order.status == "Delivered"
    assert order.delivered_at is not None


def test_updating_status_to_preparing_is_a_plain_update(client, admin_auth, db_session):
    order = _make_order(db_session)
    resp = client.post(
        f"/admin/orders/{order.order_ref}/status",
        data={"new_status": "Preparing"},
        auth=admin_auth,
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db_session.refresh(order)
    assert order.status == "Preparing"
    assert order.delivered_at is None


def test_updating_status_rejects_unrecognized_value(client, admin_auth, db_session):
    order = _make_order(db_session)
    resp = client.post(
        f"/admin/orders/{order.order_ref}/status",
        data={"new_status": "Teleported"},
        auth=admin_auth,
    )
    assert resp.status_code == 400


def test_updating_status_for_unknown_order_returns_404(client, admin_auth):
    resp = client.post(
        "/admin/orders/DOES-NOT-EXIST/status",
        data={"new_status": "Preparing"},
        auth=admin_auth,
    )
    assert resp.status_code == 404


def test_status_update_requires_admin_credentials(client, admin_auth, db_session):
    order = _make_order(db_session)
    resp = client.post(f"/admin/orders/{order.order_ref}/status", data={"new_status": "Preparing"})
    assert resp.status_code == 401
