class FeedError(Exception): pass
class AuthError(FeedError): pass
class RateLimited(FeedError):
    def __init__(self,after_s): self.after_s=float(after_s); super().__init__(f"rate limited for {after_s}s")
class InvalidResponse(FeedError):
    def __init__(self,message,status=None): self.status=status; super().__init__(message)
class TransientError(FeedError): pass
