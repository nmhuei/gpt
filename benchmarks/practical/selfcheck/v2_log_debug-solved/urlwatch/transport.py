class HttpTransport:
    """Tiny transport model with balanced open/close accounting."""
    def __init__(self): self.sessions_open=0; self.sessions_close=0
    def request(self,status):
        self.sessions_open += 1
        try:
            return status
        finally:
            self.sessions_close += 1
