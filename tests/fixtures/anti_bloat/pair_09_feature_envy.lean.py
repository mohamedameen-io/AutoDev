"""Lean: `format_invoice` lives on Order where its data lives."""


class Customer:
    def __init__(self, name: str, vip: bool):
        self.name = name
        self.vip = vip


class Order:
    def __init__(self, customer: Customer, items: list, tax_rate: float):
        self.customer = customer
        self.items = items
        self.tax_rate = tax_rate

    def format_invoice(self) -> str:
        subtotal = sum(item["price"] * item["qty"] for item in self.items)
        tax = subtotal * self.tax_rate
        discount = 0.10 * subtotal if self.customer.vip else 0.0
        return f"{self.customer.name}: subtotal={subtotal:.2f} tax={tax:.2f} discount={discount:.2f} total={subtotal + tax - discount:.2f}"
