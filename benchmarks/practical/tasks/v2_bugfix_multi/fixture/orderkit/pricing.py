from .models import Order

def apply_discounts(order: Order, discounts) -> int:
    """Apply stacked discounts in integer cents.

    Contract: percentage basis points ADD, cap at 10000, then apply once using
    integer FLOOR. Flat discounts are subtracted afterwards; result clamps at 0.
    """
    total = sum(li.qty * li.unit_price_cents for li in order.items)
    for d in discounts:
        if d.kind == "pct":
            total = round(total * (10000 - d.basis_points) / 10000)
        elif d.kind == "flat":
            total -= d.cents
    return max(total, 0)
