"""Price lookups for the demo shop."""

PRICES = {"sku-1": 10}


def price(sku):
    """Look up the base price of a sku."""
    return PRICES.get(sku, 0)


def discount(sku):
    """Apply the standing discount."""
    return price(sku) * 0.9
