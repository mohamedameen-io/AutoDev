"""Verbose: function in module A reaches into module B's internals via attribute chains.

Both classes are in this file for fixture portability, but `format_invoice`
clearly belongs as a method on Order.
"""


class Customer:
    def __init__(self, name: str, vip: bool):
        self.name = name
        self.vip = vip


class Order:
    def __init__(self, customer: Customer, items: list, tax_rate: float):
        self.customer = customer
        self.items = items
        self.tax_rate = tax_rate


def format_invoice(order: Order) -> str:
    subtotal = sum(item["price"] * item["qty"] for item in order.items)
    tax = subtotal * order.tax_rate
    discount = 0.10 * subtotal if order.customer.vip else 0.0
    total = subtotal + tax - discount
    return f"{order.customer.name}: subtotal={subtotal:.2f} tax={tax:.2f} discount={discount:.2f} total={total:.2f}"
