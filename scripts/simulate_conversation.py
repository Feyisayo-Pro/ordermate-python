"""
Fires the same conversation used in the sales demo (index.html) at a REAL
running local server — not a test, an actual HTTP client hitting your actual
webhook, the same way Meta would. Use this to sanity-check the whole thing
end to end before you have real WhatsApp Business API access.

Usage:
    # In one terminal:
    uvicorn app.main:app --reload

    # In another:
    python scripts/simulate_conversation.py
"""
import time
# pyrefly: ignore [missing-import]
import httpx

BASE_URL = "http://127.0.0.1:8000"
PHONE = "2348012345678"
NAME = "Tolu"


def send(text: str):
    payload = {
        "entry": [{"changes": [{"value": {
            "messages": [{"from": PHONE, "text": {"body": text}}],
            "contacts": [{"profile": {"name": NAME}}],
        }}]}]
    }
    resp = httpx.post(f"{BASE_URL}/webhook", json=payload, timeout=10)
    print(f">> {text!r:60} -> {resp.status_code} {resp.json()}")
    time.sleep(0.4)


def main():
    print("Watch the uvicorn terminal — with no WhatsApp credentials set, every")
    print("outgoing reply is printed there as '[DRY_RUN] -> <phone>: <message>'.\n")

    send("Hi! Do una still dey sell jollof rice today?")  # -> shows categories
    send("Mains")                                          # -> category chosen, shows items
    send("1")
    send("2 plates please")
    send("14 Adeyemi Street, Ikorodu")

    # find the order we just created so we can mark it delivered
    import sqlite3
    con = sqlite3.connect("ordermate.db")
    order_ref = con.execute(
        "SELECT order_ref FROM orders ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    con.close()

    print(f"\n>> Marking order {order_ref} as delivered (simulates a delivery-partner webhook)")
    resp = httpx.post(f"{BASE_URL}/admin/orders/{order_ref}/delivered", timeout=10)
    print(resp.json())

    wait_for = resp.json()["feedback_request_in_seconds"]
    print(f"Waiting {wait_for}s for the scheduled feedback request "
          f"(set FEEDBACK_DELAY_SECONDS=5 in .env to see this fire quickly)...")
    time.sleep(wait_for + 1)

    send("⭐️⭐️⭐️⭐️⭐️ that jollof hit different today")


if __name__ == "__main__":
    main()
