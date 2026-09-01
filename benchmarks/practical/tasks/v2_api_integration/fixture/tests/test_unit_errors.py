from feedclient.errors import FeedError,AuthError,RateLimited,InvalidResponse,TransientError

def test_error_hierarchy():
    assert issubclass(AuthError,FeedError) and issubclass(RateLimited,FeedError) and issubclass(InvalidResponse,FeedError) and issubclass(TransientError,FeedError)

def test_rate_limit_carries_delay():
    assert RateLimited(3).after_s==3
