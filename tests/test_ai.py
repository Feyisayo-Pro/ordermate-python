"""
Unit tests for the rule-based fallback in app/ai.py (no ANTHROPIC_API_KEY set
in tests, so classify_intent always exercises _classify_intent_rules).
"""
from app import ai
from app.ai import classify_cart_continuation, classify_intent, parse_quantity, transcribe_voice_note
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


def test_parse_quantity_rules_extracts_a_digit():
    assert parse_quantity("2 plates please", "plates") == 2


def test_parse_quantity_rules_extracts_a_number_word():
    assert parse_quantity("just one", "plates") == 1
    assert parse_quantity("two please", "bottles") == 2


def test_parse_quantity_rules_returns_none_when_unclear():
    assert parse_quantity("hmm not sure", "plates") is None


def test_parse_quantity_claude_only_reads_the_quantity_field(monkeypatch):
    # Scoped narrowly on purpose: quantity extraction used to be bundled
    # together with delivery_address AND (further back) the item itself in
    # one combined Claude call — that's exactly what let Claude "helpfully"
    # rewrite fields it had no business touching (see git history for the
    # multi-item corruption bug this caused). Each field gets its own call now.
    settings = get_settings()
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(ai, "_call_tool", lambda **kwargs: {"quantity": 3, "found": True})
    assert parse_quantity("three please", "plates") == 3


def test_parse_quantity_claude_returns_none_when_not_found(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(ai, "_call_tool", lambda **kwargs: {"quantity": 0, "found": False})
    assert parse_quantity("what's on the menu again?", "plates") is None


def test_classify_cart_continuation_rules_recognizes_a_category_name():
    # Regression scenario, now a supported feature rather than rejected:
    # caught live, a customer asked for a second item ("Drinks") mid-order —
    # this used to get crammed into the (single) item field, silently
    # undercharging. Real carts now support exactly this.
    decision = classify_cart_continuation("Drinks", ["Mains", "Drinks"])
    assert decision.wants_more is True
    assert decision.category == "Drinks"


def test_classify_cart_continuation_rules_recognizes_add_more_phrasing():
    decision = classify_cart_continuation("Can I also get a Coca-Cola", ["Mains", "Drinks"])
    assert decision.wants_more is True


def test_classify_cart_continuation_rules_treats_an_address_as_not_wanting_more():
    decision = classify_cart_continuation("14 Adeyemi Street, Ikorodu", ["Mains", "Drinks"])
    assert decision.wants_more is False
    assert decision.category is None
    assert decision.has_address is True


def test_classify_cart_continuation_rules_does_not_treat_checkout_as_an_address():
    # Regression guard: "checkout"/"that's all" confirms the customer is done
    # adding items, but it is NOT itself a delivery address — must not be
    # silently accepted as one.
    for phrase in ["checkout", "that's all", "that's all thanks", "done", "no more", "nope"]:
        decision = classify_cart_continuation(phrase, ["Mains", "Drinks"])
        assert decision.wants_more is False
        assert decision.has_address is False, f"{phrase!r} should not be treated as an address"


def test_classify_cart_continuation_rules_allows_addresses_starting_with_no():
    # A real Nigerian address commonly starts with "No" ("No 5 Adeyemi
    # Close") — must not be mistaken for a bare "no" confirmation.
    decision = classify_cart_continuation("No 5 Adeyemi Close, Ikeja", ["Mains", "Drinks"])
    assert decision.wants_more is False
    assert decision.has_address is True


def test_classify_cart_continuation_claude_ignores_a_hallucinated_category(monkeypatch):
    # Defensive: even if Claude names a category that doesn't exist on this
    # business's menu, the caller must never end up with a category to look
    # items up against that isn't real.
    settings = get_settings()
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(
        ai, "_call_tool",
        lambda **kwargs: {"wants_more": True, "category": "Desserts", "has_address": False},
    )
    decision = classify_cart_continuation("something sweet too", ["Mains", "Drinks"])
    assert decision.wants_more is True
    assert decision.category is None
