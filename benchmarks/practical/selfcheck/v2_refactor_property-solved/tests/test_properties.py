import json,re,subprocess,sys
from pathlib import Path
from csvnorm.core import normalize_key, normalize_row, normalize_value

def test_idempotent():
    row={" User Name ":" cafe\u0301 ","Empty":""}
    assert normalize_row(normalize_row(row))==normalize_row(row)

def test_keys_safe():
    for key in [" User Name ","X-Y","A.B"," spaces  here "]:
        assert re.fullmatch(r"[a-z0-9_]+",normalize_key(key))

def test_empty_none_and_strip():
    assert normalize_value("")==None
    assert normalize_value("   ")==None
    assert normalize_value("  x ")=="x"

def test_nfc_sentinel():
    assert normalize_value("cafe\u0301")=="café"

def test_canonical_json_stable():
    row={" B ":"2"," A ":"1"}; a=json.dumps(normalize_row(row),sort_keys=True,ensure_ascii=False,separators=(",",":")); b=json.dumps(normalize_row(row),sort_keys=True,ensure_ascii=False,separators=(",",":")); assert a==b

def test_generator_is_deterministic():
    tool=Path(__file__).parents[1]/"tools"/"gen_rows.py"
    a=subprocess.check_output([sys.executable,str(tool),"--seed","3","--rows","5"]); b=subprocess.check_output([sys.executable,str(tool),"--seed","3","--rows","5"]); assert a==b
