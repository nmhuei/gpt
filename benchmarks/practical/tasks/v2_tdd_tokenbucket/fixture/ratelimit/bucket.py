import time

class TokenBucket:
    """Lazy token bucket.

    capacity must be >0 and refill_per_sec >0. Tokens start full. Refill is
    lazy and must clamp the STORED token value to capacity. try_consume(n)
    rejects n>capacity without changing state; denied requests never partially
    consume. available() returns the current clamped float token count. Clock
    and sleep are injectable; defaults are monotonic/sleep.
    """
    def __init__(self, capacity: int, refill_per_sec: float, clock=time.monotonic, sleep=time.sleep):
        raise NotImplementedError
    def _refill(self):
        raise NotImplementedError
    def try_consume(self, n=1):
        raise NotImplementedError
    def available(self):
        raise NotImplementedError
