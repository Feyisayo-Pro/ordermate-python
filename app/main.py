"""
FastAPI application: the WhatsApp webhook + a small admin API.

Endpoints:
  GET  /webhook                       Meta's verification handshake
  POST /webhook                       Inbound WhatsApp messages
  POST /admin/orders/{ref}/delivered  Mark an order delivered -> schedules feedback request
  GET  /admin/orders                  Owner dashboard (see app/admin.py) — auth required
  GET  /health                        Liveness check
"""
from __future__ import annotations

import logging

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, Request, Response, Depends, HTTPException
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Session

from app import admin, ai, conversation
from app.config import get_settings
from app.database import get_db, init_db
from app.scheduler import start_scheduler
from app.whatsapp_client import download_media, extract_incoming_message, send_message, send_typing_indicator, verify_webhook_signature

logging.basicConfig(level=get_settings().LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ordermate.main")
settings = get_settings()

app = FastAPI(title=settings.APP_NAME)
app.include_router(admin.router)


@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()
    if not settings.whatsapp_configured:
        log.warning("WhatsApp credentials not set — running in DRY_RUN mode (outgoing messages are logged, not sent).")
    if not settings.openai_configured:
        log.warning("OPENAI_API_KEY not set — using rule-based fallback for intent/order/rating parsing.")


@app.get("/health")
def health():
    return {"status": "ok", "dry_run": not settings.whatsapp_configured}


@app.get("/webhook")
def verify_webhook(request: Request):
    """
    Meta calls this once, when you first configure the webhook URL in the
    developer dashboard, to prove you control this endpoint.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == settings.WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_webhook_signature(raw_body, signature):
        # Never process (or echo details about) a payload that failed signature
        # verification — could be spoofed.
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()
    incoming = extract_incoming_message(payload)

    if incoming is None:
        # Status update / read receipt / non-message webhook event. Meta expects
        # a fast 200 regardless, or it will retry (and eventually disable the
        # webhook) — so we always acknowledge, we just don't act on these.
        return {"status": "ignored"}

    send_typing_indicator(incoming["message_id"])

    text = incoming["text"]
    if incoming.get("media_id"):
        # Voice note: download + transcribe, then feed the result through the
        # exact same text pipeline as a typed message. If transcription isn't
        # available (OPENAI_API_KEY not set) or fails, ask the customer to
        # type instead rather than silently treating the voice note as an
        # empty message — that used to just re-show the fallback menu with
        # no acknowledgment we'd received anything at all.
        audio_bytes = download_media(incoming["media_id"])
        text = ai.transcribe_voice_note(audio_bytes) if audio_bytes else None
        if not text:
            send_message(incoming["phone"], "I can't listen to voice notes just yet 🙏 — could you type that for me instead?")
            return {"status": "received"}

    try:
        conversation.handle_incoming_message(
            db, phone=incoming["phone"], name=incoming["name"], text=text
        )
    except Exception:
        # Internal errors are logged, never leaked into the webhook response —
        # WhatsApp doesn't need (and shouldn't see) a stack trace, and Meta will
        # aggressively retry a non-200 response.
        log.exception("Error handling incoming message from %s", incoming.get("phone"))

    return {"status": "received"}


@app.post("/admin/orders/{order_ref}/delivered")
def mark_delivered(order_ref: str, db: Session = Depends(get_db)):
    """
    Simulates a delivery-partner webhook. The owner can also do this from
    their own WhatsApp by replying "delivered #<ref>" (see conversation.py::
    _handle_owner_command) — this endpoint remains for a real logistics
    partner's webhook integration, or scripted/admin use.
    """
    order = conversation.mark_delivered_and_schedule_feedback(db, order_ref)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return {"status": "delivered", "order_ref": order.order_ref, "feedback_request_in_seconds": settings.FEEDBACK_DELAY_SECONDS}
