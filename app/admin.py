"""
Minimal admin dashboard — lets the business owner view orders and update
their status from a browser, instead of a developer running curl or the
owner needing to remember a WhatsApp command syntax.

SECURITY NOTE (read this before deploying):
  - Protected by HTTP Basic Auth (ADMIN_USERNAME/ADMIN_PASSWORD in .env),
    compared with hmac.compare_digest — a constant-time comparison that
    avoids leaking how many characters matched via response timing, the same
    technique already used for the webhook signature check. The credential
    is NOT hashed: this is a single shared secret living in the same trust
    boundary as this app's other .env values, not a database of per-user
    password hashes where hashing protects against a breach. A real login
    page with per-user accounts and sessions is the natural upgrade once
    there's more than one admin — not warranted for one shared credential.
  - No session or token: the browser resends credentials on every request,
    so there's nothing to expire or invalidate.
  - This is only safe over HTTPS. Basic Auth credentials are base64-encoded,
    not encrypted, in transit — fine behind ngrok or any real deployment
    platform (both terminate HTTPS for you), never over plain HTTP.
  - If ADMIN_USERNAME/ADMIN_PASSWORD aren't set, the dashboard is fully
    disabled (503) rather than silently open — this page shows customer
    names, phone numbers, and delivery addresses.

BROADCAST SECURITY/COMPLIANCE NOTE (see the /broadcast routes below):
  - WhatsApp only allows a free-text (non-template) message within 24h of
    the customer's last inbound message — this app has no approved message
    template to fall back on, so a customer outside that window is SKIPPED
    entirely rather than attempted (an attempt would just fail against
    Meta's API, or worse, risk the business's messaging quality rating).
  - Customers who've opted out (Customer.opted_out, set by conversation.py
    when someone replies "unsubscribe" etc.) are excluded regardless of
    window — broadcast messaging is promotional, and continuing to message
    someone who opted out risks both the relationship and Meta's policies.
  - The owner's own number is always excluded, in case they've messaged the
    bot themselves for testing and ended up with a Customer row.
  - Sending is a two-step confirm (preview counts, then an explicit second
    submit) — a mass-send to potentially every customer is exactly the kind
    of hard-to-reverse, visible-to-others action worth a deliberate second
    step, not a single click.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Depends, Form, HTTPException, Request
# pyrefly: ignore [missing-import]
from fastapi.responses import HTMLResponse, RedirectResponse
# pyrefly: ignore [missing-import]
from fastapi.security import HTTPBasic, HTTPBasicCredentials
# pyrefly: ignore [missing-import]
from fastapi.templating import Jinja2Templates
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import Session

from app import conversation, models
from app.config import get_settings
from app.database import get_db
from app.whatsapp_client import send_message

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
security = HTTPBasic()

router = APIRouter(prefix="/admin", tags=["admin"])

# WhatsApp's real cutoff is 24h — checking against 23h instead leaves a
# margin so a customer right at the edge doesn't fail the send due to the
# time between preview and the owner clicking "confirm", or ordinary clock/
# request latency.
BROADCAST_WINDOW_HOURS = 23


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin dashboard not configured — set ADMIN_USERNAME/ADMIN_PASSWORD")
    valid_username = hmac.compare_digest(credentials.username, settings.ADMIN_USERNAME)
    valid_password = hmac.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
    if not (valid_username and valid_password):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


@router.get("/orders", response_class=HTMLResponse)
def orders_dashboard(request: Request, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    orders = db.query(models.Order).order_by(models.Order.created_at.desc()).limit(100).all()
    return templates.TemplateResponse(
        request, "admin_orders.html", {"orders": orders, "statuses": conversation.ORDER_STATUSES}
    )


@router.post("/orders/{order_ref}/status")
def update_status(
    order_ref: str,
    new_status: str = Form(...),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    if new_status not in conversation.ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    order = conversation.update_order_status(db, order_ref, new_status)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return RedirectResponse(url="/admin/orders", status_code=303)


BROADCAST_AUDIENCES = {
    "all": "Every customer who has messaged us",
    "customers": "Customers who have placed at least one order",
}


def _eligible_customers(db: Session, audience: str) -> list[models.Customer]:
    query = db.query(models.Customer).filter(models.Customer.opted_out.is_(False))
    if settings.OWNER_WHATSAPP_NUMBER:
        query = query.filter(models.Customer.phone != settings.OWNER_WHATSAPP_NUMBER)
    if audience == "customers":
        query = query.join(models.Order).distinct()
    return query.all()


def _split_by_window(customers: list[models.Customer]) -> tuple[list[models.Customer], list[models.Customer]]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=BROADCAST_WINDOW_HOURS)
    in_window, outside_window = [], []
    for c in customers:
        (in_window if c.last_inbound_at and c.last_inbound_at >= cutoff else outside_window).append(c)
    return in_window, outside_window


@router.get("/broadcast", response_class=HTMLResponse)
def broadcast_compose(request: Request, sent: int | None = None, failed: int | None = None, skipped: int | None = None, _admin: None = Depends(require_admin)):
    result = None
    if sent is not None:
        result = {"sent": sent, "failed": failed or 0, "skipped": skipped or 0}
    return templates.TemplateResponse(request, "admin_broadcast.html", {"audiences": BROADCAST_AUDIENCES, "result": result})


@router.post("/broadcast/preview", response_class=HTMLResponse)
def broadcast_preview(
    request: Request,
    message: str = Form(...),
    audience: str = Form(...),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    if audience not in BROADCAST_AUDIENCES:
        raise HTTPException(status_code=400, detail="Invalid audience")
    if not message.strip():
        raise HTTPException(status_code=400, detail="Message can't be empty")
    in_window, outside_window = _split_by_window(_eligible_customers(db, audience))
    return templates.TemplateResponse(
        request, "admin_broadcast_preview.html",
        {
            "message": message,
            "audience": audience,
            "audience_label": BROADCAST_AUDIENCES[audience],
            "in_window_count": len(in_window),
            "outside_window_count": len(outside_window),
            "window_hours": BROADCAST_WINDOW_HOURS,
        },
    )


@router.post("/broadcast/send")
def broadcast_send(
    message: str = Form(...),
    audience: str = Form(...),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    if audience not in BROADCAST_AUDIENCES:
        raise HTTPException(status_code=400, detail="Invalid audience")
    in_window, outside_window = _split_by_window(_eligible_customers(db, audience))
    sent = sum(1 for c in in_window if send_message(c.phone, message))
    failed = len(in_window) - sent
    return RedirectResponse(
        url=f"/admin/broadcast?sent={sent}&failed={failed}&skipped={len(outside_window)}",
        status_code=303,
    )
