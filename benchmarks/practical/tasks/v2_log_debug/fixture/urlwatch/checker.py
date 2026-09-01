class WorkerPool:
    """Retry status sequences; one logical incident must alert at most once."""
    def __init__(self,transport,sink):
        self.transport=transport; self.sink=sink; self.executions=[]
    def process(self,key,statuses):
        retried=False
        for status in statuses:
            self.executions.append((key,status))
            result=self.transport.request(status)
            if result >= 500:
                retried=True
                continue
            self.sink.send(key)
            if retried:
                self.sink.send(key)  # BUG: late completion path double-delivers
            return result
        return statuses[-1] if statuses else 200
    def drained(self): return True
