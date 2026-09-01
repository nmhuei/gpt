class RetryPolicy:
    def should_retry(self,status=None,exc=None): raise NotImplementedError
    def delay(self,attempt,retry_after=None): raise NotImplementedError
