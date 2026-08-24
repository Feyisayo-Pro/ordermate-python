"""
Unit tests for the rule-based fallback in app/ai.py (no ANTHROPIC_API_KEY set
in tests, so classify_intent always exercises _classify_intent_rules).
"""
from app import ai
from app.ai import classify_intent, parse_order_turn, transcribe_voice_note
from app.config import get_settings


def test_classify_intent_bare_three_is_human_handoff():
    # "3" is the fallback menu's "speak to a human" option. Before this intent
    # existed, a bare "3" fell through to "other" and just re-showed the same
    # menu — the exact broken loop this feature fixes.
    assert classify_intent("3") == "human_handoff"


def test_classify_intent_human_handoff_words():
    assert classify_intent("can I speak to a human please") == "human_handoff"
    assert classify_intent("let me talk to an agent") == "human_handoff"
    assert classify_intent("I need customer care") == "human_handoff"


def test_classify_intent_bare_three_does_not_fall_through_to_other():
    assert classify_intent("3") != "other"


def test_classify_intent_still_resolves_normal_intents():
    # regression: adding the human_handoff check shouldn't change existing behavior
    assert classify_intent("I want to order jollof") == "new_order"
    assert classify_intent("where is my order") == "track_order"
    assert classify_intent("please leave a review") == "feedback"


def test_classify_intent_bare_menu_numbers_match_their_promised_option():
    # Regression: the fallback menu promises "1 to order / 2 to track / 3 for
    # a human" — before this fix, only bare "3" actually worked; replying "2"
    # to the menu's own "track an existing order" option just re-showed the
    # same menu, an infinite loop caught live.
    assert classify_intent("1") == "new_order"
    assert classify_intent("2") == "track_order"
    assert classify_intent("3") == "human_handoff"


def test_classify_intent_acknowledgment_words():
    # A natural closing reply after an order confirms shouldn't be treated as
    # unrecognized input (caught live: "Pay on delivery" triggered the cold
    # "Welcome, place an order?" fallback right after a successful order).
    assert classify_intent("thanks") == "acknowledgment"
    assert classify_intent("pay on delivery") == "acknowledgment"
    assert classify_intent("ok") == "acknowledgment"
    assert classify_intent("noted") == "acknowledgment"


def test_transcribe_voice_note_returns_none_without_deepgram_configured():
    # There's no rule-based fallback for audio — without a real transcription
    # backend, the caller (main.py) needs a clear None to fall back to asking
    # the customer to type instead.
    settings = get_settings()
    assert settings.deepgram_configured is False
    assert transcribe_voice_note(b"fake audio bytes") is None


def test_parse_order_turn_claude_preserves_fields_it_does_not_manage(monkeypatch):
    # Regression: caught live — the Claude path built its returned draft from
    # scratch with only item/quantity/delivery_address, silently dropping
    # "category", "_unit_price", and "_quantity_unit" that conversation.py
    # stashes on this same dict. Since that draft *replaces* the stored one,
    # this reset a real conversation back to "pick a category" immediately
    # after the customer supplied the delivery address — the very last field
    # of the order.
    settings = get_settings()
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(
        ai,
        "_call_tool",
        lambda **kwargs: {
            "item": "Ofada + Beef",
            "quantity": 3,
            "delivery_address": "3 Awise Ogundare lane",
            "complete": True,
            "reply_text": "All set!",
        },
    )

    existing_draft = {
        "category": "Mains",
        "item": "Ofada + Beef",
        "_unit_price": 4500,
        "_quantity_unit": "plates",
        "quantity": 0,
        "delivery_address": "",
    }
    result = parse_order_turn("3 Awise Ogundare lane", existing_draft)

    assert result.draft["category"] == "Mains"
    assert result.draft["_unit_price"] == 4500
    assert result.draft["_quantity_unit"] == "plates"
    assert result.draft["delivery_address"] == "3 Awise Ogundare lane"
    assert result.complete is True
