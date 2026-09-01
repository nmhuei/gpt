import time

class TokenBucket:
    """Lazy, injectable token bucket with clamp-at-write semantics."""
    def __init__(self, capacity: int, refill_per_sec: float, clock=time.monotonic, sleep=time.sleep):
        if capacity <= 0: raise ValueError("capacity must be positive")
        if refill_per_sec <= 0: raise ValueError("refill_per_sec must be positive")
        self.capacity=int(capacity); self.refill_per_sec=float(refill_per_sec)
        self._clock=clock; self._sleep=sleep; self._tokens=float(capacity); self._last=float(clock())
    def _refill(self):
        now=float(self._clock()); elapsed=max(0.0, now-self._last)
        self._tokens=min(float(self.capacity), self._tokens + elapsed*self.refill_per_sec)
        self._last=now
    def try_consume(self, n=1):
        n=float(n)
        if n <= 0: raise ValueError("n must be positive")
        self._refill()
        if n > self.capacity or self._tokens < n: return False
        self._tokens -= n; return True
    def available(self):
        self._refill(); return self._tokens
