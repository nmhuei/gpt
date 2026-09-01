from dataclasses import dataclass, field

@dataclass(frozen=True)
class LineItem:
    sku: str
    qty: int
    unit_price_cents: int

@dataclass(frozen=True)
class Discount:
    kind: str
    basis_points: int = 0
    cents: int = 0

@dataclass
class Order:
    items: list[LineItem] = field(default_factory=list)

def subtotal_cents(order: Order) -> int:
    return sum(li.qty * li.unit_price_cents for li in order.items)
