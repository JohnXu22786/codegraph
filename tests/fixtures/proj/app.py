"""Entry point of the demo shop."""

from pkg.cart import Cart, create_cart
import pkg.pricing


def main():
    cart = create_cart("alice")
    cart.add("sku-1", 2)
    return cart.total()


if __name__ == "__main__":
    main()
