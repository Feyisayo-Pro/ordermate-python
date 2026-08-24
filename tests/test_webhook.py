import logging

from app.config import get_settings


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True  # no WhatsApp credentials set in tests


def test_webhook_verification_success(client):
    settings = get_settings()
    resp = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN,
        "hub.challenge": "12345",
    })
    assert resp.status_code == 200
    assert resp.text == "12345"


def test_webhook_verification_wrong_token(client):
    resp = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong-token",
        "hub.challenge": "12345",
    })
    assert resp.status_code == 403


def test_webhook_ignores_non_message_events(client):
    # e.g. a delivery/read status update — has no "messages" key
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}]}
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_webhook_rejects_invalid_signature_when_secret_configured(client):
    settings = get_settings()
    settings.WHATSAPP_APP_SECRET = "test-secret"
    try:
        resp = client.post(
            "/webhook",
            json={"entry": []},
            headers={"X-Hub-Signature-256": "sha256=not-the-real-signature"},
        )
        assert resp.status_code == 403
    finally:
        settings.WHATSAPP_APP_SECRET = ""


def test_voice_note_without_openai_configured_asks_customer_to_type_instead(client, caplog):
    # No OPENAI_API_KEY in tests -> can't transcribe. Before this feature, a
    # voice note silently became empty text and just triggered the generic
    # fallback menu with no acknowledgment we'd received anything at all.
    settings = get_settings()
    assert settings.openai_configured is False
    payload = {
        "entry": [{"changes": [{"value": {
            "messages": [{
                "from": "2348012345678",
                "id": "wamid.voice1",
                "type": "audio",
                "audio": {"id": "media.abc123", "mime_type": "audio/ogg; codecs=opus"},
            }],
            "contacts": [{"profile": {"name": "Tolu"}}],
        }}]}]
    }
    with caplog.at_level(logging.INFO):
        resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert any("can't listen to voice notes" in r.message for r in caplog.records)
