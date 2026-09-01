def allocate_cents(total_cents: int, weights: list[int]) -> list[int]:
    """Allocate with largest remainder; ties favor the lower index."""
    if not weights:
        return []
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive value")
    shares = [total_cents * w // total_weight for w in weights]
    shares[0] += total_cents - sum(shares)
    return shares
