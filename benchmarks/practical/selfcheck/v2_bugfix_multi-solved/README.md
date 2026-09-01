# orderkit

## Pricing contract
All arithmetic uses integer cents. Percentage discounts stack by ADDING basis
points, capped at 10,000, and are applied once with integer floor. Flat
discounts are subtracted after percentage discounts; totals clamp at zero.
Allocation uses largest remainder with ties resolved by lower original index.
