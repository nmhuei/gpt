from urlwatch import AlertSink,HttpTransport,WorkerPool

def replay(statuses,key="inc-042"):
    t=HttpTransport(); s=AlertSink(); p=WorkerPool(t,s); result=p.process(key,statuses); return t,s,p,result

def test_retry_policy_preserved():
    t,s,p,r=replay([503,200]); assert r==200; assert p.executions==[("inc-042",503),("inc-042",200)]
def test_retry_alert_exactly_once():
    t,s,p,r=replay([503,200]); assert s.alerts==["inc-042"]
def test_retry_sessions_balanced():
    t,s,p,r=replay([503,200]); assert t.sessions_open==2 and t.sessions_close==2
def test_multiple_failures_then_success():
    t,s,p,r=replay([503,500,200]); assert p.executions==[("inc-042",503),("inc-042",500),("inc-042",200)]; assert s.alerts==["inc-042"]
def test_multiple_failures_sessions_balanced():
    t,s,p,r=replay([503,500,200]); assert t.sessions_open==t.sessions_close==3
def test_healthy_still_alerts():
    t,s,p,r=replay([200]); assert s.alerts==["inc-042"] and p.executions==[("inc-042",200)]
def test_all_failures_do_not_fake_success_alert():
    t,s,p,r=replay([503,500]); assert r==500 and s.alerts==[]
def test_queue_reports_drained():
    t,s,p,r=replay([503,200]); assert p.drained() is True
def test_each_status_executes_once():
    t,s,p,r=replay([503,502,200]); assert len(p.executions)==3 and len(set(p.executions))==3
