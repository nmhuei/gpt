from csvnorm import normalize_row_ingest, normalize_row_export, normalize_row_backfill

def test_three_paths_agree_on_simple_row():
    row={" Name ":" Alice ","Empty":""}
    vals=[f(row) for f in (normalize_row_ingest,normalize_row_export,normalize_row_backfill)]
    assert vals[0]==vals[1]==vals[2]=={"name":"Alice","empty":None}
