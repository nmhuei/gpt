def allocate_cents(total_cents: int, weights: list[int]) -> list[int]:
    """Allocate by largest remainder, stable by original index."""
    if not weights:
        return []
    if total_cents < 0 or any(w < 0 for w in weights):
        raise ValueError("total and weights must be non-negative")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive value")
    numerators = [total_cents * w for w in weights]
    shares = [n // total_weight for n in numerators]
    remainder = total_cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-(numerators[i] % total_weight), i))
    for i in order[:remainder]:
        shares[i] += 1
    return shares
