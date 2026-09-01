from .models import Order

def apply_discounts(order: Order, discounts) -> int:
    """Apply percentage discounts once using additive basis points and floor."""
    subtotal = sum(li.qty * li.unit_price_cents for li in order.items)
    bp = min(10000, sum(max(0, d.basis_points) for d in discounts if d.kind == "pct"))
    total = (subtotal * (10000 - bp)) // 10000
    total -= sum(max(0, d.cents) for d in discounts if d.kind == "flat")
    return max(total, 0)
