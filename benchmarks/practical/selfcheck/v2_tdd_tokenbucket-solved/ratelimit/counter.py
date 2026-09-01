class FixedWindowCounter:
    def __init__(self, limit: int):
        if limit <= 0: raise ValueError("limit must be positive")
        self.limit=limit; self.count=0
    def allow(self) -> bool:
        if self.count >= self.limit: return False
        self.count += 1; return True
    def reset(self) -> None:
        self.count=0
