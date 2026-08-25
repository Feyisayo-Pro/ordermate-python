"""
End-to-end tests for the appointment_booking vertical (Glow Beauty Salon —
see app/businesses.py), driven entirely through the public HTTP webhook
interface, mirroring test_conversation_flow.py's style for the food_ordering
vertical. Exercises the "salon" trigger word switching a phone number to a
completely different business/flow on the same shared demo number.
"""
import logging
from datetime import datetime

from app import models
from app.config import get_settings
from tests.conftest import whatsapp_list_reply_payload, whatsapp_payload

PHONE = "2348022223333"
NAME = "Ada"


def _send(client, text, phone=PHONE, name=NAME):
    resp = client.post("/webhook", json=whatsapp_payload(phone, name, text))
    assert resp.status_code == 200
    return resp


def _tap(client, row_id, row_title, phone=PHONE, name=NAME):
    resp = client.post("/webhook", json=whatsapp_list_reply_payload(phone, name, row_id, row_title))
    assert resp.status_code == 200
    return resp


def test_saying_salon_starts_the_booking_flow_and_shows_services(client, caplog):
    with caplog.at_level(logging.INFO):
        _send(client, "salon")
    assert any(
        "[LIST MENU]" in r.message and "Services (4 options)" in r.message
        for r in caplog.records
    )


def test_full_booking_flow_creates_a_confirmed_appointment(client, db_session):
    _send(client, "salon")
    _send(client, "1")  # Haircut
    resp = _send(client, "1")  # first offered time slot
    assert resp.status_code == 200

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    appointment = db_session.query(models.Appointment).filter_by(customer_id=customer.id).one()

    assert appointment.service_name == "Haircut"
    assert appointment.price_ngn == 5000
    assert appointment.status == "Confirmed"
    assert appointment.slot_at > datetime.now()
    salon = db_session.query(models.Business).filter_by(slug="glow_salon").one()
    assert appointment.business_id == salon.id
    assert customer.conversation.state == "idle"


def test_typing_the_services_exact_name_selects_it_same_as_the_number(client, caplog):
    _send(client, "salon")
    with caplog.at_level(logging.INFO):
        _send(client, "Braiding")
    assert any("[LIST MENU]" in r.message and "Available Times" in r.message for r in caplog.records)
    assert not any("Not sure I caught that" in r.message for r in caplog.records)


def test_tapping_a_service_row_completes_a_booking_same_as_typing(client, db_session):
    _send(client, "salon")
    _tap(client, "3", "Manicure")  # tapped list row
    _tap(client, "2", "Thu Jan 01, 1:00 PM")  # tapped time slot row (title text irrelevant, id is what matches)

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    appointment = db_session.query(models.Appointment).filter_by(customer_id=customer.id).one()
    assert appointment.service_name == "Manicure"
    assert appointment.price_ngn == 4000


def test_unmatched_service_reply_reshows_the_service_list(client, caplog):
    _send(client, "salon")
    with caplog.at_level(logging.INFO):
        _send(client, "99")
    assert any("Not sure I caught that" in r.message and "Services" in r.message for r in caplog.records)


def test_unmatched_slot_reply_reshows_the_slot_list(client, caplog):
    _send(client, "salon")
    _send(client, "1")  # Haircut
    with caplog.at_level(logging.INFO):
        _send(client, "not a real time")
    assert any("Not sure I caught that" in r.message and "Available Times" in r.message for r in caplog.records)


def test_cancel_mid_booking_resets_to_idle(client, db_session):
    _send(client, "salon")
    _send(client, "1")  # Haircut
    resp = _send(client, "cancel")
    assert resp.status_code == 200

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert customer.conversation.state == "idle"
    assert customer.conversation.draft_order == "{}"


def test_track_booking_with_no_bookings_yet(client, caplog):
    _send(client, "salon")  # starts the booking flow (state="booking")
    _send(client, "cancel")  # back to idle, but still talking to Glow Salon
    with caplog.at_level(logging.INFO):
        _send(client, "track my booking")
    assert any("don't see any bookings" in r.message for r in caplog.records)


def test_track_booking_reports_status_after_booking(client, caplog):
    _send(client, "salon")
    _send(client, "1")
    _send(client, "1")
    with caplog.at_level(logging.INFO):
        _send(client, "where is my booking")
    assert any("is currently: Confirmed" in r.message and "Haircut" in r.message for r in caplog.records)


def test_new_booking_notifies_the_owner(client, caplog):
    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = "2348099999999"
    try:
        with caplog.at_level(logging.INFO):
            _send(client, "salon")
            _send(client, "1")
            _send(client, "1")
        assert any(
            "2348099999999" in r.message and "New booking" in r.message and "Haircut" in r.message
            for r in caplog.records
        )
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""


def test_switching_from_food_ordering_to_salon_mid_order_resets_the_conversation(client, db_session):
    # Same phone, already mid a food order on the default business — an
    # explicit "salon" should switch businesses and abandon that in-progress
    # order (a demo exploring a different vertical, not a real customer
    # losing a real order — see models.py's Customer docstring).
    _send(client, "order")
    _send(client, "Mains")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    mamas = db_session.query(models.Business).filter_by(slug="mamas_kitchen").one()
    assert customer.business_id == mamas.id
    assert customer.conversation.state == "ordering"

    _send(client, "salon")

    db_session.refresh(customer)
    salon = db_session.query(models.Business).filter_by(slug="glow_salon").one()
    assert customer.business_id == salon.id
    assert customer.conversation.state == "booking"


def test_a_second_message_with_no_trigger_word_still_defaults_to_the_default_business(client, db_session):
    # Regression guard for the routing design itself: a brand-new phone
    # saying something generic (no trigger word at all) must land on the
    # default business (Mama's Kitchen), not the salon — every existing
    # food-ordering test relies on this.
    _send(client, "hi")
    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    mamas = db_session.query(models.Business).filter_by(slug="mamas_kitchen").one()
    assert customer.business_id == mamas.id
