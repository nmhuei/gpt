from ratelimit.counter import FixedWindowCounter

def test_counter_limit_and_reset():
    c=FixedWindowCounter(2)
    assert c.allow() is True
    assert c.allow() is True
    assert c.allow() is False
    c.reset(); assert c.allow() is True

def test_counter_rejects_bad_limit():
    import pytest
    with pytest.raises(ValueError): FixedWindowCounter(0)
