"""
AI layer: intent classification, order-detail extraction, rating extraction,
voice note transcription.

Every text-understanding function here has two paths:
  1. Claude-backed (used when ANTHROPIC_API_KEY is set) — structured output
     via forced tool use, Anthropic's equivalent of OpenAI's JSON mode.
  2. A rule-based fallback (used when no key is set) — good enough to run the
     full demo conversation deterministically, at zero cost, with no external
     calls. This is what lets you develop and test the whole app before you've
     signed up for anything.

Both paths return the same shape, so callers (conversation.py) never need to
know which one ran. Voice transcription is the one exception — there's no
meaningful rule-based fallback for arbitrary audio, so it's gated behind
DEEPGRAM_API_KEY alone with no fallback path; see its own section below.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

# pyrefly: ignore [missing-import]
import httpx

from app.business_config import ACTIVE_BUSINESS
from app.config import get_settings

log = logging.getLogger("ordermate.ai")
settings = get_settings()

_client = None


def _get_anthropic_client():
    global _client
    if _client is None:
        # pyrefly: ignore [missing-import]
        from anthropic import Anthropic  # imported lazily so the package is optional in dry-run/dev
        _client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _call_tool(system: str, user_content: str, tool_name: str, description: str, input_schema: dict) -> dict:
    """
    Shared helper for the "ask Claude for one structured object back" pattern
    used by all three text-understanding functions below. Forcing tool_choice
    to this one tool is Anthropic's equivalent of OpenAI's
    response_format={"type": "json_object"} — it guarantees the response is
    exactly this shape rather than free-form text we'd have to parse.
    """
    client = _get_anthropic_client()
    resp = client.messages.create(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        tools=[{"name": tool_name, "description": description, "input_schema": input_schema}],
        tool_choice={"type": "tool", "name": tool_name},
    )
    tool_use = next(block for block in resp.content if block.type == "tool_use")
    return tool_use.input


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

VALID_INTENTS = {"new_order", "track_order", "feedback", "human_handoff", "acknowledgment", "other"}


def classify_intent(text: str) -> str:
    if settings.anthropic_configured:
        try:
            return _classify_intent_claude(text)
        except Exception:
            log.exception("Claude intent classification failed — falling back to rules")
    return _classify_intent_rules(text)


def _classify_intent_claude(text: str) -> str:
    data = _call_tool(
        system=(
            "You classify inbound WhatsApp messages for a small business bot. "
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
        user_content=text,
        tool_name="classify_intent",
        description="Record the classified intent of the customer's message.",
        input_schema={
            "type": "object",
            "properties": {"intent": {"type": "string", "enum": sorted(VALID_INTENTS)}},
            "required": ["intent"],
        },
    )
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
    # Claude-backed classifier (used automatically once ANTHROPIC_API_KEY is
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
    if settings.anthropic_configured:
        try:
            return _parse_order_turn_claude(message, draft)
        except Exception:
            log.exception("Claude order parsing failed — falling back to rules")
    return _parse_order_turn_rules(message, draft)


def _parse_order_turn_claude(message: str, draft: dict) -> OrderParseResult:
    menu_text = "\n".join(f"{m.number}. {m.name} — ₦{m.price_ngn}" for m in ACTIVE_BUSINESS.menu)
    data = _call_tool(
        system=(
            f"You are the ordering assistant for {ACTIVE_BUSINESS.display_name}. Menu:\n{menu_text}\n\n"
            "Given the current order draft and the customer's latest message, update the draft. "
            "complete=true only once item, quantity>0, and delivery_address are all filled. "
            "reply_text is a short, warm WhatsApp reply asking for whichever field is still missing. "
            "Once complete=true, set reply_text to an empty string — a separate, fully-formatted "
            "confirmation message (with the total price and payment link) is always sent "
            "automatically right after; do not write your own confirmation summary, it would "
            "arrive as a duplicate, second message."
        ),
        user_content=f"Current draft: {json.dumps(draft)}\nCustomer message: {message}",
        tool_name="update_order_draft",
        description="Record the updated order draft and the reply to send the customer.",
        input_schema={
            "type": "object",
            "properties": {
                "item": {"type": "string"},
                "quantity": {"type": "integer"},
                "delivery_address": {"type": "string"},
                "complete": {"type": "boolean"},
                "reply_text": {"type": "string"},
            },
            "required": ["item", "quantity", "delivery_address", "complete", "reply_text"],
        },
    )
    # Start from the FULL existing draft, not just the three fields Claude
    # manages — conversation.py stashes "category", "_unit_price", and
    # "_quantity_unit" on this same dict before ever delegating here,  and a
    # draft built from scratch would silently drop them, resetting the
    # customer back to "pick a category" on their very next reply. Caught
    # live: this exact bug reset a real conversation right after the address
    # was captured, the very last field of the order.
    new_draft = dict(draft)
    new_draft["item"] = data.get("item", draft.get("item", ""))
    new_draft["quantity"] = data.get("quantity", draft.get("quantity", 0))
    new_draft["delivery_address"] = data.get("delivery_address", draft.get("delivery_address", ""))
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
    if settings.anthropic_configured:
        try:
            return _parse_rating_claude(message)
        except Exception:
            log.exception("Claude rating parsing failed — falling back to rules")
    return _parse_rating_rules(message)


def _parse_rating_claude(message: str) -> int:
    data = _call_tool(
        system="Extract a 1-5 star rating from the message. If unclear, estimate from sentiment.",
        user_content=message,
        tool_name="extract_rating",
        description="Record the extracted star rating.",
        input_schema={
            "type": "object",
            "properties": {"rating": {"type": "integer", "minimum": 1, "maximum": 5}},
            "required": ["rating"],
        },
    )
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
    # Claude-backed path (used automatically once ANTHROPIC_API_KEY is set)
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
# meaningful deterministic way to "parse" arbitrary audio, and Anthropic
# doesn't offer a speech-to-text API, so this runs on Deepgram instead of
# Claude. Fully gated behind DEEPGRAM_API_KEY; the caller (app/main.py) asks
# the customer to type instead when it's not configured or transcription
# fails.
#
# SECURITY / PRIVACY NOTE: the raw voice note audio is sent to Deepgram for
# transcription. This only happens when DEEPGRAM_API_KEY is explicitly
# configured — in the default zero-cost/rule-based deployment mode, no
# customer audio ever leaves this server.

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"


def transcribe_voice_note(audio_bytes: bytes) -> str | None:
    if not settings.deepgram_configured:
        return None
    try:
        resp = httpx.post(
            DEEPGRAM_LISTEN_URL,
            params={"model": "nova-2", "smart_format": "true"},
            headers={
                "Authorization": f"Token {settings.DEEPGRAM_API_KEY}",
                "Content-Type": "audio/ogg",
            },
            content=audio_bytes,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (httpx.HTTPError, KeyError, IndexError):
        log.exception("Voice note transcription failed")
        return None
