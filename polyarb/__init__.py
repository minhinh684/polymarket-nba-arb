"""polyarb: read-only study of arbitrage in Polymarket NBA markets (replication of arXiv:2605.00864).

Stage 0  record     -> raw order-book events to disk (public feeds, no account, no keys)
Stage 1  replay     -> rebuild books, run the paper's detectors, simulate execution frictions
Stage 2  shadow     -> same engine live, logging hypothetical orders only (no orders are ever sent)
Stage 3  (closed)   -> see docs/STAGE3.md; nothing in this package can place an order.
"""
__version__ = "0.1.0"
