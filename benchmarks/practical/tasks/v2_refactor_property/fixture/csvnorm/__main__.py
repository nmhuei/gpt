import argparse,csv,json
from .ingest import normalize_row_ingest

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--in",dest="src",required=True); ap.add_argument("--out",dest="dst",required=True); a=ap.parse_args()
    with open(a.src,encoding="utf-8",newline="") as f: rows=list(csv.DictReader(f))
    with open(a.dst,"w",encoding="utf-8",newline="") as out:
        for row in rows:
            obj=normalize_row_ingest(row)
            out.write(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n")
if __name__=="__main__": main()
