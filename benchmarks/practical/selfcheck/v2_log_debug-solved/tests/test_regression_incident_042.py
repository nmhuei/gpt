from urlwatch import AlertSink,HttpTransport,WorkerPool

def test_incident_042_retry_does_not_duplicate_or_leak():
    transport=HttpTransport(); sink=AlertSink(); pool=WorkerPool(transport,sink)
    assert pool.process("inc-042",[503,200])==200
    assert sink.alerts==["inc-042"]
    assert transport.sessions_open==transport.sessions_close==2
    assert pool.executions==[("inc-042",503),("inc-042",200)]
