"""
WhatsApp Cloud API client + webhook security helpers.

SECURITY NOTE (read this before deploying):
  - verify_webhook_signature() checks Meta's X-Hub-Signature-256 header, an
    HMAC-SHA256 of the raw request body keyed with your App Secret. Without
    this check, ANYONE who finds your webhook URL could POST fake "customer
    messages" that create fake orders or trigger fake alerts. This check is
    mandatory before this goes live with a real WHATSAPP_APP_SECRET set.
  - The access token is a long-lived credential. Use a System User token
    scoped only to this WhatsApp Business Account (least privilege), and in
    production store it in a secret manager, not a committed .env file.
  - send_message() never raises the raw Meta API error back to the caller —
    it logs the failure server-side and returns False, so a WhatsApp API
    outage can't leak internal details into a webhook response.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

# pyrefly: ignore [missing-import]
import httpx

from app.config import get_settings

log = logging.getLogger("ordermate.whatsapp")
settings = get_settings()

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Returns True if the request really came from Meta.

    In local dev, if WHATSAPP_APP_SECRET isn't set yet, we skip verification
    (there's nothing to verify against) but log a loud warning — this must
    never be silently skipped in production.
    """
    if not settings.WHATSAPP_APP_SECRET:
        log.warning("WHATSAPP_APP_SECRET not set — webhook signature verification SKIPPED (dev only!)")
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def send_message(to_phone: str, text: str) -> bool:
    """
    Sends a WhatsApp text message. In dry-run mode (no credentials configured),
    logs the outgoing message instead of calling the real API — this is what
    lets you run and demo the entire conversation flow before you have a
    verified WhatsApp Business number.
    """
    if not settings.whatsapp_configured:
        log.info("[DRY_RUN] -> %s: %s", to_phone, text)
        return True

    url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        log.exception("Failed to send WhatsApp message to %s", to_phone)
        return False


@dataclass(frozen=True)
class ListMenuRow:
    id: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class ListMenuSection:
    title: str
    rows: list[ListMenuRow]


@dataclass(frozen=True)
class ListMenu:
    """
    A native WhatsApp interactive list message — customer taps a button, gets
    a modal of selectable rows grouped into sections. Meta caps this at 10
    total rows across all sections combined (not 10 per section), which is
    why conversation.py splits a categorized menu into two of these (pick a
    category, then pick an item) rather than one flat list.
    """
    body: str
    button_text: str
    sections: list[ListMenuSection]


def send_list_menu(to_phone: str, menu: ListMenu) -> bool:
    """
    Sends a WhatsApp interactive list message. Mirrors send_message's
    dry-run/error-handling behavior exactly — see its docstring.
    """
    if not settings.whatsapp_configured:
        section_summary = ", ".join(f"{s.title} ({len(s.rows)} options)" for s in menu.sections)
        log.info("[DRY_RUN] -> %s: [LIST MENU] %s | %s", to_phone, menu.body, section_summary)
        return True

    url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": menu.body},
            "action": {
                "button": menu.button_text,
                "sections": [
                    {
                        "title": section.title,
                        "rows": [
                            {"id": row.id, "title": row.title, "description": row.description}
                            for row in section.rows
                        ],
                    }
                    for section in menu.sections
                ],
            },
        },
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        log.exception("Failed to send list menu to %s", to_phone)
        return False


@dataclass(frozen=True)
class ButtonOption:
    id: str
    title: str  # Meta caps this at 20 characters


@dataclass(frozen=True)
class ButtonMenu:
    """
    Native WhatsApp reply buttons — up to 3, rendered directly in the chat
    (no extra tap to open a modal, unlike ListMenu). Use this instead of
    ListMenu when there are 3 or fewer options; Meta doesn't support more
    than 3 buttons in one message.
    """
    body: str
    options: list[ButtonOption]


def send_button_menu(to_phone: str, menu: ButtonMenu) -> bool:
    """
    Sends a WhatsApp interactive reply-button message. Mirrors send_message's
    dry-run/error-handling behavior exactly — see its docstring.
    """
    if not settings.whatsapp_configured:
        options_summary = ", ".join(o.title for o in menu.options)
        log.info("[DRY_RUN] -> %s: [BUTTONS] %s | %s", to_phone, menu.body, options_summary)
        return True

    url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": menu.body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": o.id, "title": o.title}}
                    for o in menu.options
                ],
            },
        },
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        log.exception("Failed to send button menu to %s", to_phone)
        return False


def send_typing_indicator(message_id: str) -> bool:
    """
    Marks an inbound message as read and shows a "typing..." indicator to the
    customer — a real WhatsApp Cloud API feature (persists until we send our
    actual reply, or 25s max, whichever comes first). Purely cosmetic: skipped
    entirely in dry-run mode (there's no real WhatsApp UI to show it in), and
    never raises — a failure here should never block the actual reply.
    """
    if not message_id or not settings.whatsapp_configured:
        return False

    url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        log.exception("Failed to send typing indicator for message_id=%s", message_id)
        return False


def download_media(media_id: str) -> bytes | None:
    """
    Downloads WhatsApp media (e.g. a voice note) via Meta's two-step Media
    API: resolve media_id to a short-lived download URL, then fetch the
    bytes from that URL — both steps need the same Bearer token. Returns
    None in dry-run mode (no real media to fetch) or on any failure; never
    raises, matching send_message's error-handling style.
    """
    if not settings.whatsapp_configured:
        return None

    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    try:
        meta_resp = httpx.get(f"{GRAPH_API_BASE}/{media_id}", headers=headers, timeout=10)
        meta_resp.raise_for_status()
        media_url = meta_resp.json()["url"]

        media_resp = httpx.get(media_url, headers=headers, timeout=20)
        media_resp.raise_for_status()
        return media_resp.content
    except (httpx.HTTPError, KeyError):
        log.exception("Failed to download media %s", media_id)
        return None


def extract_incoming_message(payload: dict) -> dict | None:
    """
    Pulls the useful bits out of Meta's (fairly deeply nested) webhook payload.
    Returns None for payloads that aren't an actual inbound text message
    (delivery receipts, status updates, etc. — WhatsApp sends those to the
    same webhook and they should be quietly ignored, not errored on).

    A tapped interactive-list row arrives as its own message type (not
    "text"), carrying the row's id rather than free-form text. Normalizing
    that id into the same "text" field here means every downstream consumer
    (conversation.py, ai.py) handles a typed reply and a tapped one
    identically — they never need to know which one happened.
    """
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
        messages = change.get("messages")
        if not messages:
            return None  # status update / read receipt, not a message
        message = messages[0]
        contact = change.get("contacts", [{}])[0]

        media_id = ""
        if message.get("type") == "interactive":
            interactive = message.get("interactive", {})
            # A tapped list row and a tapped reply button are different
            # sub-shapes ("list_reply" vs "button_reply") but both just carry
            # an id — normalized into "text" the same way either way, so
            # conversation.py/ai.py never need to know which UI was tapped.
            reply = interactive.get("list_reply") or interactive.get("button_reply") or {}
            text = reply.get("id", "")
        elif message.get("type") == "audio":
            # Voice notes carry no text at all — just a media_id to fetch.
            # The caller (main.py) downloads and transcribes it, then feeds
            # the result back through the same "text" pipeline as everything
            # else, so conversation.py and ai.py never need to know a message
            # started as audio.
            text = ""
            media_id = message.get("audio", {}).get("id", "")
        else:
            text = message.get("text", {}).get("body", "")

        return {
            "phone": message.get("from", ""),
            "name": contact.get("profile", {}).get("name", ""),
            "text": text,
            "message_id": message.get("id", ""),
            "media_id": media_id,
        }
    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Message templates — the only way to message a customer outside WhatsApp's
# 24h reply window (used by app/admin.py's broadcast + /templates routes).
# Meta requires every template to go through their own review before it can
# be used; nothing here can skip or speed that up. WHATSAPP_BUSINESS_ACCOUNT_ID
# (distinct from WHATSAPP_PHONE_NUMBER_ID) is required for the create/status
# calls, since templates belong to the WABA, not to a specific phone number.
# ---------------------------------------------------------------------------

def create_message_template(name: str, body_text: str, language: str = "en_US") -> dict | None:
    """
    Submits a new template for Meta's review. body_text may include exactly
    one {{1}} placeholder (the caller decides what to fill it with at send
    time — see send_template_message). Returns Meta's response dict
    (typically {"id":..., "status": "PENDING", "category": "MARKETING"}) on
    success, or None if template management isn't configured or the request
    fails — never raises, matching this module's other functions.
    """
    if not settings.template_management_configured:
        log.warning("WHATSAPP_BUSINESS_ACCOUNT_ID not set — can't submit a message template")
        return None

    url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_BUSINESS_ACCOUNT_ID}/message_templates"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "name": name,
        "language": language,
        "category": "MARKETING",
        "components": [{"type": "BODY", "text": body_text}],
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        log.exception("Failed to submit message template %r", name)
        return None


def get_message_template_status(name: str, language: str = "en_US") -> str | None:
    """Re-checks a submitted template's current review status with Meta. Returns
    None if template management isn't configured, the template isn't found, or
    the request fails."""
    if not settings.template_management_configured:
        return None

    url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_BUSINESS_ACCOUNT_ID}/message_templates"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    try:
        resp = httpx.get(url, headers=headers, params={"name": name}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        match = next((t for t in data.get("data", []) if t.get("language") == language), None)
        return match.get("status") if match else None
    except httpx.HTTPError:
        log.exception("Failed to check status of message template %r", name)
        return None


def send_template_message(to_phone: str, name: str, language: str, variable: str | None) -> bool:
    """
    Sends an approved template message — the only compliant way to reach a
    customer outside WhatsApp's 24h reply window. Mirrors send_message's
    dry-run/error-handling behavior exactly.
    """
    if not settings.whatsapp_configured:
        log.info("[DRY_RUN] -> %s: [TEMPLATE %s] variable=%r", to_phone, name, variable)
        return True

    url = f"{GRAPH_API_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    template: dict = {"name": name, "language": {"code": language}}
    if variable is not None:
        template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": variable}]}]
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": template,
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        log.exception("Failed to send template message %r to %s", name, to_phone)
        return False
