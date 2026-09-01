#!/usr/bin/env python3
import argparse,csv,random,sys

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--seed",type=int,required=True); ap.add_argument("--rows",type=int,required=True); a=ap.parse_args()
    r=random.Random(a.seed); w=csv.writer(sys.stdout,lineterminator="\n"); w.writerow([" User ID "," Display-Name ","Note","Empty"]); vals=["  x  ","café","cafe\u0301"," alpha ","beta","  spaced  "]
    for i in range(a.rows): w.writerow([i, f" User {r.randrange(9999)} ", r.choice(vals), ""] )
if __name__=="__main__": main()
