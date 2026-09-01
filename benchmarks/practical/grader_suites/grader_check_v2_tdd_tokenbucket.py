import pytest
from ratelimit.bucket import TokenBucket

class FakeClock:
    def __init__(self,now=100.0): self.now=now
    def __call__(self): return self.now
    def advance(self,dt): self.now += dt

def new(cap=4,rate=2.0):
    c=FakeClock(); b=TokenBucket(cap,rate,clock=c,sleep=lambda _:None); return c,b

def test_start_full(): c,b=new(); assert b.available()==4
def test_consume_exact_capacity(): c,b=new(); assert b.try_consume(4); assert b.available()==0
def test_deny_empty_no_drain(): c,b=new(); b.try_consume(3); assert not b.try_consume(2); assert b.available()==1
def test_over_capacity_no_state_change(): c,b=new(); assert not b.try_consume(5); assert b.available()==4
def test_refill_one_second(): c,b=new(); b.try_consume(4); c.advance(1); assert b.available()==2
def test_refill_half_second(): c,b=new(); b.try_consume(4); c.advance(.5); assert b.available()==1
def test_clamp_long_idle(): c,b=new(); b.try_consume(4); c.advance(999); assert b.available()==4 and b._tokens<=4
def test_fractional_rate(): c,b=new(rate=.25); b.try_consume(4); c.advance(2); assert b.available()==.5
def test_multiple_reads_path_independent(): c,b=new(); b.try_consume(4); c.advance(.25); assert b.available()==.5; c.advance(.25); assert b.available()==1
def test_denial_preserves_fraction(): c,b=new(); b.try_consume(4); c.advance(.25); assert not b.try_consume(1); assert b.available()==.5
def test_refilled_then_consume(): c,b=new(); b.try_consume(4); c.advance(1); assert b.try_consume(2); assert b.available()==0
def test_bad_capacity():
    with pytest.raises(ValueError): TokenBucket(0,1)
def test_bad_rate():
    with pytest.raises(ValueError): TokenBucket(1,0)
@pytest.mark.parametrize("n",[0,-1])
def test_bad_consume(n):
    c,b=new()
    with pytest.raises(ValueError): b.try_consume(n)
def test_clock_can_move_back_without_refill(): c,b=new(); b.try_consume(2); c.advance(-1); assert b.available()==2
