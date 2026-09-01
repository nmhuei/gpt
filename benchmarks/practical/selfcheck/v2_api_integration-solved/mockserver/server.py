#!/usr/bin/env python3
import json,os,sys
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlparse
from scenarios import items

SCENARIO=os.environ.get("SCENARIO","happy"); STATS={"requests":0,"items":0,"subscribe":0}
DATA=items()
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def sendj(self,status,obj,headers=None):
        raw=json.dumps(obj,separators=(",",":" )).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw)))
        for k,v in (headers or {}).items(): self.send_header(k,str(v))
        self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=="/healthz": self.sendj(200,{"ok":True}); return
        if u.path=="/__debug/stats": self.sendj(200,STATS); return
        STATS["requests"]+=1
        if u.path!="/items": self.sendj(404,{"error":"NOT_FOUND"}); return
        STATS["items"]+=1
        if self.headers.get("X-API-Key")!="secret": self.sendj(401,{"error":"UNAUTHORIZED"}); return
        q=parse_qs(u.query); limit=int(q.get("limit",["100"])[0])
        if limit>100: self.sendj(400,{"error":"INVALID_LIMIT"}); return
        n=STATS["items"]
        if SCENARIO=="ratelimited" and n==1: self.sendj(429,{"error":"RATE_LIMIT"},{"Retry-After":"3"}); return
        if SCENARIO=="flaky500" and n<=2: self.sendj(500,{"error":"UPSTREAM"}); return
        if SCENARIO=="badjson":
            raw=b"{not-json"; self.send_response(200); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if SCENARIO=="paginated":
            cursor=q.get("cursor",[None])[0]
            start=0 if cursor is None else max(0,int(cursor)-1)
            page=DATA[start:start+limit]
            end=start+len(page)
            nxt=None if end>=len(DATA) else str(end)
            self.sendj(200,{"items":page,"next_cursor":nxt}); return
        self.sendj(200,{"items":DATA[:3],"next_cursor":None})
    def do_POST(self):
        STATS["requests"]+=1; STATS["subscribe"]+=1
        if self.path!="/subscribe": self.sendj(404,{"error":"NOT_FOUND"}); return
        if self.headers.get("X-API-Key")!="secret": self.sendj(401,{"error":"UNAUTHORIZED"}); return
        if not self.headers.get("Idempotency-Key"): self.sendj(409,{"error":"IDEMPOTENCY_REQUIRED"}); return
        self.sendj(201,{"ok":True})

def main():
    port=int(sys.argv[1]); ThreadingHTTPServer(("127.0.0.1",port),H).serve_forever()
if __name__=="__main__": main()
