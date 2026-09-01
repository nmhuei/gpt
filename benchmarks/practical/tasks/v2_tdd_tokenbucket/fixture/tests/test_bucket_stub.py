from ratelimit.bucket import TokenBucket

def test_bucket_starts_full():
    b=TokenBucket(3, 1.0, clock=lambda: 0.0)
    assert b.available() == 3.0
