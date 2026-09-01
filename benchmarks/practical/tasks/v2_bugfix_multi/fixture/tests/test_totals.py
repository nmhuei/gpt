from orderkit import Discount, LineItem, Order, allocate_cents, apply_discounts

def order(cents):
    return Order([LineItem("sku", 1, cents)])

def test_no_discount():
    assert apply_discounts(order(1234), []) == 1234

def test_flat_discount():
    assert apply_discounts(order(1000), [Discount("flat", cents=250)]) == 750

def test_flat_clamps_zero():
    assert apply_discounts(order(100), [Discount("flat", cents=250)]) == 0

def test_percent_stack_is_additive():
    ds=[Discount("pct", basis_points=2000), Discount("pct", basis_points=3000)]
    assert apply_discounts(order(100000), ds) == 50000

def test_percent_uses_floor():
    assert apply_discounts(order(101), [Discount("pct", basis_points=5000)]) == 50

def test_flat_after_percent():
    ds=[Discount("flat", cents=500), Discount("pct", basis_points=1000)]
    assert apply_discounts(order(1000), ds) == 400

def test_allocate_equal_weights():
    assert allocate_cents(10, [1, 1]) == [5, 5]

def test_allocate_largest_remainder():
    assert allocate_cents(10, [1, 2]) == [3, 7]
