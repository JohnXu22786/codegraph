"""Demo entry point: exercises the graph across services."""

from services.billing import charge, quote
from services.orders import create_order


def run(plan: str, sku: str, qty: int) -> dict:
    total = quote(plan, qty)
    order = create_order(sku, qty, total)
    return {"order": order, "charge": charge(order)}


if __name__ == "__main__":
    print(run("pro", "sku-1", 3))
