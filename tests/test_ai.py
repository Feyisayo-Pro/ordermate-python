"""
Unit tests for the rule-based fallback in app/ai.py (no OPENAI_API_KEY set in
tests, so classify_intent always exercises _classify_intent_rules).
"""
from app.ai import classify_intent


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
