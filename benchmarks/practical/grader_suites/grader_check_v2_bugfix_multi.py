import pytest
from orderkit import Discount, LineItem, Order, allocate_cents, apply_discounts

def order(cents, qty=1):
    return Order([LineItem("x", qty, cents)])

@pytest.mark.parametrize("subtotal,bp,expected", [
    (100000, 5000, 50000), (101, 5000, 50), (999, 3333, 666),
    (1000, 0, 1000), (1000, 10000, 0), (1000, 12000, 0),
])
def test_pct_floor_and_cap(subtotal,bp,expected):
    assert apply_discounts(order(subtotal), [Discount("pct", basis_points=bp)]) == expected

def test_pct_stacks_additively():
    ds=[Discount("pct",basis_points=2000),Discount("pct",basis_points=3000)]
    assert apply_discounts(order(100000), ds)==50000

def test_flat_after_pct_regardless_input_order():
    ds=[Discount("flat",cents=500),Discount("pct",basis_points=1000)]
    assert apply_discounts(order(1000), ds)==400

def test_multiple_flats_and_clamp():
    ds=[Discount("flat",cents=600),Discount("flat",cents=500)]
    assert apply_discounts(order(1000),ds)==0

def test_empty_order():
    assert apply_discounts(Order([]),[])==0

@pytest.mark.parametrize("total,weights,expected", [
    (10,[1,2],[3,7]), (11,[1,1,1],[4,4,3]), (5,[1,3,1],[1,3,1]),
    (1,[1,1,1],[1,0,0]), (0,[1,2],[0,0]),
])
def test_largest_remainder(total,weights,expected):
    assert allocate_cents(total,weights)==expected

def test_allocation_sum_and_shape():
    out=allocate_cents(997,[3,7,11,13])
    assert sum(out)==997 and len(out)==4 and all(x>=0 for x in out)
