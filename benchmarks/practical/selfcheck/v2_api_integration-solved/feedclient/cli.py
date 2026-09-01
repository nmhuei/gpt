import argparse,json
from .client import FeedClient
from .pager import iterate_items

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True); p=sub.add_parser("pull"); p.add_argument("--base-url",required=True); p.add_argument("--api-key",required=True); p.add_argument("--since"); p.add_argument("--out",required=True); a=ap.parse_args()
    c=FeedClient(a.base_url,a.api_key)
    with open(a.out,"w",encoding="utf-8") as f:
        for item in iterate_items(c,a.since): f.write(json.dumps(item,sort_keys=True)+"\n")
if __name__=="__main__": main()
