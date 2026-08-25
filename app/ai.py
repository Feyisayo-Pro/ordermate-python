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
# Quantity parsing — how many of the item the customer already picked
# ---------------------------------------------------------------------------

_QTY_RE = re.compile(r"(\d+)\s*(plate|piece|pcs|bottle|x)?", re.I)
_QTY_WORDS = {
    "a": 1, "an": 1, "single": 1, "one": 1, "two": 2, "couple": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def parse_quantity(message: str, quantity_unit: str) -> int | None:
    """
    Extracts how many of the already-chosen item the customer wants. Scoped
    to exactly this one field — quantity used to be extracted together with
    delivery_address in a single combined call, which is what let Claude
    also "helpfully" try to change the item itself (see git history). Each
    field now has its own narrow, single-purpose extraction step.
    """
    if settings.anthropic_configured:
        try:
            return _parse_quantity_claude(message, quantity_unit)
        except Exception:
            log.exception("Claude quantity parsing failed — falling back to rules")
    return _parse_quantity_rules(message)


def _parse_quantity_claude(message: str, quantity_unit: str) -> int | None:
    data = _call_tool(
        system=(
            f"The customer was asked how many {quantity_unit} they want of an item they already "
            "chose. Extract that quantity as an integer. If the message doesn't contain a usable "
            "quantity (e.g. it's off-topic), set found=false."
        ),
        user_content=message,
        tool_name="extract_quantity",
        description="Record the extracted quantity.",
        input_schema={
            "type": "object",
            "properties": {
                "quantity": {"type": "integer"},
                "found": {"type": "boolean"},
            },
            "required": ["quantity", "found"],
        },
    )
    if not data.get("found"):
        return None
    qty = int(data.get("quantity", 0))
    return qty if qty > 0 else None


def _parse_quantity_rules(message: str) -> int | None:
    m = _QTY_RE.search(message)
    if m:
        return int(m.group(1))
    lowered = message.lower()
    for word, n in _QTY_WORDS.items():
        if re.search(rf"\b{word}\b", lowered):
            return n
    return None


# ---------------------------------------------------------------------------
# Cart continuation — after adding an item, is the customer adding another
# one, giving their delivery address, or just confirming they're done (which
# is NOT itself an address — "checkout" must never get saved as one)?
# ---------------------------------------------------------------------------

_ADD_MORE_WORDS = re.compile(
    r"\b(add|another item|one more|something else|also (get|want|need|order)|"
    r"can i (also )?(get|have|add)|i (also )?(want|need))\b", re.I
)

# Deliberately word/phrase-based rather than a single bare "no" — a real
# address can start with "No" ("No 5 Adeyemi Close", common Nigerian address
# format), so a bare negation can't be treated as "no address given" here.
_CONFIRM_DONE_RE = re.compile(
    r"\b(checkout|check ?out|that'?s all|thats all|that'?s it|thats it|"
    r"done|no more|nothing else|no thanks|nope)\b", re.I
)


@dataclass
class CartContinuation:
    wants_more: bool
    category: str | None
    has_address: bool  # only meaningful when wants_more is False


def classify_cart_continuation(message: str, categories: list[str]) -> CartContinuation:
    """
    Decides what to do with the customer's message sent right after an item
    was added to the cart: add another item, treat this message as their
    delivery address, or (has_address=False) they're confirming they're done
    without actually having given an address yet — "checkout"/"that's all"
    must prompt for the real address rather than being saved as one.
    """
    if settings.anthropic_configured:
        try:
            return _classify_cart_continuation_claude(message, categories)
        except Exception:
            log.exception("Claude cart-continuation classification failed — falling back to rules")
    return _classify_cart_continuation_rules(message, categories)


def _classify_cart_continuation_claude(message: str, categories: list[str]) -> CartContinuation:
    data = _call_tool(
        system=(
            "The customer just added an item to their cart and was asked whether they'd like to "
            f"add another item, or (if not) to give their delivery address. Categories available: "
            f"{', '.join(categories)}. Decide: is this message asking to add another item "
            "(wants_more=true)? If wants_more=true and the message mentions a category or item by "
            "name, set category to that category's name exactly as given above; otherwise leave "
            "category empty. If wants_more=false, decide has_address: true only if this message "
            "itself actually contains a real delivery address or location — a plain confirmation "
            "like 'checkout', 'that's all', or 'done' is NOT an address, so has_address must be "
            "false for those (the customer will be asked for the real address next)."
        ),
        user_content=message,
        tool_name="classify_cart_continuation",
        description="Decide whether the customer wants to add another item, is giving their address, or is just confirming they're done.",
        input_schema={
            "type": "object",
            "properties": {
                "wants_more": {"type": "boolean"},
                "category": {"type": "string"},
                "has_address": {"type": "boolean"},
            },
            "required": ["wants_more", "category", "has_address"],
        },
    )
    category = data.get("category") or None
    if category not in categories:
        category = None
    return CartContinuation(
        wants_more=bool(data.get("wants_more")),
        category=category,
        has_address=bool(data.get("has_address")),
    )


def _classify_cart_continuation_rules(message: str, categories: list[str]) -> CartContinuation:
    stripped = message.strip()
    matched = next((c for c in categories if c.lower() == stripped.lower()), None)
    if matched:
        return CartContinuation(True, matched, False)
    if _ADD_MORE_WORDS.search(message):
        lowered = message.lower()
        mentioned_category = next((c for c in categories if c.lower() in lowered), None)
        if mentioned_category:
            return CartContinuation(True, mentioned_category, False)
        # No category named directly, but the message might name an item
        # instead ("can I also get a Coca-Cola") — resolve that to its
        # category so the customer isn't asked to pick a category they
        # already effectively answered.
        mentioned_item_category = next(
            (m.category for m in ACTIVE_BUSINESS.menu if m.name.lower() in lowered), None
        )
        return CartContinuation(True, mentioned_item_category, False)
    if _CONFIRM_DONE_RE.search(message):
        return CartContinuation(False, None, False)
    return CartContinuation(False, None, True)


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
