# Feed API
GET /items?cursor&limit returns {items:[...], next_cursor:string|null}; limit must be <=100. Header X-API-Key is required. 401 is never retried. 429 includes Retry-After and must be retried using injected sleep. 5xx retries use 0.1*2^k. POST /subscribe requires Idempotency-Key. Pagination de-duplicates ids across page boundaries.
