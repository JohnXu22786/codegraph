"""Cart storage for the demo shop."""

import os
from pkg import pricing


class Cart:
    """A simple shopping cart."""

    def __init__(self, owner):
        self.owner = owner
        self.items = []

    def add(self, sku, qty=1):
        self._items.append((sku, qty))
        pricing.discount(sku)

    def total(self):
        t = 0
        for sku, qty in self.items:
            t += pricing.price(sku) * qty
        return t


def create_cart(owner):
    return Cart(owner)
