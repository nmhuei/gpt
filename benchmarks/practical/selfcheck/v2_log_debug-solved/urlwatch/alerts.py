class AlertSink:
    def __init__(self): self.alerts=[]
    def send(self, incident_id): self.alerts.append(incident_id)
