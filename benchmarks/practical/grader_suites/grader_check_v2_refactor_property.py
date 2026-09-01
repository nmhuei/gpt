import json,re
import pytest
from csvnorm.core import normalize_key,normalize_value,normalize_row
from csvnorm.ingest import normalize_row_ingest
from csvnorm.export import normalize_row_export
from csvnorm.backfill import normalize_row_backfill

@pytest.mark.parametrize("value,expected",[("  x  ","x"),("",None),("   ",None),("cafe\u0301","café"),(None,None)])
def test_value_contract(value,expected): assert normalize_value(value)==expected
@pytest.mark.parametrize("key,expected",[(" User Name ","user_name"),("A-B","a_b"),(" Café ","cafe")])
def test_key_contract(key,expected): assert normalize_key(key)==expected
def test_three_way_parity():
    rows=[{" User Name ":" Alice ","Empty":""},{"A-B":"cafe\u0301"," X ":" y "}]
    for row in rows:
        got=[f(row) for f in (normalize_row_ingest,normalize_row_export,normalize_row_backfill)]
        assert got[0]==got[1]==got[2]==normalize_row(row)
def test_idempotent_hidden():
    row={" A B ":" cafe\u0301 "," Empty ":""}; assert normalize_row(normalize_row(row))==normalize_row(row)
def test_canonical_bytes_stable():
    row={" B ":"2"," A ":"1"}; a=json.dumps(normalize_row(row),sort_keys=True,ensure_ascii=False,separators=(",",":")); assert a==json.dumps(normalize_row(row),sort_keys=True,ensure_ascii=False,separators=(",",":"))
def test_keys_hidden_safe():
    assert all(re.fullmatch(r"[a-z0-9_]+",k) for k in normalize_row({" A-B ":"1"," C.D ":"2"}))
