def items(n=250):
    return [{"id":str(i),"published_at":f"2026-01-01T00:{i%60:02d}:00Z","title":f"item-{i}"} for i in range(n)]
