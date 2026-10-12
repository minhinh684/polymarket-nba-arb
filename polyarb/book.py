"""A limit order book rebuilt from Polymarket's feed.

The feed sends a full `book` snapshot when you subscribe, then `price_change` messages. Each
entry in a `price_change` says "the TOTAL size resting at this price on this side is now X"
(X = 0 means the level is gone). Prices are kept as integers in millionths of a dollar.
"""
from __future__ import annotations

from . import config


_MICRO_CACHE: dict = {}


def to_micro(price) -> int:
    """'0.995' -> 995000. The same few hundred price strings repeat millions of times, so cache them."""
    v = _MICRO_CACHE.get(price)
    if v is None:
        v = int(round(float(price) * config.PRICE_SCALE))
        if len(_MICRO_CACHE) < 200_000:
            _MICRO_CACHE[price] = v
    return v


class Book:
    __slots__ = ("bids", "asks", "best_bid", "best_ask", "synced", "server_ms")

    def __init__(self) -> None:
        self.bids: dict[int, float] = {}
        self.asks: dict[int, float] = {}
        self.best_bid: int | None = None
        self.best_ask: int | None = None
        self.synced = False
        self.server_ms: float | None = None

    # ---- updates ----
    def reset(self, bids, asks, server_ms: float | None = None) -> None:
        self.bids = {to_micro(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {to_micro(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}
        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None
        self.synced = True
        self.server_ms = server_ms

    def set_level(self, side: str, price_micro: int, size: float) -> None:
        """side is 'BUY' (a bid) or 'SELL' (an ask), as in the feed."""
        if side == "BUY":
            if size <= 0:
                if self.bids.pop(price_micro, None) is not None and price_micro == self.best_bid:
                    self.best_bid = max(self.bids) if self.bids else None
            else:
                self.bids[price_micro] = size
                if self.best_bid is None or price_micro > self.best_bid:
                    self.best_bid = price_micro
        else:
            if size <= 0:
                if self.asks.pop(price_micro, None) is not None and price_micro == self.best_ask:
                    self.best_ask = min(self.asks) if self.asks else None
            else:
                self.asks[price_micro] = size
                if self.best_ask is None or price_micro < self.best_ask:
                    self.best_ask = price_micro

    def invalidate(self) -> None:
        """Forget everything (used after a reconnect: a fresh snapshot is on its way)."""
        self.bids.clear(); self.asks.clear()
        self.best_bid = self.best_ask = None
        self.synced = False

    # ---- reading ----
    def ask_size(self) -> float:
        return self.asks.get(self.best_ask, 0.0) if self.best_ask is not None else 0.0

    def bid_size(self) -> float:
        return self.bids.get(self.best_bid, 0.0) if self.best_bid is not None else 0.0

    def asks_up_to(self, limit_micro: int):
        """Ask levels a buyer could hit with a limit price of `limit_micro`, cheapest first."""
        return [(p, self.asks[p]) for p in sorted(self.asks) if p <= limit_micro]

    def bids_down_to(self, limit_micro: int):
        """Bid levels a seller could hit with a limit price of `limit_micro`, best (highest) first."""
        return [(p, self.bids[p]) for p in sorted(self.bids, reverse=True) if p >= limit_micro]
