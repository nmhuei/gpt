class RetryPolicy:
    def should_retry(self,status=None,exc=None):
        if exc is not None: return True
        return status==429 or (status is not None and 500<=status<600)
    def delay(self,attempt,retry_after=None):
        return float(retry_after) if retry_after is not None else 0.1*(2**attempt)
