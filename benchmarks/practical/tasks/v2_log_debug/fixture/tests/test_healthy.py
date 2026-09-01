from urlwatch import AlertSink,HttpTransport,WorkerPool

def test_healthy_request_alerts_once():
    t=HttpTransport(); s=AlertSink(); p=WorkerPool(t,s)
    assert p.process("host-a",[200])==200
    assert s.alerts==["host-a"]
    assert t.sessions_open==t.sessions_close==1
