# OrderMate

A multi-tenant WhatsApp platform for small businesses. One WhatsApp number
serves several businesses at once, across two verticals: **food ordering**
(multi-item carts, order tracking, delivery, feedback) and **appointment
booking**. Claude reads each customer message and decides what to do, with a
deterministic rule-based fallback so the whole flow still runs without an API
key. After delivery it asks for feedback, requests a public review from happy
customers, and quietly alerts the owner when someone is unhappy, before it
becomes a bad review.

**What is in it**

- **Multi-tenant by design.** Businesses, menu items and bookable services are
  database rows (`Business`, `FoodMenuItem`, `AppointmentService`), not a
  hardcoded constant. Conversation flow, admin dashboard and messaging all work
  against per-business data.
- **Language model layer** (`app/ai.py`): intent classification and structured
  field extraction with Anthropic Claude (Haiku by default), plus a rule-based
  fallback used when no key is set or the call fails.
- **Voice notes**: transcribed with Deepgram, then handled like typed text.
- **Admin dashboard** (`app/admin.py`): orders, appointments, status updates,
  broadcast campaigns and WhatsApp message templates, behind constant-time
  Basic Auth.
- **Campaign messaging** with a two-step confirm, the 24-hour window rule and
  opt-out handling: customers who opted out are excluded from every broadcast.
- **Scheduled jobs that survive restarts**: delayed feedback requests,
  abandoned-order nudges and a weekly owner digest, using APScheduler with a
  SQLAlchemy job store in the same database.
- **136 automated tests** across webhook security, conversation flow, AI
  parsing, appointment booking, the admin dashboard and the digest.

## Architecture, in one paragraph

FastAPI receives WhatsApp webhooks (`app/main.py`) after verifying the
`X-Hub-Signature-256` HMAC. A small explicit state machine per customer
(`app/conversation.py` + the `Conversation` table) decides what is happening:
mid-order, mid-booking, being asked for feedback, or starting fresh. It
resolves which business the customer is talking to (`app/businesses.py`) and
delegates "what did they actually mean" to `app/ai.py`, which calls Claude when
a key is configured and falls back to rule-based parsing when it is not.
Everything persists through SQLAlchemy (`app/models.py`): SQLite by default for
local development, Postgres in production by changing `DATABASE_URL`. A
background scheduler (`app/scheduler.py`) fires feedback requests, nudges and
the weekly digest.

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

136 tests, by file:

| File | Tests | Covers |
|---|---|---|
| `test_conversation_flow.py` | 45 | Order flow over the real HTTP webhook, multi-item carts, cancel and repeat customers, unsubscribe, owner commands, feedback branches (high rating asks for a review, low rating alerts the owner), human handoff, abandoned-order nudges |
| `test_admin.py` | 24 | Dashboard auth, order status updates, broadcast preview and send, opt-out and 24-hour window handling, templates |
| `test_whatsapp_client.py` | 22 | Webhook signature verification (valid, tampered, missing), parsing of text, list and button taps and voice-note payloads, dry-run sending, message-template API payloads |
| `test_ai.py` | 18 | Intent classification, quantity parsing and cart-continuation on both the rules path and the Claude path (including ignoring a hallucinated category), voice notes gated on Deepgram |
| `test_appointment_booking.py` | 12 | The second vertical: service and slot selection, full booking flow, cancel, owner notification, switching between businesses mid-conversation |
| `test_digest.py` | 9 | Weekly digest aggregation and formatting |
| `test_webhook.py` | 6 | Meta verification handshake, malformed and non-message payloads safely ignored |

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
- Use a managed Postgres instance in production by setting `DATABASE_URL`
  (`psycopg` is already in `requirements.txt`). SQLite is for local
  development only.
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
- **The admin dashboard is closed by default.** It uses HTTP Basic Auth with
  `ADMIN_USERNAME` / `ADMIN_PASSWORD`, compared with `hmac.compare_digest`
  (constant time). If either is unset the dashboard returns 503 instead of
  opening, because it shows customer names, phone numbers and addresses. Basic
  Auth is only safe over HTTPS, which every deployment platform provides.
- **Broadcasts respect consent.** Customers who opted out are excluded, anyone
  outside WhatsApp's 24-hour window is skipped rather than attempted, the
  owner's own number is excluded, and sending takes a preview step and an
  explicit second confirmation.
- **Customer phone numbers and order history are personal data** under
  Nigeria's NDPR. Before connecting real customers, put a basic data
  retention policy in place (how long you keep order/feedback history) and
  make sure the business owner knows this data exists and where it lives.
- **Errors never leak to the webhook response.** Any internal exception
  while processing a message is logged server-side and the endpoint still
  returns `200` (see `app/main.py`) — this avoids leaking stack traces and
  avoids Meta's retry storms on non-2xx responses.

## Known limitations (be upfront about these with a client)

- **Businesses are defined in code** (`app/businesses.py` seed data) and loaded
  into the database. Runtime data is per-business and database-backed, but
  there is no self-service onboarding screen yet.
- **One shared admin login.** Fine for one owner, not for a team. Per-user
  accounts with sessions are the natural upgrade.
- **The scheduler runs inside the web process.** Jobs persist across restarts,
  but run a single instance: APScheduler does not support several schedulers
  sharing one job store.
- **The rule-based fallback is intentionally simple.** Customers typing in
  natural, varied Nigerian English need the Claude-backed path
  (`ANTHROPIC_API_KEY` set) for reliable understanding.
- **Voice notes have no fallback.** Without `DEEPGRAM_API_KEY`, a voice note
  gets a "please type instead" reply.
- **Early, small-scale usage.** It is in use with two businesses and four of
  their customers so far. The conversation flow is covered by tests and the
  dry-run simulator, but it has not been proven at larger volume.
