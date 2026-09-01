class WorkerPool:
    """Retry status sequences while emitting at most one alert per logical run."""
    def __init__(self,transport,sink):
        self.transport=transport; self.sink=sink; self.executions=[]
    def process(self,key,statuses):
        alerted=False
        for status in statuses:
            self.executions.append((key,status))
            result=self.transport.request(status)
            if result >= 500:
                continue
            if not alerted:
                self.sink.send(key); alerted=True
            return result
        return statuses[-1] if statuses else 200
    def drained(self): return True
