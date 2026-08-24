# OrderMate

A WhatsApp bot for small businesses: takes orders, tracks them, requests
feedback after delivery, and automatically asks happy customers for a public
review — while quietly alerting the owner when a customer isn't happy, before
it becomes a bad review.

This is the Python rebuild of the OrderMate concept, scoped as a fast
**single-business MVP** (currently configured for "Mama's Kitchen" — see
`app/business_config.py` to point it at a different business). It runs fully
locally with zero paid API keys via built-in dry-run/fallback modes, so you
can develop, test, and demo the entire flow before signing up for anything.

## Architecture, in one paragraph

FastAPI receives WhatsApp webhooks (`app/main.py`), a small explicit state
machine per customer (`app/conversation.py` + the `Conversation` table)
decides what's happening — are they mid-order, being asked for feedback, or
starting fresh — and delegates the "what did they actually mean" understanding
to `app/ai.py`, which calls Anthropic's Claude when a key is configured or
falls back to deterministic rule-based parsing when it isn't. Everything
persists to SQLite via SQLAlchemy (`app/models.py`). A background scheduler
(`app/scheduler.py`) fires the feedback request a few hours after an order is
marked delivered.

## Quickstart (no WhatsApp or Anthropic account needed)

Requires **Python 3.11 or 3.12** (matches the Dockerfile). Newer interpreters
(3.13+) may not yet have prebuilt wheels for pinned dependencies like
`pydantic-core`, which fails to build from source until upstream catches up —
if `pip install` fails on a wheel build, check `python3 --version` first.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# optionally edit .env — defaults work fine for local dry-run development

uvicorn app.main:app --reload
```

In another terminal, run the scripted demo conversation against your real
running server:

```bash
python scripts/simulate_conversation.py
```

Watch the `uvicorn` terminal — every outgoing WhatsApp message is printed as
`[DRY_RUN] -> <phone>: <message>` since no WhatsApp credentials are configured
yet. This proves the entire order → deliver → feedback → review-request loop
end to end, for free, before you've touched Meta's developer portal.

## Running the tests

```bash
python -m pytest -v
```

Tests cover: webhook signature verification (accepts valid, rejects tampered/
missing), the Meta verification handshake, the full order-creation flow via
the actual HTTP webhook endpoint, order tracking, both the high-rating
(review request) and low-rating (owner alert, no review request) feedback
branches, malformed/non-message webhook payloads being safely ignored, the
"speak to a human" handoff (owner alert + customer acknowledgment, with and
without OWNER_WHATSAPP_NUMBER configured), abandoned-order nudges (scheduled
while ordering, cancelled on completion, fires after real silence), and the
weekly digest's aggregation/formatting logic.

## Getting a real WhatsApp Business number (Meta setup guide)

You don't need this to develop or demo locally — only once you're ready to
go live with a real business's customers.

1. **Create a Meta Developer account** at [developers.facebook.com](https://developers.facebook.com)
   if you don't have one, then create a new App, choosing the "Business"
   type.
2. **Add the WhatsApp product** to that app from the App Dashboard. Meta
   gives you a free test phone number automatically — enough to develop and
   demo with real WhatsApp messages before the business's real number is
   verified.
3. **Get your credentials** from the WhatsApp > API Setup page:
   - `WHATSAPP_PHONE_NUMBER_ID` — shown directly on that page.
   - `WHATSAPP_ACCESS_TOKEN` — the page gives you a 24-hour temporary token
     to start. For anything beyond quick testing, generate a **permanent
     token** instead: Business Settings → System Users → create a system
     user scoped only to this WhatsApp Business Account → generate token.
     Never use your personal user access token for a production bot.
   - `WHATSAPP_APP_SECRET` — App Dashboard → Settings → Basic.
4. **Expose your local server to the internet** so Meta can reach your
   webhook during development — [ngrok](https://ngrok.com) is the standard
   tool: `ngrok http 8000`, then use the `https://...ngrok-free.app/webhook`
   URL it gives you.
5. **Configure the webhook** in WhatsApp > Configuration: paste your ngrok
   (or, once deployed, your real) URL + `/webhook`, and set the verify token
   to whatever you put in `WHATSAPP_VERIFY_TOKEN` in `.env` (they must match
   exactly). Subscribe to the `messages` field.
6. **Move to the business's real number**: once you're ready to go live,
   Meta requires the business to verify their own phone number and complete
   Business Verification for higher messaging limits — this can take a few
   business days, so start it early, not the day you want to launch.

## Deploying

Any platform that runs a long-lived Python process works — Render, Railway,
and Fly.io are the simplest for a project this size (a `Dockerfile` is
included). Whichever you choose:

- Set all `.env` values as platform secrets/environment variables, **not**
  as a committed file.
- Switch `DATABASE_URL` to a managed Postgres instance once you have more
  than one business or meaningful order volume — no code changes needed,
  SQLAlchemy handles both identically.
- Set `FEEDBACK_DELAY_SECONDS` back to a real delay (default is 3 hours).

## Security notes (read before connecting a real business's number)

- **Webhook signature verification is mandatory in production.** Once
  `WHATSAPP_APP_SECRET` is set, every inbound webhook's `X-Hub-Signature-256`
  header is verified via HMAC-SHA256 before the payload is processed
  (`app/whatsapp_client.py::verify_webhook_signature`) — without this,
  anyone who discovers your webhook URL could POST fake customer messages.
  With no secret set (local dev only), verification is skipped and a loud
  warning is logged — this should never be true in production.
- **Credentials live only in environment variables**, never in source. Use
  a proper secrets manager on your deployment platform rather than a
  committed `.env`.
- **Least-privilege access token.** Generate the WhatsApp permanent token
  from a System User scoped to only this WhatsApp Business Account, not a
  personal or admin-level token.
- **No payment card data touches this system.** Payments are handled via an
  external payment link (e.g. Paystack) sent as plain text — this keeps the
  bot entirely out of PCI-DSS scope.
- **Customer phone numbers and order history are personal data** under
  Nigeria's NDPR. Before connecting real customers, put a basic data
  retention policy in place (how long you keep order/feedback history) and
  make sure the business owner knows this data exists and where it lives.
- **Errors never leak to the webhook response.** Any internal exception
  while processing a message is logged server-side and the endpoint still
  returns `200` (see `app/main.py`) — this avoids leaking stack traces and
  avoids Meta's retry storms on non-2xx responses.

## Known limitations of this MVP (be upfront about these with a client)

- Single business per deployment (see `app/business_config.py`) — fine for
  a pilot, would need a `Business` table + per-tenant routing to serve
  multiple clients from one deployment.
- Scheduled feedback requests use an in-memory job store (APScheduler) —
  a job is lost if the server restarts before it fires. Acceptable for a
  pilot's order volume; swap in a persistent job store or a proper task
  queue (Celery/RQ + Redis) before relying on this at scale.
- The rule-based AI fallback is intentionally simple (built to run the demo
  conversation deterministically) — real customers typing in natural,
  varied Nigerian English will need the Claude-backed path (`ANTHROPIC_API_KEY`
  set) for reliable understanding in production.
- Voice note transcription (`DEEPGRAM_API_KEY`) has no rule-based fallback —
  without it, a voice note gets a "please type instead" reply.
