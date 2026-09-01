import re
import unicodedata

def _key(key):
    text=unicodedata.normalize("NFKD", str(key)).encode("ascii","ignore").decode("ascii").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")

def _value(value):
    if value is None: return None
    text=unicodedata.normalize("NFC", str(value)).strip()
    return None if text == "" else text

def normalize_row_backfill(row):
    return {_key(k): _value(v) for k,v in row.items()}
