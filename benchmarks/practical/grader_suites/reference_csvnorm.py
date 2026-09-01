#!/usr/bin/env python3
import csv,json,re,sys,unicodedata

def key(k):
    text=unicodedata.normalize("NFKD",str(k)).encode("ascii","ignore").decode("ascii").strip().lower()
    return re.sub(r"[^a-z0-9]+","_",text).strip("_")
def val(v):
    if v is None:return None
    text=unicodedata.normalize("NFC",str(v)).strip(); return None if text=="" else text
def main():
    src,dst=sys.argv[1:3]
    with open(src,encoding="utf-8",newline="") as f: rows=list(csv.DictReader(f))
    with open(dst,"w",encoding="utf-8",newline="") as out:
        for row in rows: out.write(json.dumps({key(k):val(v) for k,v in row.items()},sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n")
if __name__=="__main__":main()
