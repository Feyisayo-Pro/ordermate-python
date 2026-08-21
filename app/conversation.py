"""
Conversation orchestration — the piece that decides what happens for each
inbound message.

Design: a tiny explicit state machine (idle / ordering / awaiting_feedback)
stored per-customer in the Conversation table, delegating the "what did they
actually say" understanding to app.ai. Explicit state means the bot never
gets confused about whether a bare number like "2" means "quantity: 2" or
"rating: 2 stars" — that ambiguity is exactly why free-form AI-only parsing
without state gets unreliable fast.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone

# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Session

from app import ai, models
from app.business_config import ACTIVE_BUSINESS
from app.config import get_settings
from app.scheduler import cancel_abandoned_order_nudge, schedule_abandoned_order_nudge, schedule_feedback_request
from app.whatsapp_client import ListMenu, ListMenuRow, ListMenuSection, send_list_menu, send_message

log = logging.getLogger("ordermate.conversation")
settings = get_settings()


def _get_or_create_customer(db: Session, phone: str, name: str) -> models.Customer:
    customer = db.query(models.Customer).filter_by(phone=phone).one_or_none()
    if customer is None:
        customer = models.Customer(phone=phone, name=name or "")
        db.add(customer)
        db.flush()
        db.add(models.Conversation(customer_id=customer.id, state="idle", draft_order="{}"))
        db.flush()
    elif name and not customer.name:
        customer.name = name
    return customer


def _new_order_ref() -> str:
    return f"A-{random.randint(1000, 9999)}"


def handle_incoming_message(db: Session, phone: str, name: str, text: str) -> list[str]:
    """
    Main entry point. Returns the list of reply texts sent to the customer
    (useful for tests / logging — the actual sending already happened via
    whatsapp_client.send_message by the time this returns).
    """
    if settings.OWNER_WHATSAPP_NUMBER and phone == settings.OWNER_WHATSAPP_NUMBER:
        owner_reply = _handle_owner_command(db, text)
        if owner_reply is not None:
            db.commit()
            send_message(phone, owner_reply)
            return [owner_reply]
        # Not a recognized owner command (e.g. the owner is just testing the
        # bot themselves) — fall through to normal customer-facing handling.

    customer = _get_or_create_customer(db, phone, name)
    convo = customer.conversation
    replies: list[str] = []

    if convo.state == "awaiting_feedback":
        replies = _handle_feedback(db, customer, convo, text)
    elif convo.state == "ordering":
        replies = _continue_order(db, customer, convo, text)
    else:
        intent = ai.classify_intent(text)
        if intent == "new_order":
            convo.state = "ordering"
            convo.draft_order = "{}"
            replies = _continue_order(db, customer, convo, text, first_turn=True)
        elif intent == "track_order":
            replies = _handle_track_order(db, customer)
        elif intent == "feedback":
            # customer volunteered a rating without us asking — still handle it
            replies = _handle_feedback(db, customer, convo, text, unsolicited=True)
        elif intent == "human_handoff":
            replies = _handle_human_handoff(customer)
        elif intent == "acknowledgment":
            # A closing reply ("thanks", "pay on delivery", 👍) after an order
            # just confirmed shouldn't re-show the full menu — that reads as
            # "I didn't understand you" right after things went right.
            replies = [_acknowledgment_message(customer)]
        else:
            replies = [_fallback_message(customer)]

    # Commit before sending: this durably persists state changes (including
    # whatever nudge scheduling just happened) before the outbound network
    # call, rather than after — keeps the abandoned-order nudge's debounce
    # window tight regardless of how long the WhatsApp API call takes.
    db.commit()

    if replies and settings.TYPING_DELAY_SECONDS:
        time.sleep(settings.TYPING_DELAY_SECONDS)

    for r in replies:
        if isinstance(r, ListMenu):
            send_list_menu(phone, r)
        else:
            send_message(phone, r)
    return replies


def _menu_categories() -> list[str]:
    return list(dict.fromkeys(item.category for item in ACTIVE_BUSINESS.menu))  # preserves order, dedups


def _category_list_menu(body: str) -> ListMenu:
    rows = [ListMenuRow(id=c, title=c) for c in _menu_categories()]
    return ListMenu(body=body, button_text="View Categories", sections=[ListMenuSection(title="Categories", rows=rows)])


def _item_list_menu(body: str, category: str) -> ListMenu:
    rows = [
        ListMenuRow(id=m.number, title=m.name, description=f"₦{m.price_ngn:,}")
        for m in ACTIVE_BUSINESS.menu if m.category == category
    ]
    return ListMenu(body=body, button_text="Select Item", sections=[ListMenuSection(title=category, rows=rows)])


def _continue_order(db: Session, customer: models.Customer, convo: models.Conversation, text: str, first_turn: bool = False) -> list:
    draft = json.loads(convo.draft_order or "{}")
    greeting = f"Hey {customer.name}! 👋 " if customer.name else "Hey! 👋 "

    if first_turn:
        # First message just triggered the "new_order" intent (e.g. "do you
        # sell jollof today?") — greet + show categories rather than treating
        # that message itself as a selection.
        categories = _menu_categories()
        schedule_abandoned_order_nudge(customer.id)
        if len(categories) <= 1:
            # Nothing to categorize — skip straight to the item list, same as
            # the old single-tier menu behavior.
            only_category = categories[0] if categories else ""
            convo.draft_order = json.dumps({"category": only_category})
            return [_item_list_menu(f"{greeting}Here's today's menu:", only_category)]
        convo.draft_order = "{}"
        return [_category_list_menu(f"{greeting}Here's today's menu — pick a category:")]

    if not draft.get("category"):
        # Case-insensitive: matches both a tapped list row (id == category
        # name) and a customer typing the category name directly.
        chosen = next((c for c in _menu_categories() if c.lower() == text.strip().lower()), None)
        schedule_abandoned_order_nudge(customer.id)
        if not chosen:
            return [_category_list_menu("Not sure I caught that — pick a category:")]
        draft["category"] = chosen
        convo.draft_order = json.dumps(draft)
        return [_item_list_menu(f"Great, here's what's in {chosen}:", chosen)]

    if not draft.get("item"):
        items_in_category = {m.number: m for m in ACTIVE_BUSINESS.menu if m.category == draft["category"]}
        chosen_item = items_in_category.get(text.strip())
        schedule_abandoned_order_nudge(customer.id)
        if not chosen_item:
            return [_item_list_menu(f"Not sure I caught that — here's {draft['category']} again:", draft["category"])]
        draft["item"] = chosen_item.name
        draft["_unit_price"] = chosen_item.price_ngn
        draft["_quantity_unit"] = chosen_item.quantity_unit
        convo.draft_order = json.dumps(draft)
        return [f"Great choice 😋 How many {chosen_item.quantity_unit}?"]

    result = ai.parse_order_turn(text, draft)
    convo.draft_order = json.dumps(result.draft)

    if not result.complete:
        schedule_abandoned_order_nudge(customer.id)
        return [result.reply_text]

    # Order complete -> persist it, confirm, reset state
    unit_price = result.draft.get("_unit_price", 0)
    quantity = int(result.draft.get("quantity", 1))
    order = models.Order(
        order_ref=_new_order_ref(),
        customer_id=customer.id,
        item=result.draft.get("item", ""),
        quantity=quantity,
        delivery_address=result.draft.get("delivery_address", ""),
        total_ngn=unit_price * quantity,
        status="Confirmed",
    )
    db.add(order)
    db.flush()

    convo.state = "idle"
    convo.draft_order = "{}"
    cancel_abandoned_order_nudge(customer.id)

    # The owner needs to know a new order came in so prep can start — this
    # was missing entirely before: the owner only ever heard about low
    # ratings, human-handoff requests, and the weekly digest.
    if settings.OWNER_WHATSAPP_NUMBER:
        owner_alert = (
            f"🆕 New order #{order.order_ref}!\n"
            f"{quantity}x {order.item} — ₦{order.total_ngn:,}\n"
            f"From: {customer.name or customer.phone} ({customer.phone})\n"
            f"Delivering to: {order.delivery_address}"
        )
        send_message(settings.OWNER_WHATSAPP_NUMBER, owner_alert)

    payment_link = f"{ACTIVE_BUSINESS.payment_link_base}{order.order_ref.split('-')[-1]}"
    confirmation = (
        f"All set{', ' + customer.name if customer.name else ''}! 🎉\n"
        f"Order #{order.order_ref} confirmed — ₦{order.total_ngn:,}\n"
        f"Delivering to: {order.delivery_address}\n\n"
        f"Pay on delivery or tap to pay now: {payment_link}"
    )
    return [result.reply_text, confirmation] if result.reply_text and result.reply_text not in confirmation else [confirmation]


_DELIVERED_COMMAND_RE = re.compile(r"^delivered\s+#?([A-Za-z]+-?\d+)$", re.I)


def _handle_owner_command(db: Session, text: str) -> str | None:
    """
    Lets the business owner mark an order delivered by replying from their own
    WhatsApp number — before this, the only way to do it was a developer
    running curl against the admin API directly, which doesn't work for a
    non-technical owner running this day to day.

    Returns None if the message isn't a recognized owner command, so the
    caller falls through to normal customer-facing handling (the owner can
    still use the bot as a customer would, e.g. for their own testing).
    """
    match = _DELIVERED_COMMAND_RE.match(text.strip())
    if not match:
        return None
    order_ref = match.group(1).upper()
    order = mark_delivered_and_schedule_feedback(db, order_ref)
    if not order:
        return f"❌ Couldn't find order #{order_ref} — check the reference and try again."
    return f"✅ Order #{order.order_ref} marked delivered. Feedback request scheduled."


def _handle_track_order(db: Session, customer: models.Customer) -> list[str]:
    order = (
        db.query(models.Order)
        .filter_by(customer_id=customer.id)
        .order_by(models.Order.created_at.desc())
        .first()
    )
    if not order:
        return ["I don't see any orders on your account yet — want to place one? Just say 'order' 🙂"]
    return [f"Your order #{order.order_ref} is currently: {order.status} 📦"]


def _handle_feedback(db: Session, customer: models.Customer, convo: models.Conversation, text: str, unsolicited: bool = False) -> list[str]:
    order = (
        db.query(models.Order)
        .filter_by(customer_id=customer.id, status="Delivered")
        .order_by(models.Order.delivered_at.desc())
        .first()
    )
    if not order:
        return [] if unsolicited else [_fallback_message(customer)]

    rating = ai.parse_rating(text)
    feedback = models.Feedback(order_id=order.id, rating=rating, raw_message=text)
    db.add(feedback)
    convo.state = "idle"

    if rating >= 4:
        return [f"That makes our day! 🥹 Would you drop a quick review? {ACTIVE_BUSINESS.review_link}"]

    # Low rating: don't just thank them — alert the owner so they can follow up
    # before it turns into a public bad review.
    if settings.OWNER_WHATSAPP_NUMBER:
        alert = (
            f"⚠️ Low rating alert: {customer.name or customer.phone} ({customer.phone}) "
            f"gave order #{order.order_ref} {rating}★. Follow up before it becomes a public review."
        )
        send_message(settings.OWNER_WHATSAPP_NUMBER, alert)
    return ["Thanks for being honest with us 🙏 Someone from our team will reach out shortly to make this right."]


def _handle_human_handoff(customer: models.Customer) -> list[str]:
    if settings.OWNER_WHATSAPP_NUMBER:
        alert = f"🙋 {customer.name or customer.phone} ({customer.phone}) asked to speak to a human."
        send_message(settings.OWNER_WHATSAPP_NUMBER, alert)
    else:
        log.warning("Customer %s asked for a human but OWNER_WHATSAPP_NUMBER isn't configured", customer.phone)
    return ["Got it 🙋 Someone from our team will reach out to you shortly."]


def _acknowledgment_message(customer: models.Customer) -> str:
    name = f" {customer.name}" if customer.name else ""
    return f"You're welcome{name}! 😊 We'll take it from here."


def _fallback_message(customer: models.Customer) -> str:
    # Also the response to a bare greeting ("hi") — anything that doesn't match
    # a recognized intent lands here, so it doubles as the first impression for
    # a brand-new customer. Worth being warm and branded, not just functional.
    name = f" {customer.name}" if customer.name else ""
    return (
        f"Hey{name}! {ACTIVE_BUSINESS.greeting_emoji} Welcome to {ACTIVE_BUSINESS.display_name}. Reply:\n"
        f"1️⃣ to place an order\n"
        f"2️⃣ to track an existing order\n"
        f"3️⃣ to speak to a human"
    )


def mark_order_delivered(db: Session, order_ref: str) -> models.Order | None:
    """
    Called by the admin endpoint (simulating a delivery-partner webhook, or a
    manual "mark delivered" action). Flips the order status and puts that
    customer's conversation into awaiting_feedback — the actual feedback
    *request* message is scheduled separately (see app/scheduler.py) so it
    goes out after a delay rather than the instant delivery is marked.
    """
    order = db.query(models.Order).filter_by(order_ref=order_ref).one_or_none()
    if not order:
        return None
    order.status = "Delivered"
    order.delivered_at = datetime.now(timezone.utc)
    db.commit()
    return order


def mark_delivered_and_schedule_feedback(db: Session, order_ref: str) -> models.Order | None:
    """Shared by the admin HTTP endpoint and the owner's WhatsApp "delivered"
    command — one place that does both steps so they can't drift apart."""
    order = mark_order_delivered(db, order_ref)
    if order:
        schedule_feedback_request(order.id)
    return order


ORDER_STATUSES = ["Confirmed", "Preparing", "Out for Delivery", "Delivered", "Cancelled"]


def update_order_status(db: Session, order_ref: str, new_status: str) -> models.Order | None:
    """
    General status update for the admin dashboard (app/admin.py). "Delivered"
    is special — it also stamps delivered_at and schedules the feedback
    request, same as mark_delivered_and_schedule_feedback; every other status
    is a plain update with no side effects.
    """
    if new_status == "Delivered":
        return mark_delivered_and_schedule_feedback(db, order_ref)
    order = db.query(models.Order).filter_by(order_ref=order_ref).one_or_none()
    if not order:
        return None
    order.status = new_status
    db.commit()
    return order


def send_feedback_request(db: Session, order_id: int) -> None:
    order = db.get(models.Order, order_id)
    if not order or order.status != "Delivered":
        return
    customer = order.customer
    convo = customer.conversation
    convo.state = "awaiting_feedback"
    db.commit()

    name = f" {customer.name}" if customer.name else ""
    message = f"Hey{name} 🙌 Hope you loved your order! How would you rate it — 1 to 5 stars?"
    send_message(customer.phone, message)


# Absorbs the small, unavoidable gap between scheduling a nudge and the DB
# commit that finalizes Conversation.updated_at — not a tuning knob, just
# slack for normal processing overhead (see send_abandoned_order_nudge).
_NUDGE_RACE_TOLERANCE_SECONDS = 1


def send_abandoned_order_nudge(db: Session, customer_id: int) -> None:
    """
    Fired by the scheduled job in app/scheduler.py. Re-checks both the
    conversation state AND how long it's actually been quiet before sending.

    The state check alone isn't sufficient: every reply while ordering
    reschedules this job (replace_existing=True), which usually means a
    customer who keeps replying never gets nudged — but if a reply arrives
    right as an old job's timer is about to fire, the reschedule can lose
    that race and the nudge fires mid-conversation instead of after real
    silence. Re-validating elapsed time here (using Conversation.updated_at,
    which changes on every turn) closes that race regardless of *why* the
    reschedule was late, rather than just tightening the window and hoping.
    """
    customer = db.get(models.Customer, customer_id)
    if not customer or customer.conversation.state != "ordering":
        return

    convo = customer.conversation
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    quiet_for = (now - convo.updated_at).total_seconds()
    if quiet_for < settings.ABANDONED_ORDER_NUDGE_SECONDS - _NUDGE_RACE_TOLERANCE_SECONDS:
        return  # customer was active more recently than this job's timer assumed — stale firing

    name = f" {customer.name}" if customer.name else ""
    message = f"Hey{name} 👋 Still with us? Want to finish your order — just reply and we'll pick up where we left off 🙂"
    send_message(customer.phone, message)
