class HttpTransport:
    """Tiny injectable transport model used by the incident replay."""
    def __init__(self): self.sessions_open=0; self.sessions_close=0
    def request(self,status):
        self.sessions_open += 1
        if status >= 500:
            return status  # BUG: error path leaks the opened session
        self.sessions_close += 1
        return status
