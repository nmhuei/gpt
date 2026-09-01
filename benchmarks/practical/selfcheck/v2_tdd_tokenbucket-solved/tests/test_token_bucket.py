import pytest
from ratelimit.bucket import TokenBucket

class Clock:
    def __init__(self): self.now=0.0
    def __call__(self): return self.now
    def advance(self,x): self.now += x

def make(cap=5, rate=1.0):
    c=Clock(); return c, TokenBucket(cap,rate,clock=c,sleep=lambda _:None)

def test_full_start(): c,b=make(); assert b.available()==5
def test_consume(): c,b=make(); assert b.try_consume(2) and b.available()==3
def test_denied_does_not_drain(): c,b=make(); b.try_consume(4); assert not b.try_consume(2); assert b.available()==1
def test_over_capacity_guard(): c,b=make(); assert not b.try_consume(6); assert b.available()==5
def test_lazy_refill(): c,b=make(); b.try_consume(5); c.advance(2); assert b.available()==2
def test_capacity_clamp(): c,b=make(); b.try_consume(5); c.advance(100); assert b.available()==5; assert b._tokens==5
def test_fractional_refill(): c,b=make(rate=.5); b.try_consume(5); c.advance(1); assert b.available()==.5
def test_fractional_accumulates(): c,b=make(rate=.5); b.try_consume(5); c.advance(.5); assert b.available()==.25; c.advance(.5); assert b.available()==.5
def test_deny_then_refill(): c,b=make(); b.try_consume(5); assert not b.try_consume(); c.advance(1); assert b.try_consume()
def test_zero_rejected(): c,b=make();

@pytest.mark.parametrize("n",[0,-1])
def test_nonpositive_rejected(n):
    c,b=make()
    with pytest.raises(ValueError): b.try_consume(n)

def test_injected_clock_is_used(): c,b=make(); b.try_consume(5); c.advance(3); assert b.available()==3
