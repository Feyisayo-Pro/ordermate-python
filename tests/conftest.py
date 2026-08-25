import time

# pyrefly: ignore [missing-import]
import pytest
# pyrefly: ignore [missing-import]
from sqlalchemy import create_engine
# pyrefly: ignore [missing-import]
from sqlalchemy.orm import sessionmaker
# pyrefly: ignore [missing-import]
from sqlalchemy.pool import StaticPool
# pyrefly: ignore [missing-import]
from fastapi.testclient import TestClient

from app import database
from app.database import Base, get_db
from app.main import app
from app.config import get_settings

TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=TEST_ENGINE, autoflush=False, autocommit=False)

# The background scheduler (app/scheduler.py) opens its own session via
# app.database.SessionLocal rather than the get_db dependency (it's not
# running inside a request, so there's no dependency injection to hook into).
# Point that at the same in-memory test engine so a scheduled job and a test
# assertion see the same data — otherwise the job silently writes to the
# real ordermate.db file instead of the test database.
database.SessionLocal = TestingSessionLocal

# scheduler.py's persistent job store (SQLAlchemyJobStore) is built from
# database.engine, read at start_scheduler() call time — override it too, for
# the same reason as SessionLocal above, so it never touches the real
# configured database. Deliberately a SEPARATE dedicated engine from
# TEST_ENGINE (not the same one): APScheduler's background thread polls this
# engine for due jobs on its own schedule, concurrently with whatever the
# test/request thread is doing on TEST_ENGINE — sharing one StaticPool
# connection between both caused real "cannot commit transaction: SQL
# statements in progress" failures under SQLite. Postgres (production) has
# no such issue since it supports genuine concurrent connections; this
# split only exists to keep the test suite's SQLite backend deterministic.
SCHEDULER_TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
database.engine = SCHEDULER_TEST_ENGINE


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture(autouse=True)
def _reset_scheduler_jobs():
    """
    The job store now persists jobs in the database (see app/scheduler.py) —
    without this, a job scheduled by one test (e.g. "abandoned-1") would
    still be sitting in the (shared, session-lifetime) test engine's job
    table on the next test, where autoincrement ids restart from 1 too,
    causing cross-test interference that never happened with the old
    in-memory-only job store.
    """
    from app.scheduler import scheduler
    yield
    if scheduler.running:
        scheduler.remove_all_jobs()


#: Settings fields that must never leak from the developer's local .env into
#: a test run — each one has broken a "not configured by default" assumption
#: at least once already (WhatsApp creds when live-testing started; admin
#: creds when the dashboard's real credentials landed in .env). Add any new
#: credential-shaped setting here the moment it's real-tested locally, not
#: after it silently breaks a test.
_LOCAL_ONLY_CREDENTIAL_FIELDS = [
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_APP_SECRET",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "ANTHROPIC_API_KEY",
    "DEEPGRAM_API_KEY",
]


@pytest.fixture(autouse=True)
def _isolate_local_env_credentials():
    """
    Tests must never depend on (or accidentally act on) real credentials that
    might be sitting in the developer's local .env — force every field in
    _LOCAL_ONLY_CREDENTIAL_FIELDS blank regardless, so the suite stays
    deterministic, never makes a real network call, and never picks up a
    "configured" code path by accident. This was previously true by
    coincidence (the fields were always blank); once real credentials landed
    in .env for live testing, every test assuming that default broke.
    Individual tests that specifically want to test signed/configured
    behavior already set these themselves within a try/finally.
    """
    settings = get_settings()
    original = {field: getattr(settings, field) for field in _LOCAL_ONLY_CREDENTIAL_FIELDS}
    for field in _LOCAL_ONLY_CREDENTIAL_FIELDS:
        setattr(settings, field, "")
    yield
    for field, value in original.items():
        setattr(settings, field, value)


@pytest.fixture(autouse=True)
def _fast_feedback_delay():
    """Tests never want to wait 3 real hours for the feedback flow."""
    settings = get_settings()
    original = settings.FEEDBACK_DELAY_SECONDS
    settings.FEEDBACK_DELAY_SECONDS = 0
    yield
    settings.FEEDBACK_DELAY_SECONDS = original


@pytest.fixture(autouse=True)
def _fast_abandoned_nudge_delay():
    """
    Tests never want to wait 15 real minutes for the abandoned-order nudge.
    Deliberately NOT 0: this is a "date" trigger job, so a 0-second delay fires
    (and self-removes from the scheduler) almost instantly — too fast for tests
    that need to assert a job is still *pending* (e.g. that starting an order
    schedules one, or that completing one cancels it) without racing the
    background thread. 2s is comfortably longer than any test's synchronous
    assertions but still fast; tests that specifically want the job to fire
    override this to a near-zero value locally instead.
    """
    settings = get_settings()
    original = settings.ABANDONED_ORDER_NUDGE_SECONDS
    settings.ABANDONED_ORDER_NUDGE_SECONDS = 2
    yield
    settings.ABANDONED_ORDER_NUDGE_SECONDS = original


@pytest.fixture(autouse=True)
def _no_typing_delay():
    """Tests never want the deliberate 1s typing-indicator pause on every message."""
    settings = get_settings()
    original = settings.TYPING_DELAY_SECONDS
    settings.TYPING_DELAY_SECONDS = 0
    yield
    settings.TYPING_DELAY_SECONDS = original


def wait_until(predicate, timeout=2.0, interval=0.02) -> bool:
    """
    Polls `predicate()` until it's truthy or `timeout` elapses. Used instead of
    a flat time.sleep() when a test needs to wait on a background-scheduler job
    (APScheduler) — polling is both faster on the happy path and less flaky
    under load than a fixed sleep guessed to be "long enough".
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    yield session
    session.close()


def whatsapp_payload(phone: str, name: str, text: str) -> dict:
    """Builds a minimal Meta WhatsApp webhook payload for the given inbound message."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [{"from": phone, "text": {"body": text}}],
                            "contacts": [{"profile": {"name": name}}],
                        }
                    }
                ]
            }
        ]
    }


def whatsapp_list_reply_payload(phone: str, name: str, row_id: str, row_title: str) -> dict:
    """
    Builds a webhook payload for a customer tapping a row in an interactive
    list message (as opposed to typing plain text) — a different message
    shape ("type": "interactive") than whatsapp_payload's plain text.
    """
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [{
                                "from": phone,
                                "type": "interactive",
                                "interactive": {
                                    "type": "list_reply",
                                    "list_reply": {"id": row_id, "title": row_title},
                                },
                            }],
                            "contacts": [{"profile": {"name": name}}],
                        }
                    }
                ]
            }
        ]
    }
