import re
import unicodedata

def normalize_key(key):
    text=unicodedata.normalize("NFKD",str(key)).encode("ascii","ignore").decode("ascii").strip().lower()
    return re.sub(r"[^a-z0-9]+","_",text).strip("_")

def normalize_value(value):
    if value is None: return None
    text=unicodedata.normalize("NFC",str(value)).strip()
    return None if text == "" else text

def normalize_row(row):
    return {normalize_key(k): normalize_value(v) for k,v in row.items()}
