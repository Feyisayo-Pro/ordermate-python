"""
Single-tenant business configuration.

This is the ONE file you edit to point OrderMate at a different business —
everything else in the app is generic. When you're ready to support multiple
businesses at once, this becomes a database table instead of a constant; for
a fast single-business MVP, a plain config object is the right amount of
complexity (no need for multi-tenant plumbing you don't need yet).
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MenuItem:
    number: str  # must be unique across the WHOLE menu, not just within a category
    name: str
    price_ngn: int
    category: str
    quantity_unit: str = "plates"  # already-plural word used in "How many {quantity_unit}?"


@dataclass(frozen=True)
class BusinessConfig:
    slug: str
    display_name: str
    greeting_emoji: str
    menu: list[MenuItem]
    review_link: str
    payment_link_base: str  # e.g. paystack payment link prefix


MAMAS_KITCHEN = BusinessConfig(
    slug="mamas_kitchen",
    display_name="Mama's Kitchen",
    greeting_emoji="🍲",
    menu=[
        MenuItem(number="1", name="Jollof Rice + Chicken", price_ngn=3500, category="Mains"),
        MenuItem(number="2", name="Fried Rice + Turkey", price_ngn=4000, category="Mains"),
        MenuItem(number="3", name="Ofada + Beef", price_ngn=4500, category="Mains"),
        # Placeholder category/items so the categorized menu actually has more
        # than one category to demo — replace with the business's real drink
        # options and pricing before using this with a real customer.
        MenuItem(number="4", name="Chapman", price_ngn=1500, category="Drinks", quantity_unit="bottles"),
        MenuItem(number="5", name="Coca-Cola", price_ngn=500, category="Drinks", quantity_unit="bottles"),
    ],
    review_link="https://g.page/mamaskitchen/review",
    payment_link_base="https://paystack.link/mk-",
)

# The MVP runs against a single active business. Swap this constant to point
# the whole app at a different client without touching any other file.
ACTIVE_BUSINESS = MAMAS_KITCHEN
