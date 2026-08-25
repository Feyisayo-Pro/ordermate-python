import hashlib
import hmac
import logging

from app import whatsapp_client
from app.whatsapp_client import (
    verify_webhook_signature,
    extract_incoming_message,
    send_typing_indicator,
    send_list_menu,
    send_button_menu,
    download_media,
    create_message_template,
    get_message_template_status,
    send_template_message,
    ListMenu,
    ListMenuSection,
    ListMenuRow,
    ButtonMenu,
    ButtonOption,
)
from app.config import get_settings


def test_signature_verification_accepts_correct_signature():
    settings = get_settings()
    settings.WHATSAPP_APP_SECRET = "my-secret"
    try:
        body = b'{"hello": "world"}'
        sig = "sha256=" + hmac.new(b"my-secret", body, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(body, sig) is True
    finally:
        settings.WHATSAPP_APP_SECRET = ""


def test_signature_verification_rejects_tampered_body():
    settings = get_settings()
    settings.WHATSAPP_APP_SECRET = "my-secret"
    try:
        real_body = b'{"hello": "world"}'
        sig = "sha256=" + hmac.new(b"my-secret", real_body, hashlib.sha256).hexdigest()
        tampered_body = b'{"hello": "attacker"}'
        assert verify_webhook_signature(tampered_body, sig) is False
    finally:
        settings.WHATSAPP_APP_SECRET = ""


def test_signature_verification_rejects_missing_header():
    settings = get_settings()
    settings.WHATSAPP_APP_SECRET = "my-secret"
    try:
        assert verify_webhook_signature(b"anything", None) is False
    finally:
        settings.WHATSAPP_APP_SECRET = ""


def test_signature_verification_skips_when_no_secret_configured():
    # Dev-mode behavior: with no secret set yet, we can't verify anything,
    # so we allow the request through (loudly logged as a warning).
    settings = get_settings()
    assert settings.WHATSAPP_APP_SECRET == ""
    assert verify_webhook_signature(b"anything", None) is True


def test_extract_incoming_message_parses_valid_payload():
    payload = {
        "entry": [{"changes": [{"value": {
            "messages": [{"from": "234801", "id": "wamid.abc123", "text": {"body": "hi"}}],
            "contacts": [{"profile": {"name": "Tolu"}}],
        }}]}]
    }
    result = extract_incoming_message(payload)
    assert result == {"phone": "234801", "name": "Tolu", "text": "hi", "message_id": "wamid.abc123", "media_id": ""}


def test_extract_incoming_message_returns_none_for_status_update():
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    assert extract_incoming_message(payload) is None


def test_extract_incoming_message_returns_none_for_malformed_payload():
    assert extract_incoming_message({}) is None
    assert extract_incoming_message({"entry": []}) is None


def test_extract_incoming_message_normalizes_a_tapped_list_row_into_text():
    # A tapped interactive-list row is a different message shape than typed
    # text ("type": "interactive", not "text") — this normalizes its id into
    # the same "text" field so conversation.py/ai.py never need to know which
    # one happened.
    payload = {
        "entry": [{"changes": [{"value": {
            "messages": [{
                "from": "234801",
                "id": "wamid.xyz",
                "type": "interactive",
                "interactive": {"type": "list_reply", "list_reply": {"id": "Mains", "title": "Mains"}},
            }],
            "contacts": [{"profile": {"name": "Tolu"}}],
        }}]}]
    }
    result = extract_incoming_message(payload)
    assert result == {"phone": "234801", "name": "Tolu", "text": "Mains", "message_id": "wamid.xyz", "media_id": ""}


def test_extract_incoming_message_normalizes_a_tapped_button_into_text():
    # A tapped reply button is a *different* interactive sub-shape than a
    # tapped list row ("button_reply" vs "list_reply") — same normalization
    # goal, distinct payload shape worth its own regression test.
    payload = {
        "entry": [{"changes": [{"value": {
            "messages": [{
                "from": "234801",
                "id": "wamid.btn1",
                "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": "2", "title": "Track my order"}},
            }],
            "contacts": [{"profile": {"name": "Tolu"}}],
        }}]}]
    }
    result = extract_incoming_message(payload)
    assert result == {"phone": "234801", "name": "Tolu", "text": "2", "message_id": "wamid.btn1", "media_id": ""}


def test_extract_incoming_message_returns_media_id_for_voice_note():
    payload = {
        "entry": [{"changes": [{"value": {
            "messages": [{
                "from": "234801",
                "id": "wamid.voice1",
                "type": "audio",
                "audio": {"id": "media.abc123", "mime_type": "audio/ogg; codecs=opus"},
            }],
            "contacts": [{"profile": {"name": "Tolu"}}],
        }}]}]
    }
    result = extract_incoming_message(payload)
    assert result == {"phone": "234801", "name": "Tolu", "text": "", "message_id": "wamid.voice1", "media_id": "media.abc123"}


def test_send_list_menu_is_dry_run_logged_when_not_configured(caplog):
    import logging

    settings = get_settings()
    assert settings.whatsapp_configured is False
    menu = ListMenu(
        body="Pick a category:",
        button_text="View Categories",
        sections=[ListMenuSection(title="Categories", rows=[ListMenuRow(id="Mains", title="Mains")])],
    )
    with caplog.at_level(logging.INFO):
        assert send_list_menu("234801", menu) is True
    assert any("[LIST MENU]" in r.message and "Pick a category" in r.message for r in caplog.records)


def test_send_button_menu_is_dry_run_logged_when_not_configured(caplog):
    import logging

    settings = get_settings()
    assert settings.whatsapp_configured is False
    menu = ButtonMenu(
        body="What would you like to do?",
        options=[ButtonOption(id="1", title="Place an order")],
    )
    with caplog.at_level(logging.INFO):
        assert send_button_menu("234801", menu) is True
    assert any("[BUTTONS]" in r.message and "What would you like to do?" in r.message for r in caplog.records)


def test_send_typing_indicator_is_noop_in_dry_run_mode():
    settings = get_settings()
    assert settings.whatsapp_configured is False
    # No credentials configured -> no real API call is attempted, just returns False quietly
    assert send_typing_indicator("wamid.abc123") is False


def test_send_typing_indicator_is_noop_for_empty_message_id():
    settings = get_settings()
    settings.WHATSAPP_ACCESS_TOKEN = "fake-token"
    settings.WHATSAPP_PHONE_NUMBER_ID = "123456"
    try:
        assert send_typing_indicator("") is False
    finally:
        settings.WHATSAPP_ACCESS_TOKEN = ""
        settings.WHATSAPP_PHONE_NUMBER_ID = ""


def test_download_media_is_noop_in_dry_run_mode():
    settings = get_settings()
    assert settings.whatsapp_configured is False
    # No credentials configured -> no real API call is attempted
    assert download_media("media.abc123") is None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_create_message_template_returns_none_when_not_configured():
    settings = get_settings()
    assert settings.template_management_configured is False
    assert create_message_template("promo_template", "Hi there!") is None


def test_create_message_template_submits_the_correct_payload(monkeypatch):
    settings = get_settings()
    settings.WHATSAPP_ACCESS_TOKEN = "fake-token"
    settings.WHATSAPP_BUSINESS_ACCOUNT_ID = "waba-123"
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"id": "999", "status": "PENDING", "category": "MARKETING"})

    monkeypatch.setattr(whatsapp_client.httpx, "post", fake_post)
    try:
        result = create_message_template("hello_template", "Hi {{1}}, we're open!")
        assert result == {"id": "999", "status": "PENDING", "category": "MARKETING"}
        assert captured["url"] == "https://graph.facebook.com/v21.0/waba-123/message_templates"
        assert captured["json"]["name"] == "hello_template"
        assert captured["json"]["category"] == "MARKETING"
        assert captured["json"]["components"] == [{"type": "BODY", "text": "Hi {{1}}, we're open!"}]
    finally:
        settings.WHATSAPP_ACCESS_TOKEN = ""
        settings.WHATSAPP_BUSINESS_ACCOUNT_ID = ""


def test_get_message_template_status_returns_none_when_not_configured():
    settings = get_settings()
    assert settings.template_management_configured is False
    assert get_message_template_status("promo_template") is None


def test_get_message_template_status_finds_the_matching_language(monkeypatch):
    settings = get_settings()
    settings.WHATSAPP_ACCESS_TOKEN = "fake-token"
    settings.WHATSAPP_BUSINESS_ACCOUNT_ID = "waba-123"

    def fake_get(url, headers, params, timeout):
        return _FakeResponse({"data": [
            {"name": "promo_template", "language": "fr", "status": "REJECTED"},
            {"name": "promo_template", "language": "en_US", "status": "APPROVED"},
        ]})

    monkeypatch.setattr(whatsapp_client.httpx, "get", fake_get)
    try:
        assert get_message_template_status("promo_template", "en_US") == "APPROVED"
    finally:
        settings.WHATSAPP_ACCESS_TOKEN = ""
        settings.WHATSAPP_BUSINESS_ACCOUNT_ID = ""


def test_send_template_message_is_dry_run_logged_when_not_configured(caplog):
    settings = get_settings()
    assert settings.whatsapp_configured is False
    with caplog.at_level(logging.INFO):
        assert send_template_message("234801", "promo_template", "en_US", "Tolu") is True
    assert any("[TEMPLATE promo_template]" in r.message for r in caplog.records)


def test_send_template_message_omits_components_when_no_variable(monkeypatch):
    settings = get_settings()
    settings.WHATSAPP_ACCESS_TOKEN = "fake-token"
    settings.WHATSAPP_PHONE_NUMBER_ID = "123456"
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["json"] = json
        return _FakeResponse({})

    monkeypatch.setattr(whatsapp_client.httpx, "post", fake_post)
    try:
        assert send_template_message("234801", "promo_template", "en_US", None) is True
        assert "components" not in captured["json"]["template"]
    finally:
        settings.WHATSAPP_ACCESS_TOKEN = ""
        settings.WHATSAPP_PHONE_NUMBER_ID = ""


def test_send_template_message_includes_the_variable_when_given(monkeypatch):
    settings = get_settings()
    settings.WHATSAPP_ACCESS_TOKEN = "fake-token"
    settings.WHATSAPP_PHONE_NUMBER_ID = "123456"
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["json"] = json
        return _FakeResponse({})

    monkeypatch.setattr(whatsapp_client.httpx, "post", fake_post)
    try:
        assert send_template_message("234801", "promo_template", "en_US", "Tolu") is True
        assert captured["json"]["template"]["components"] == [
            {"type": "body", "parameters": [{"type": "text", "text": "Tolu"}]}
        ]
    finally:
        settings.WHATSAPP_ACCESS_TOKEN = ""
        settings.WHATSAPP_PHONE_NUMBER_ID = ""
