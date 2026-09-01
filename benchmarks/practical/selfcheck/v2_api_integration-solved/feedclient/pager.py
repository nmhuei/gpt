def iterate_items(client,since=None):
    cursor=None; seen=set()
    while True:
        page=client.fetch_page(cursor=cursor,since=since)
        for item in page["items"]:
            ident=item.get("id")
            if ident in seen: continue
            seen.add(ident); yield item
        cursor=page.get("next_cursor")
        if cursor is None: break
