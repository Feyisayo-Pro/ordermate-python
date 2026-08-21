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
"""
from __future__ import annotations

import hmac

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

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
security = HTTPBasic()

router = APIRouter(prefix="/admin", tags=["admin"])


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
