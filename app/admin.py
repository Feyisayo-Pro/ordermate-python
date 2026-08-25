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
import re
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
from app.whatsapp_client import create_message_template, get_message_template_status, send_message, send_template_message

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


@router.get("/appointments", response_class=HTMLResponse)
def appointments_dashboard(request: Request, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    appointments = db.query(models.Appointment).order_by(models.Appointment.created_at.desc()).limit(100).all()
    return templates.TemplateResponse(request, "admin_appointments.html", {"appointments": appointments})


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


def _eligible_customers(db: Session, business: models.Business, audience: str) -> list[models.Customer]:
    # Scoped to ONE business — a customer currently exploring the salon demo
    # shouldn't get a "Mama's Kitchen is open" broadcast, and vice versa (see
    # models.py's Customer docstring on business_id being "who they're
    # talking to right now," not a permanent identity).
    query = db.query(models.Customer).filter(
        models.Customer.business_id == business.id, models.Customer.opted_out.is_(False),
    )
    if settings.OWNER_WHATSAPP_NUMBER:
        query = query.filter(models.Customer.phone != settings.OWNER_WHATSAPP_NUMBER)
    if audience == "customers":
        # "Has transacted at least once" — which table that means depends on
        # the business's vertical (Order for food_ordering, Appointment for
        # appointment_booking).
        join_model = models.Appointment if business.vertical == "appointment_booking" else models.Order
        query = query.join(join_model).distinct()
    return query.all()


def _split_by_window(customers: list[models.Customer]) -> tuple[list[models.Customer], list[models.Customer]]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=BROADCAST_WINDOW_HOURS)
    in_window, outside_window = [], []
    for c in customers:
        (in_window if c.last_inbound_at and c.last_inbound_at >= cutoff else outside_window).append(c)
    return in_window, outside_window


def _approved_templates(db: Session, business_id: int) -> list[models.MessageTemplate]:
    return db.query(models.MessageTemplate).filter_by(status="APPROVED", business_id=business_id).order_by(models.MessageTemplate.name).all()


def _get_business_or_400(db: Session, business_id: int) -> models.Business:
    business = db.get(models.Business, business_id)
    if not business:
        raise HTTPException(status_code=400, detail="Invalid business")
    return business


@router.get("/broadcast", response_class=HTMLResponse)
def broadcast_compose(
    request: Request,
    sent: int | None = None, failed: int | None = None, skipped: int | None = None,
    db: Session = Depends(get_db), _admin: None = Depends(require_admin),
):
    result = None
    if sent is not None:
        result = {"sent": sent, "failed": failed or 0, "skipped": skipped or 0}
    all_businesses = db.query(models.Business).order_by(models.Business.id).all()
    return templates.TemplateResponse(
        request, "admin_broadcast.html",
        {
            "audiences": BROADCAST_AUDIENCES, "result": result, "businesses": all_businesses,
            "approved_templates_by_business": {b.id: _approved_templates(db, b.id) for b in all_businesses},
        },
    )


@router.post("/broadcast/preview", response_class=HTMLResponse)
def broadcast_preview(
    request: Request,
    business_id: int = Form(...),
    method: str = Form(...),  # "text" or "template:<id>"
    message: str = Form(""),
    audience: str = Form(...),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    if audience not in BROADCAST_AUDIENCES:
        raise HTTPException(status_code=400, detail="Invalid audience")
    business = _get_business_or_400(db, business_id)
    eligible = _eligible_customers(db, business, audience)

    if method == "text":
        if not message.strip():
            raise HTTPException(status_code=400, detail="Message can't be empty")
        in_window, outside_window = _split_by_window(eligible)
        return templates.TemplateResponse(
            request, "admin_broadcast_preview.html",
            {
                "business_id": business.id, "business_name": business.display_name,
                "method": "text", "message": message, "audience": audience,
                "audience_label": BROADCAST_AUDIENCES[audience],
                "in_window_count": len(in_window), "outside_window_count": len(outside_window),
                "window_hours": BROADCAST_WINDOW_HOURS,
            },
        )

    template_id = method.removeprefix("template:")
    tpl = db.get(models.MessageTemplate, int(template_id)) if template_id.isdigit() else None
    if not tpl or tpl.status != "APPROVED" or tpl.business_id != business.id:
        raise HTTPException(status_code=400, detail="Template not found or not approved")
    # Template messages aren't limited by the 24h window at all — that's the
    # whole point of using one — so every eligible (opted-in, non-owner)
    # customer will actually receive it.
    return templates.TemplateResponse(
        request, "admin_broadcast_preview.html",
        {
            "business_id": business.id, "business_name": business.display_name,
            "method": f"template:{tpl.id}", "message": tpl.body_text, "audience": audience,
            "audience_label": BROADCAST_AUDIENCES[audience],
            "in_window_count": len(eligible), "outside_window_count": 0,
            "window_hours": BROADCAST_WINDOW_HOURS, "template_name": tpl.name,
        },
    )


@router.post("/broadcast/send")
def broadcast_send(
    business_id: int = Form(...),
    method: str = Form(...),
    message: str = Form(""),
    audience: str = Form(...),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    if audience not in BROADCAST_AUDIENCES:
        raise HTTPException(status_code=400, detail="Invalid audience")
    business = _get_business_or_400(db, business_id)
    eligible = _eligible_customers(db, business, audience)

    if method == "text":
        in_window, outside_window = _split_by_window(eligible)
        sent = sum(1 for c in in_window if send_message(c.phone, message))
        failed = len(in_window) - sent
        skipped = len(outside_window)
    else:
        template_id = method.removeprefix("template:")
        tpl = db.get(models.MessageTemplate, int(template_id)) if template_id.isdigit() else None
        if not tpl or tpl.status != "APPROVED" or tpl.business_id != business.id:
            raise HTTPException(status_code=400, detail="Template not found or not approved")
        sent = sum(
            1 for c in eligible
            if send_template_message(c.phone, tpl.name, tpl.language, c.name if tpl.has_variable and c.name else None)
        )
        failed = len(eligible) - sent
        skipped = 0

    return RedirectResponse(
        url=f"/admin/broadcast?sent={sent}&failed={failed}&skipped={skipped}",
        status_code=303,
    )


@router.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    rows = db.query(models.MessageTemplate).order_by(models.MessageTemplate.created_at.desc()).all()
    all_businesses = db.query(models.Business).order_by(models.Business.id).all()
    return templates.TemplateResponse(
        request, "admin_templates.html",
        {"templates": rows, "businesses": all_businesses, "configured": settings.template_management_configured},
    )


@router.post("/templates")
def templates_create(
    business_id: int = Form(...),
    name: str = Form(...),
    body_text: str = Form(...),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_admin),
):
    if not settings.template_management_configured:
        raise HTTPException(status_code=503, detail="Set WHATSAPP_BUSINESS_ACCOUNT_ID to submit templates")
    business = _get_business_or_400(db, business_id)
    # Meta requires lowercase letters, numbers, and underscores only.
    clean_name = re.sub(r"[^a-z0-9_]", "", name.strip().lower().replace(" ", "_"))
    if not clean_name:
        raise HTTPException(status_code=400, detail="Invalid template name")
    if not body_text.strip():
        raise HTTPException(status_code=400, detail="Template body can't be empty")

    result = create_message_template(clean_name, body_text)
    if result is None:
        raise HTTPException(status_code=502, detail="Meta rejected the template — check the name/body and try again")

    db.add(models.MessageTemplate(
        business_id=business.id, name=clean_name, body_text=body_text, has_variable="{{1}}" in body_text,
        status=result.get("status", "PENDING"),
    ))
    db.commit()
    return RedirectResponse(url="/admin/templates", status_code=303)


@router.post("/templates/{template_id}/refresh")
def templates_refresh(template_id: int, db: Session = Depends(get_db), _admin: None = Depends(require_admin)):
    tpl = db.get(models.MessageTemplate, template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    status = get_message_template_status(tpl.name, tpl.language)
    if status:
        tpl.status = status
        db.commit()
    return RedirectResponse(url="/admin/templates", status_code=303)
