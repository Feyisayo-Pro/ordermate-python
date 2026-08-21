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
