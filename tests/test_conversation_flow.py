"""
End-to-end test of the full order -> delivery -> feedback -> review-request
loop, driven entirely through the public HTTP interface (exactly what Meta's
webhook would send), using the rule-based AI fallback (no Anthropic key
needed — this is what makes the test deterministic and free to run).
"""
import logging

from app import models
from app.config import get_settings
from app.conversation import _ordinal
from app.scheduler import scheduler
from tests.conftest import wait_until, whatsapp_list_reply_payload, whatsapp_payload

PHONE = "2348012345678"
NAME = "Tolu"


def _send(client, text):
    resp = client.post("/webhook", json=whatsapp_payload(PHONE, NAME, text))
    assert resp.status_code == 200
    return resp


def _send_from(client, phone, name, text):
    resp = client.post("/webhook", json=whatsapp_payload(phone, name, text))
    assert resp.status_code == 200
    return resp


def _tap(client, row_id, row_title):
    resp = client.post("/webhook", json=whatsapp_list_reply_payload(PHONE, NAME, row_id, row_title))
    assert resp.status_code == 200
    return resp


def test_full_order_flow_creates_a_confirmed_order(client, db_session):
    _send(client, "Hi! Do una still dey sell jollof rice today?")  # -> new_order intent, shows categories
    _send(client, "Mains")                                          # -> category selected, shows items
    _send(client, "1")                                              # -> item = Jollof Rice + Chicken
    _send(client, "2 plates please")                                # -> quantity = 2
    _send(client, "14 Adeyemi Street, Ikorodu")                     # -> address -> order completes

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = db_session.query(models.Order).filter_by(customer_id=customer.id).one()

    assert order.item == "Jollof Rice + Chicken"
    assert order.quantity == 2
    assert order.delivery_address == "14 Adeyemi Street, Ikorodu"
    assert order.total_ngn == 7000  # 2 x ₦3,500
    assert order.status == "Confirmed"
    assert customer.conversation.state == "idle"  # flow reset after completion


def test_starting_an_order_shows_categories_not_a_flat_item_list(client, caplog):
    with caplog.at_level(logging.INFO):
        _send(client, "order")
    assert any(
        "[LIST MENU]" in r.message and "Categories (2 options)" in r.message
        for r in caplog.records
    )


def test_selecting_a_category_shows_that_categorys_items_only(client, caplog):
    _send(client, "order")
    with caplog.at_level(logging.INFO):
        _send(client, "Drinks")
    assert any(
        "[LIST MENU]" in r.message and "Drinks (2 options)" in r.message
        for r in caplog.records
    )


def test_unmatched_category_reply_reshows_the_category_list(client, caplog):
    _send(client, "order")
    with caplog.at_level(logging.INFO):
        _send(client, "Desserts")  # not a real category
    assert any(
        "Not sure I caught that" in r.message and "Categories (2 options)" in r.message
        for r in caplog.records
    )


def test_unmatched_item_reply_reshows_that_categorys_items(client, caplog):
    _send(client, "order")
    _send(client, "Mains")
    with caplog.at_level(logging.INFO):
        _send(client, "99")  # not a valid item number
    assert any(
        "Not sure I caught that" in r.message and "Mains (3 options)" in r.message
        for r in caplog.records
    )


def test_typing_the_items_exact_name_selects_it_same_as_the_number(client, caplog):
    # The list message shows the item's name prominently — a customer typing
    # it back instead of tapping is completely natural and should still work,
    # not just the underlying row number.
    _send(client, "order")
    _send(client, "Mains")
    with caplog.at_level(logging.INFO):
        _send(client, "Ofada + Beef")
    assert any("How many plates?" in r.message for r in caplog.records)
    assert not any("Not sure I caught that" in r.message for r in caplog.records)


def test_quantity_question_matches_the_selected_items_category(client, db_session, caplog):
    # Caught live: selecting a drink still asked "How many plates?" — the
    # quantity unit needs to follow what was actually picked, not be hardcoded
    # for food. Mains -> "plates", Drinks -> "bottles" (business_config.py).
    _send(client, "order")
    _send(client, "Mains")
    with caplog.at_level(logging.INFO):
        _send(client, "1")  # Jollof Rice + Chicken
    assert any("How many plates?" in r.message for r in caplog.records)

    _send(client, "1")
    _send(client, "14 Adeyemi Street")

    _send(client, "order")
    _send(client, "Drinks")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _send(client, "4")  # Chapman
    assert any("How many bottles?" in r.message for r in caplog.records)
    assert not any("How many plates?" in r.message for r in caplog.records)


def test_cancel_mid_order_resets_to_idle(client, db_session, caplog):
    _send(client, "order")
    _send(client, "Mains")
    with caplog.at_level(logging.INFO):
        resp = _send(client, "cancel")
    assert resp.status_code == 200
    assert any("cancelled that" in r.message for r in caplog.records)

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert customer.conversation.state == "idle"
    assert customer.conversation.draft_order == "{}"


def test_cancel_also_works_as_start_over_at_any_step(client, db_session):
    # Not just right after "order" — the check runs before every turn of
    # _continue_order, so it should work regardless of which field is
    # currently being asked for (category, item, quantity, address).
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")  # item picked, now being asked for quantity
    resp = _send(client, "start over")
    assert resp.status_code == 200

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert customer.conversation.state == "idle"


def test_cancel_clears_the_abandoned_order_nudge(client, db_session):
    _send(client, "order")
    assert scheduler.get_job(f"abandoned-{db_session.query(models.Customer).filter_by(phone=PHONE).one().id}") is not None

    _send(client, "cancel")
    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert scheduler.get_job(f"abandoned-{customer.id}") is None


def test_starting_a_new_order_after_cancelling_works_cleanly(client, db_session):
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "cancel")

    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "2")
    resp = _send(client, "14 Adeyemi Street")
    assert resp.status_code == 200

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = customer.orders[0]
    assert order.item == "Jollof Rice + Chicken"
    assert order.quantity == 2
    assert customer.conversation.state == "idle"


def test_adding_a_second_item_by_naming_its_category_builds_a_real_cart(client, db_session):
    # The actual live-reported scenario this feature was built for: a customer
    # asks for a second item mid-order. Previously this got crammed into the
    # single item field and silently undercharged (see git history); now it's
    # a real second line item with its own correct pricing.
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")          # Jollof Rice + Chicken, ₦3,500
    _send(client, "1")          # qty 1 -> "Got it... What's the delivery address? (or add another)"
    _send(client, "Drinks")     # naming the category = add another item
    _send(client, "5")          # Coca-Cola, ₦500
    _send(client, "2")          # qty 2
    resp = _send(client, "14 Adeyemi Street, Ikorodu")
    assert resp.status_code == 200

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = db_session.query(models.Order).filter_by(customer_id=customer.id).one()

    assert len(order.items) == 2
    assert order.items[0].item == "Jollof Rice + Chicken"
    assert order.items[0].subtotal_ngn == 3500
    assert order.items[1].item == "Coca-Cola"
    assert order.items[1].quantity == 2
    assert order.items[1].subtotal_ngn == 1000
    assert order.total_ngn == 4500  # 3500 + (2 x 500) — not silently undercharged
    assert order.delivery_address == "14 Adeyemi Street, Ikorodu"
    assert customer.conversation.state == "idle"


def test_asking_for_a_second_item_in_natural_language_also_builds_a_cart(client, db_session):
    # Caught live, the exact phrase: "Can I also get a Coca-Cola" — should now
    # be recognized as wanting to add another item, not politely rejected.
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Can I also get a Coca-Cola")
    _send(client, "5")
    _send(client, "1")
    _send(client, "Yaba, Lagos")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = db_session.query(models.Order).filter_by(customer_id=customer.id).one()
    assert len(order.items) == 2
    assert order.total_ngn == 4000  # 3500 + 500


def test_saying_checkout_after_adding_an_item_asks_for_address_instead_of_saving_it_as_one(client, db_session, caplog):
    # Regression guard: "checkout" (or "that's all", "done", etc.) is a
    # confirmation that the customer is done adding items — it must not be
    # silently saved as the literal delivery address.
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    with caplog.at_level(logging.INFO):
        _send(client, "1")
        resp = _send(client, "checkout")
    assert resp.status_code == 200
    assert any("What's the delivery address?" in r.message for r in caplog.records)

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert len(customer.orders) == 0  # not yet completed — still waiting on the real address

    _send(client, "12 Marina Road")
    db_session.refresh(customer)
    order = customer.orders[0]
    assert order.delivery_address == "12 Marina Road"


def test_single_item_order_still_shows_the_original_confirmation_shape(client, caplog):
    # A single-item cart shouldn't read any differently than before this
    # feature existed — no "cart" language for the common case.
    with caplog.at_level(logging.INFO):
        _send(client, "order")
        _send(client, "Mains")
        _send(client, "1")
        _send(client, "1")
        _send(client, "Yaba, Lagos")
    assert any("Order #A-" in r.message and "confirmed" in r.message for r in caplog.records)


def test_saying_unsubscribe_opts_out_without_touching_conversation_state(client, db_session):
    _send(client, "order")
    _send(client, "Mains")

    resp = _send(client, "please unsubscribe")
    assert resp.status_code == 200

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert customer.opted_out is True
    # order flow untouched — opting out of promos doesn't cancel an order in progress
    assert customer.conversation.state == "ordering"


def test_unsubscribe_does_not_collide_with_mid_order_cancel(client, db_session):
    # "stop" alone still means "cancel this order" (existing behavior) —
    # only an explicit unsubscribe-shaped phrase should opt someone out.
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "stop")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert customer.conversation.state == "idle"  # cancelled, as before
    assert customer.opted_out is False  # NOT treated as an unsubscribe


def test_every_inbound_message_updates_last_inbound_at(client, db_session):
    _send(client, "hi")
    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert customer.last_inbound_at is not None


def test_ordinal_suffixes():
    assert _ordinal(1) == "1st"
    assert _ordinal(2) == "2nd"
    assert _ordinal(3) == "3rd"
    assert _ordinal(4) == "4th"
    assert _ordinal(11) == "11th"
    assert _ordinal(12) == "12th"
    assert _ordinal(13) == "13th"
    assert _ordinal(21) == "21st"
    assert _ordinal(22) == "22nd"
    assert _ordinal(23) == "23rd"
    assert _ordinal(101) == "101st"


def test_first_order_has_no_repeat_customer_note(client, caplog):
    with caplog.at_level(logging.INFO):
        _send(client, "order")
        _send(client, "Mains")
        _send(client, "1")
        _send(client, "1")
        _send(client, "14 Adeyemi Street")
    assert not any("This is your" in r.message for r in caplog.records)


def test_second_order_gets_repeat_customer_recognition(client, caplog):
    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = "2348099999999"
    try:
        _send(client, "order")
        _send(client, "Mains")
        _send(client, "1")
        _send(client, "1")
        _send(client, "14 Adeyemi Street")

        with caplog.at_level(logging.INFO):
            _send(client, "order")
            _send(client, "Mains")
            _send(client, "2")
            _send(client, "1")
            _send(client, "16 Adeyemi Street")

        assert any("This is your 2nd order" in r.message for r in caplog.records)
        assert any("repeat customer — 2nd order" in r.message for r in caplog.records)
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""


def test_tapping_list_rows_completes_an_order_same_as_typing(client, db_session):
    # A real customer taps rows rather than typing category/item names — this
    # exercises the actual "interactive" message shape end to end, not just
    # the plain-text equivalent the other flow tests use.
    _send(client, "order")
    _tap(client, "Mains", "Mains")
    _tap(client, "1", "Jollof Rice + Chicken")
    _send(client, "1")
    _send(client, "9 Allen Avenue, Ikeja")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = db_session.query(models.Order).filter_by(customer_id=customer.id).one()
    assert order.item == "Jollof Rice + Chicken"
    assert order.delivery_address == "9 Allen Avenue, Ikeja"


def test_track_order_reports_status(client, db_session):
    _send(client, "I want to order")
    _send(client, "Mains")
    _send(client, "2")   # Fried Rice + Turkey
    _send(client, "1")
    _send(client, "Lekki Phase 1")

    resp = _send(client, "where is my order")
    # can't easily assert the outgoing WhatsApp text directly (dry-run only logs it),
    # but we can assert the DB order really exists and the endpoint didn't error
    assert resp.status_code == 200
    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert len(customer.orders) == 1
    assert customer.orders[0].status == "Confirmed"


def test_high_rating_feedback_moves_conversation_back_to_idle(client, db_session):
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "3")   # Ofada + Beef
    _send(client, "1")
    _send(client, "Yaba, Lagos")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = customer.orders[0]

    # simulate delivery (FEEDBACK_DELAY_SECONDS=0 in tests via fixture)
    resp = client.post(f"/admin/orders/{order.order_ref}/delivered")
    assert resp.status_code == 200

    def _is_awaiting_feedback():
        db_session.refresh(customer.conversation)
        return customer.conversation.state == "awaiting_feedback"

    # Poll rather than a flat sleep — the scheduled job runs on a background
    # thread, and a fixed sleep guessed to be "long enough" is exactly the kind
    # of thing that flakes under load (this one has, intermittently).
    assert wait_until(_is_awaiting_feedback), "feedback-request job never fired"

    _send(client, "⭐️⭐️⭐️⭐️⭐️ amazing food!")

    db_session.refresh(customer.conversation)
    feedback = db_session.query(models.Feedback).filter_by(order_id=order.id).one()
    assert feedback.rating == 5
    assert customer.conversation.state == "idle"


def test_feedback_request_is_a_list_menu_not_plain_text(client, db_session, caplog):
    # 5 rating options > WhatsApp's 3-button cap, so this has to be a list
    # rather than reply buttons — unlike the fallback menu's 3 options.
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Yaba, Lagos")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = customer.orders[0]

    def _list_menu_logged():
        return any(
            "[LIST MENU]" in r.message and "Rating (5 options)" in r.message
            for r in caplog.records
        )

    # Poll for the actual log line, not just the state flip — send_feedback_
    # request sets state=awaiting_feedback *before* building and sending the
    # list menu, so waiting on state alone races the later send/log call
    # under load. Caught live while stress-testing this exact suite.
    with caplog.at_level(logging.INFO):
        client.post(f"/admin/orders/{order.order_ref}/delivered")
        assert wait_until(_list_menu_logged), "feedback-request list menu never logged"


def test_tapping_a_star_rating_records_it_same_as_typing(client, db_session):
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Yaba, Lagos")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = customer.orders[0]
    client.post(f"/admin/orders/{order.order_ref}/delivered")

    def _is_awaiting_feedback():
        db_session.refresh(customer.conversation)
        return customer.conversation.state == "awaiting_feedback"

    assert wait_until(_is_awaiting_feedback), "feedback-request job never fired"

    _tap(client, "4", "⭐⭐⭐⭐")

    feedback = db_session.query(models.Feedback).filter_by(order_id=order.id).one()
    assert feedback.rating == 4


def test_low_rating_does_not_request_a_review(client, db_session):
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Ikeja")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = customer.orders[0]
    client.post(f"/admin/orders/{order.order_ref}/delivered")

    def _is_awaiting_feedback():
        db_session.refresh(customer.conversation)
        return customer.conversation.state == "awaiting_feedback"

    assert wait_until(_is_awaiting_feedback), "feedback-request job never fired"

    _send(client, "Not great, food was cold")

    feedback = db_session.query(models.Feedback).filter_by(order_id=order.id).one()
    assert feedback.rating <= 2


def test_mark_delivered_on_unknown_order_returns_404(client):
    resp = client.post("/admin/orders/DOES-NOT-EXIST/delivered")
    assert resp.status_code == 404


OWNER_PHONE = "2348099999999"


def test_new_order_notifies_the_owner(client, caplog):
    # Previously missing entirely: the owner only ever heard about low
    # ratings, human-handoff requests, and the weekly digest — never that a
    # new order actually came in.
    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = OWNER_PHONE
    try:
        with caplog.at_level(logging.INFO):
            _send(client, "order")
            _send(client, "Mains")
            _send(client, "1")
            _send(client, "2")
            _send(client, "Yaba, Lagos")
        assert any(
            OWNER_PHONE in r.message and "New order" in r.message and "Jollof Rice + Chicken" in r.message
            for r in caplog.records
        )
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""


def test_owner_can_mark_an_order_delivered_from_whatsapp(client, db_session, caplog):
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Yaba, Lagos")

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    order = customer.orders[0]
    assert order.status == "Confirmed"

    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = OWNER_PHONE
    try:
        with caplog.at_level(logging.INFO):
            resp = _send_from(client, OWNER_PHONE, "Business Owner", f"delivered #{order.order_ref}")
        assert resp.status_code == 200
        assert any(f"Order #{order.order_ref} marked delivered" in r.message for r in caplog.records)
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""

    db_session.refresh(order)
    assert order.status == "Delivered"


def test_owner_delivered_command_with_unknown_ref_gets_a_clean_error(client, caplog):
    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = OWNER_PHONE
    try:
        with caplog.at_level(logging.INFO):
            resp = _send_from(client, OWNER_PHONE, "Business Owner", "delivered #A-9999")
        assert resp.status_code == 200
        assert any("Couldn't find order" in r.message for r in caplog.records)
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""


def test_owner_sending_a_normal_message_still_gets_customer_facing_handling(client, caplog):
    # The owner isn't locked out of using the bot as a customer would (e.g.
    # for their own testing) — only recognized owner commands get intercepted.
    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = OWNER_PHONE
    try:
        with caplog.at_level(logging.INFO):
            resp = _send_from(client, OWNER_PHONE, "Business Owner", "hi")
        assert resp.status_code == 200
        assert any("Welcome to Mama's Kitchen" in r.message for r in caplog.records)
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""


def test_bare_greeting_gets_a_warm_branded_fallback(client, caplog):
    # "hi" doesn't match any recognized intent, so it falls through to the same
    # fallback message shown for genuinely unrecognized input — which doubles
    # as a new customer's first impression, so it should welcome them by name.
    # 3 options fits WhatsApp's button cap, so this is native reply buttons
    # rather than a list (unlike the 5-option star rating).
    with caplog.at_level(logging.INFO):
        resp = _send(client, "hi")
    assert resp.status_code == 200
    assert any(
        "[BUTTONS]" in r.message and "Mama's Kitchen" in r.message and "Welcome" in r.message
        for r in caplog.records
    )


def test_tapping_the_fallback_menus_track_button_tracks_the_order(client, db_session):
    # Button ids are "1"/"2"/"3", the same convention a typed reply already
    # used — tapping "Track my order" should work identically to typing "2".
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Yaba, Lagos")

    resp = _tap(client, "2", "Track my order")
    assert resp.status_code == 200

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert len(customer.orders) == 1


def test_replying_two_to_the_fallback_menu_tracks_the_order_not_a_loop(client, db_session, caplog):
    # Caught live: replying "2" to the fallback menu's own "track an existing
    # order" option just re-showed the same menu instead of tracking anything.
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Yaba")  # order completes, state back to idle

    with caplog.at_level(logging.INFO):
        resp = _send(client, "2")
    assert resp.status_code == 200
    assert any("is currently: Confirmed" in r.message for r in caplog.records)
    assert not any("Welcome to Mama's Kitchen" in r.message for r in caplog.records)


def test_acknowledgment_after_order_confirmation_gets_a_warm_close_not_the_menu(client, caplog):
    # Caught live: replying "Pay on delivery" right after an order confirmed
    # triggered the cold "Welcome, place an order?" fallback instead of a
    # natural close — exactly the "I didn't understand you" feeling to avoid
    # right after something went right.
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Yaba")  # order completes, state back to idle

    with caplog.at_level(logging.INFO):
        resp = _send(client, "Pay on delivery")
    assert resp.status_code == 200
    assert any("You're welcome" in r.message for r in caplog.records)
    assert not any("Welcome to Mama's Kitchen" in r.message for r in caplog.records)


def test_human_handoff_alerts_owner_and_acknowledges_customer(client, caplog):
    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = "2348099999999"
    try:
        with caplog.at_level(logging.INFO):
            resp = _send(client, "3")  # the fallback menu's "speak to a human" option
        assert resp.status_code == 200
        assert any(
            "2348099999999" in r.message and "asked to speak to a human" in r.message
            for r in caplog.records
        )
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""


def test_human_handoff_recognizes_free_text_not_just_the_menu_number(client, caplog):
    settings = get_settings()
    settings.OWNER_WHATSAPP_NUMBER = "2348099999999"
    try:
        with caplog.at_level(logging.INFO):
            resp = _send(client, "please let me speak to a human")
        assert resp.status_code == 200
        assert any("asked to speak to a human" in r.message for r in caplog.records)
    finally:
        settings.OWNER_WHATSAPP_NUMBER = ""


def test_human_handoff_without_owner_number_still_acknowledges_customer(client, caplog):
    # OWNER_WHATSAPP_NUMBER is blank by default in tests — no one to alert, but the
    # customer should still get a reply (not silently dropped) and we should log
    # loudly server-side, same pattern as the other missing-config warnings.
    with caplog.at_level(logging.WARNING):
        resp = _send(client, "3")
    assert resp.status_code == 200
    assert any("OWNER_WHATSAPP_NUMBER" in r.message for r in caplog.records)


def test_starting_an_order_schedules_an_abandonment_nudge(client, db_session):
    _send(client, "I want to order")  # new_order intent -> shows menu, state=ordering

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert customer.conversation.state == "ordering"
    assert scheduler.get_job(f"abandoned-{customer.id}") is not None


def test_completing_an_order_cancels_the_abandonment_nudge(client, db_session):
    _send(client, "order")
    _send(client, "Mains")
    _send(client, "1")
    _send(client, "1")
    _send(client, "Ikeja")  # completes -> state back to idle

    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    assert customer.conversation.state == "idle"
    assert scheduler.get_job(f"abandoned-{customer.id}") is None


def test_abandonment_nudge_fires_when_customer_goes_quiet_mid_order(client, db_session, caplog):
    # The autouse fixture uses a 2s default (see conftest.py) so the "still pending"
    # tests above aren't racy — here we actually want the job to fire, so override
    # to near-zero just for this test.
    settings = get_settings()
    original = settings.ABANDONED_ORDER_NUDGE_SECONDS
    settings.ABANDONED_ORDER_NUDGE_SECONDS = 0
    try:
        with caplog.at_level(logging.INFO):
            _send(client, "order")  # shows menu, state=ordering
            fired = wait_until(lambda: any("Still with us?" in r.message for r in caplog.records))
    finally:
        settings.ABANDONED_ORDER_NUDGE_SECONDS = original

    assert fired
    customer = db_session.query(models.Customer).filter_by(phone=PHONE).one()
    # the nudge is a reminder, not a state change — the customer can still reply and continue
    assert customer.conversation.state == "ordering"


def test_send_abandoned_order_nudge_is_noop_if_state_already_changed(db_session, caplog):
    # Guards against the case where cancel_abandoned_order_nudge() didn't manage to
    # remove the job in time (e.g. it was already mid-execution) — the job itself
    # must re-check state rather than trusting it's still valid to fire.
    from app.conversation import send_abandoned_order_nudge

    customer = models.Customer(phone=PHONE, name=NAME)
    db_session.add(customer)
    db_session.flush()
    db_session.add(models.Conversation(customer_id=customer.id, state="idle", draft_order="{}"))
    db_session.commit()

    with caplog.at_level(logging.INFO):
        send_abandoned_order_nudge(db_session, customer.id)

    assert not any("Still with us?" in r.message for r in caplog.records)


def test_send_abandoned_order_nudge_is_noop_if_customer_replied_recently(db_session, caplog):
    # Regression test for a real bug: state=="ordering" alone isn't enough to
    # decide a nudge is warranted — if the customer replied moments ago (this
    # firing raced an in-flight reschedule), sending "still with us?" mid
    # conversation is jarring and wrong. The job must re-check elapsed time too.
    from datetime import datetime, timezone

    from app.conversation import send_abandoned_order_nudge

    settings = get_settings()
    customer = models.Customer(phone=PHONE, name=NAME)
    db_session.add(customer)
    db_session.flush()
    convo = models.Conversation(customer_id=customer.id, state="ordering", draft_order="{}")
    db_session.add(convo)
    db_session.commit()
    db_session.refresh(convo)
    assert (datetime.now(timezone.utc).replace(tzinfo=None) - convo.updated_at).total_seconds() < 1

    with caplog.at_level(logging.INFO):
        send_abandoned_order_nudge(db_session, customer.id)

    assert not any("Still with us?" in r.message for r in caplog.records)


def test_send_abandoned_order_nudge_fires_when_genuinely_quiet_long_enough(db_session, caplog):
    from datetime import datetime, timedelta, timezone

    from app.conversation import send_abandoned_order_nudge

    settings = get_settings()
    customer = models.Customer(phone=PHONE, name=NAME)
    db_session.add(customer)
    db_session.flush()
    convo = models.Conversation(customer_id=customer.id, state="ordering", draft_order="{}")
    db_session.add(convo)
    db_session.commit()
    # simulate real silence: back-date updated_at well past the nudge window
    convo.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=settings.ABANDONED_ORDER_NUDGE_SECONDS + 10
    )
    db_session.commit()

    with caplog.at_level(logging.INFO):
        send_abandoned_order_nudge(db_session, customer.id)

    assert any("Still with us?" in r.message for r in caplog.records)
