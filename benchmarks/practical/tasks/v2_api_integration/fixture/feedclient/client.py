class FeedClient:
    """Feed API client. Public signatures are part of the benchmark contract."""
    def __init__(self,base_url,api_key,transport=None,sleep=None,max_retries=3):
        self.base_url=base_url; self.api_key=api_key; self.transport=transport; self.sleep=sleep; self.max_retries=max_retries
    def fetch_page(self,cursor=None,since=None,limit=100): raise NotImplementedError
    def subscribe(self,topic,idempotency_key=None): raise NotImplementedError
