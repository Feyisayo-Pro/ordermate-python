"""
AI layer: intent classification, order-detail extraction, rating extraction.

Every function here has two paths:
  1. OpenAI-backed (used when OPENAI_API_KEY is set) — structured JSON output
     via response_format={"type": "json_object"}.
  2. A rule-based fallback (used when no key is set) — good enough to run the
     full demo conversation deterministically, at zero cost, with no external
     calls. This is what lets you develop and test the whole app before you've
     signed up for anything.

Both paths return the same shape, so callers (conversation.py) never need to
know which one ran.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.business_config import ACTIVE_BUSINESS
from app.config import get_settings

log = logging.getLogger("ordermate.ai")
settings = get_settings()

_client = None


def _get_openai_client():
    global _client
    if _client is None:
        # pyrefly: ignore [missing-import]
        from openai import OpenAI  # imported lazily so the package is optional in dry-run/dev
        _client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

VALID_INTENTS = {"new_order", "track_order", "feedback", "human_handoff", "acknowledgment", "other"}


def classify_intent(text: str) -> str:
    if settings.openai_configured:
        try:
            return _classify_intent_openai(text)
        except Exception:
            log.exception("OpenAI intent classification failed — falling back to rules")
    return _classify_intent_rules(text)


def _classify_intent_openai(text: str) -> str:
    client = _get_openai_client()
    resp = client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You classify inbound WhatsApp messages for a small business bot. "
                    'Reply with STRICT JSON only: {"intent": one of '
                    '["new_order","track_order","feedback","human_handoff","acknowledgment","other"]}. '
                    "'feedback' = replying with a rating/star/opinion about a past order. "
                    "'track_order' = asking where their order is, OR the bare number 2 "
                    "(the fallback menu's 'track an existing order' option). "
                    "'new_order' = wants to buy/order something, OR the bare number 1 "
                    "(the fallback menu's 'place an order' option). "
                    "'human_handoff' = explicitly asking for a person/agent/customer care, "
                    "or the bare number 3 (the fallback menu's 'speak to a human' option). "
                    "'acknowledgment' = a short closing reply after an order was just confirmed "
                    "(e.g. 'thanks', 'ok', 'pay on delivery', 'noted', 👍) that isn't asking for "
                    "anything new."
                ),
            },
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    intent = data.get("intent", "other")
    return intent if intent in VALID_INTENTS else "other"


_ORDER_WORDS = re.compile(r"\b(order|buy|want|get me|jollof|rice|dress|available|dey sell)\b", re.I)
_TRACK_WORDS = re.compile(r"\b(track|where|status|my order)\b", re.I)
_FEEDBACK_WORDS = re.compile(r"(★|⭐|\brate\b|\brating\b|\breview\b)", re.I)
_HUMAN_WORDS = re.compile(r"\b(human|agent|representative|customer care|customer service|real person)\b", re.I)
_ACK_WORDS = re.compile(r"\b(thanks|thank you|thx|ok|okay|k|done|noted|cool|alright|got it|pay on delivery|pod|sure)\b|👍|🙏", re.I)

# The fallback menu (_fallback_message in conversation.py) explicitly promises
# "1️⃣ to place an order / 2️⃣ to track an existing order / 3️⃣ to speak to a
# human" — these map bare digit replies to that exact promise. Checked first
# since a lone digit wouldn't match any of the word lists below anyway, but
# doing it explicitly keeps this correct regardless of future word additions.
_MENU_NUMBER_INTENTS = {
    "1": "new_order", "1️⃣": "new_order",
    "2": "track_order", "2️⃣": "track_order",
    "3": "human_handoff", "3️⃣": "human_handoff",
}


def _classify_intent_rules(text: str) -> str:
    # Note: this only matters when a customer volunteers a rating without us
    # having asked (conversation.py's "unsolicited feedback" path) — the
    # normal flow bypasses classification entirely while state is already
    # "awaiting_feedback". This heuristic is intentionally coarse; the
    # OpenAI-backed classifier (used automatically once OPENAI_API_KEY is
    # set) is what you want for reliable free-form understanding.
    stripped = text.strip()
    if stripped in _MENU_NUMBER_INTENTS:
        return _MENU_NUMBER_INTENTS[stripped]
    if _HUMAN_WORDS.search(text):
        return "human_handoff"
    if _FEEDBACK_WORDS.search(text):
        return "feedback"
    if _TRACK_WORDS.search(text):
        return "track_order"
    if _ORDER_WORDS.search(text):
        return "new_order"
    if _ACK_WORDS.search(text):
        return "acknowledgment"
    return "other"


# ---------------------------------------------------------------------------
# Order parsing (incremental — fills in a partial draft over multiple turns)
# ---------------------------------------------------------------------------

@dataclass
class OrderParseResult:
    draft: dict
    complete: bool
    reply_text: str


def parse_order_turn(message: str, draft: dict) -> OrderParseResult:
    """
    Given the customer's latest message and whatever's already in the draft
    order, extract more fields and decide what to ask next (or confirm if
    the order is now complete).
    """
    if settings.openai_configured:
        try:
            return _parse_order_turn_openai(message, draft)
        except Exception:
            log.exception("OpenAI order parsing failed — falling back to rules")
    return _parse_order_turn_rules(message, draft)


def _parse_order_turn_openai(message: str, draft: dict) -> OrderParseResult:
    client = _get_openai_client()
    menu_text = "\n".join(f"{m.number}. {m.name} — ₦{m.price_ngn}" for m in ACTIVE_BUSINESS.menu)
    resp = client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are the ordering assistant for {ACTIVE_BUSINESS.display_name}. Menu:\n{menu_text}\n\n"
                    "Given the current order draft (JSON) and the customer's latest message, update the draft "
                    'and reply with STRICT JSON: {"item": "", "quantity": 0, "delivery_address": "", '
                    '"complete": false, "reply_text": ""}. '
                    "complete=true only once item, quantity>0, and delivery_address are all filled. "
                    "reply_text is a short, warm WhatsApp reply — either the next question needed, or an "
                    "order confirmation summary if complete."
                ),
            },
            {"role": "user", "content": f"Current draft: {json.dumps(draft)}\nCustomer message: {message}"},
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    new_draft = {
        "item": data.get("item", draft.get("item", "")),
        "quantity": data.get("quantity", draft.get("quantity", 0)),
        "delivery_address": data.get("delivery_address", draft.get("delivery_address", "")),
    }
    return OrderParseResult(draft=new_draft, complete=bool(data.get("complete")), reply_text=data.get("reply_text", ""))


_QTY_RE = re.compile(r"(\d+)\s*(plate|piece|pcs|x)?", re.I)


def _parse_order_turn_rules(message: str, draft: dict) -> OrderParseResult:
    """
    Deterministic fallback: walks the same three fields (item, quantity,
    address) ONE PER TURN. Good enough to run the scripted demo conversation
    without any API key.

    Important: each branch returns immediately after filling a field (except
    the last one, which falls through to completion). Without that early
    return, a message like "1" would satisfy both the item-number check AND
    the quantity regex in the same turn, silently skipping the quantity
    question — that was a real bug caught by the test suite before this fix.
    """
    draft = dict(draft)
    menu_by_number = {m.number: m for m in ACTIVE_BUSINESS.menu}

    if not draft.get("item"):
        chosen = menu_by_number.get(message.strip())
        if chosen:
            draft["item"] = chosen.name
            draft["_unit_price"] = chosen.price_ngn
            return OrderParseResult(draft=draft, complete=False, reply_text="Great choice 😋 How many plates?")
        menu_lines = "\n".join(f"{m.number}️⃣ {m.name} — ₦{m.price_ngn}" for m in ACTIVE_BUSINESS.menu)
        return OrderParseResult(draft=draft, complete=False,
                                 reply_text=f"What would you like? Here's today's menu:\n{menu_lines}\n\nReply with a number.")

    if not draft.get("quantity"):
        m = _QTY_RE.search(message)
        qty = int(m.group(1)) if m else None
        if qty:
            draft["quantity"] = qty
            return OrderParseResult(draft=draft, complete=False, reply_text="What's the delivery address?")
        unit = draft.get("_quantity_unit", "plates")
        return OrderParseResult(draft=draft, complete=False, reply_text=f"How many {unit}?")

    if not draft.get("delivery_address"):
        if len(message.strip()) >= 4 and not message.strip().isdigit():
            draft["delivery_address"] = message.strip()
            # falls through to completion below — this is the field that
            # finishes the order, so we don't ask another question first
        else:
            return OrderParseResult(draft=draft, complete=False, reply_text="What's the delivery address?")

    total = draft.get("_unit_price", 0) * draft.get("quantity", 1)
    reply = (
        f"Got it — {draft['quantity']}x {draft['item']} = ₦{total:,}\n\n"
        f"Delivering to: {draft['delivery_address']}"
    )
    return OrderParseResult(draft=draft, complete=True, reply_text=reply)


# ---------------------------------------------------------------------------
# Rating extraction
# ---------------------------------------------------------------------------

_STAR_RE = re.compile(r"([1-5])\s*(?:★|star|stars|⭐)?")
_EMOJI_STAR_RE = re.compile(r"⭐️?")


def parse_rating(message: str) -> int:
    if settings.openai_configured:
        try:
            return _parse_rating_openai(message)
        except Exception:
            log.exception("OpenAI rating parsing failed — falling back to rules")
    return _parse_rating_rules(message)


def _parse_rating_openai(message: str) -> int:
    client = _get_openai_client()
    resp = client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": 'Extract a 1-5 star rating from the message. Reply STRICT JSON: {"rating": 1-5}. If unclear, estimate from sentiment.'},
            {"role": "user", "content": message},
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    rating = int(data.get("rating", 3))
    return max(1, min(5, rating))


def _parse_rating_rules(message: str) -> int:
    emoji_count = len(_EMOJI_STAR_RE.findall(message))
    if emoji_count:
        return max(1, min(5, emoji_count))
    m = _STAR_RE.search(message)
    if m:
        return int(m.group(1))

    lowered = message.lower()
    # Negative phrases are checked FIRST and deliberately include the negated
    # forms ("not great", "not good") explicitly — a naive substring check
    # for positive words alone would match "great" inside "not great" and
    # score a complaint as 5 stars. This is exactly the kind of nuance the
    # OpenAI-backed path (used automatically once OPENAI_API_KEY is set)
    # handles properly; this fallback only needs to be reliable for the
    # scripted demo phrases, not arbitrary customer language.
    negative_words = ["not great", "not good", "not amazing", "bad", "poor", "disappointed", "terrible", "awful", "cold", "hated"]
    if any(w in lowered for w in negative_words):
        return 2
    positive_words = ["great", "love", "amazing", "fit so well", "excellent", "perfect", "fantastic"]
    if any(w in lowered for w in positive_words):
        return 5
    return 3


# ---------------------------------------------------------------------------
# Voice note transcription
# ---------------------------------------------------------------------------
#
# Unlike everything above, this has no rule-based fallback — there's no
# meaningful deterministic way to "parse" arbitrary audio. It's fully gated
# behind OPENAI_API_KEY; the caller (app/main.py) asks the customer to type
# instead when it's not configured or transcription fails.
#
# SECURITY / PRIVACY NOTE: the raw voice note audio is sent to OpenAI's
# Whisper API for transcription. This only happens when OPENAI_API_KEY is
# explicitly configured — in the default zero-cost/rule-based deployment
# mode, no customer audio ever leaves this server.


def transcribe_voice_note(audio_bytes: bytes) -> str | None:
    if not settings.openai_configured:
        return None
    try:
        client = _get_openai_client()
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=("voice_note.ogg", audio_bytes, "audio/ogg"),
        )
        return resp.text
    except Exception:
        log.exception("Voice note transcription failed")
        return None
