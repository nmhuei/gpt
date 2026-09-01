import json,time
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from .errors import AuthError,FeedError,InvalidResponse,RateLimited,TransientError
from .retry import RetryPolicy

class FeedClient:
    def __init__(self,base_url,api_key,transport=None,sleep=time.sleep,max_retries=3):
        self.base_url=base_url.rstrip("/"); self.api_key=api_key; self.transport=transport; self.sleep=sleep; self.max_retries=max_retries; self.retry=RetryPolicy()
    def _decode(self,resp,status):
        try: data=json.loads(resp.read().decode("utf-8"))
        except Exception as exc: raise InvalidResponse("invalid JSON",status) from exc
        if not isinstance(data,dict): raise InvalidResponse("response must be an object",status)
        return data
    def fetch_page(self,cursor=None,since=None,limit=100):
        params={"limit":str(limit)}
        if cursor is not None: params["cursor"]=str(cursor)
        if since is not None: params["since"]=str(since)
        url=self.base_url+"/items?"+urlencode(params)
        for attempt in range(self.max_retries+1):
            req=Request(url,headers={"X-API-Key":self.api_key})
            try:
                resp=urlopen(req,timeout=2); status=resp.status; data=self._decode(resp,status)
                if "items" not in data or "next_cursor" not in data or not isinstance(data["items"],list): raise InvalidResponse("invalid page shape",status)
                return data
            except HTTPError as exc:
                status=exc.code; retry_after=exc.headers.get("Retry-After")
                retry=self.retry.should_retry(status=status)
                if retry and attempt<self.max_retries:
                    self.sleep(self.retry.delay(attempt,retry_after)); continue
                if status==401: raise AuthError("unauthorized") from exc
                if status==429: raise RateLimited(float(retry_after or 0)) from exc
                if 500<=status<600: raise TransientError(f"server status {status}") from exc
                raise FeedError(f"HTTP {status}") from exc
            except URLError as exc:
                if self.retry.should_retry(exc=exc) and attempt<self.max_retries:
                    self.sleep(self.retry.delay(attempt)); continue
                raise TransientError(str(exc)) from exc
        raise TransientError("retry budget exhausted")
    def subscribe(self,topic,idempotency_key=None):
        raw=json.dumps({"topic":topic}).encode(); headers={"X-API-Key":self.api_key,"Content-Type":"application/json"}
        if idempotency_key is not None: headers["Idempotency-Key"]=idempotency_key
        req=Request(self.base_url+"/subscribe",data=raw,headers=headers,method="POST")
        try:
            resp=urlopen(req,timeout=2); return self._decode(resp,resp.status)
        except HTTPError as exc:
            if exc.code==401: raise AuthError("unauthorized") from exc
            raise FeedError(f"HTTP {exc.code}") from exc
