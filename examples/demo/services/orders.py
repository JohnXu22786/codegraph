"""Order lifecycle for the demo."""

from .billing import price


def create_order(sku: str, qty: int, total: float) -> dict:
    """Persist an order and return its record."""
    return {"sku": sku, "qty": qty, "total": total}


def apply_coupon(order: dict, code: str) -> float:
    """Return the discounted total for a coupon code."""
    return price(order["total"]) * 0.8
