"""Pricing and payment for the demo."""

PLAN_PRICES = {"basic": 10, "pro": 25, "enterprise": 80}


def quote(plan: str, seats: int) -> float:
    """Total list price for a plan and seat count."""
    return PLAN_PRICES.get(plan, 0) * seats


def price(total: float) -> float:
    """Apply the standard surcharge."""
    return total * 1.05


def charge(order: dict) -> str:
    """Simulate charging an order; returns a reference id."""
    return f"chg-{order['total']}"
