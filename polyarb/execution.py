"""A deliberately pessimistic model of what happens AFTER a detector fires.

The paper counts an arbitrage when prices cross. This module asks the harder question: if a bot had
reacted, what would it really have got? It models, step by step:

  1. reaction time   our code + the network delay before the order reaches Polymarket's matching engine
  2. the book moves  by the time the order arrives the quote may be gone (we replay the REAL book)
  3. two legs        each leg is its own order on its own market; one can fill while the other does not
  4. leg failure     if only one leg fills we hold a bet on a game, so we try to sell it back (taker)
  5. fees            takers pay shares x rate x price x (1 - price) on every fill; makers pay nothing

Nothing here talks to the network and nothing here can place an order. It only reads recorded books.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field

from . import config

SCALE = config.PRICE_SCALE


def taker_fee(shares: float, price: float, rate: float, exponent: float = 1.0) -> float:
    """Polymarket sports taker fee in dollars: shares x rate x (p x (1 - p)) ** exponent.
    Check against the docs' example: 100 shares at $0.50 with rate 0.05 -> $1.25."""
    return shares * rate * (price * (1.0 - price)) ** exponent


def walk(levels, shares: float):
    """Take up to `shares` from price levels listed in execution order.
    Returns a list of (price_micro, taken_shares)."""
    out, left = [], shares
    for price, size in levels:
        if left <= 1e-12:
            break
        take = min(size, left)
        if take > 0:
            out.append((price, take))
            left -= take
    return out


@dataclass
class Scenario:
    name: str
    latency_ms: float                       # decision -> order arrives at the matching engine
    order_type: str = "FOK"                 # FOK: all-or-nothing per leg. FAK: take whatever is there.
    fee_rate: float | None = None           # None -> each market's own rate (read from the API)
    delay_s: float | None = None            # None -> the market's own secondsDelay (0 for NBA right now)
    unwind_latency_ms: float | None = None  # None -> same as latency_ms
    cap_usd: float = config.CAP_USD
    daily_loss_cap_usd: float = config.DAILY_LOSS_CAP_USD
    compute_ms: float = config.COMPUTE_MS


@dataclass
class Trade:
    scenario: str
    episode_key: str
    kind: str
    event_slug: str
    phase: str
    detected_ms: float
    arrival_ms: float
    wanted_shares: float
    fills: list = field(default_factory=list)       # [(token, shares, vwap)]
    cost: float = 0.0
    fees: float = 0.0
    matched_shares: float = 0.0
    guaranteed: int = 1
    unwind_proceeds: float = 0.0
    unwind_fees: float = 0.0
    stuck_shares: float = 0.0
    stuck_value: float = 0.0
    outcome: str = "pending"                        # no_fill | balanced | leg_failure
    pnl: float = 0.0
    detected_capped_profit: float = 0.0             # what the paper would have counted
    censored: bool = False

    def finalize(self) -> None:
        self.pnl = (self.guaranteed * self.matched_shares - self.cost - self.fees
                    + self.unwind_proceeds - self.unwind_fees + self.stuck_value)


class ScenarioSim:
    """One simulated trader with its own latency, fee assumption and risk limits."""

    def __init__(self, scenario: Scenario, engine) -> None:
        self.sc = scenario
        self.eng = engine
        self.trades: list[Trade] = []
        self.skipped: dict[str, int] = {}
        self._heap: list = []
        self._seq = itertools.count()
        self._daily: dict[str, float] = {}

    # ---- called by the engine ----
    def on_signal(self, ep, sig) -> None:
        """A new qualifying arbitrage just appeared. Decide whether to react, and schedule the order."""
        if self.eng.killed:
            return self._skip("kill_switch")
        if sig.path != "buy":                       # minting needs an on-chain step we don't model
            return self._skip("not_simulated_mint_sell")
        day = _utc_day(ep.start_recv_ms)
        if self._daily.get(day, 0.0) <= -self.sc.daily_loss_cap_usd:
            return self._skip("daily_loss_cap")
        cost_per_pair = sum(l.price_micro for l in sig.legs) / SCALE
        shares = min(sig.shares, self.sc.cap_usd / cost_per_pair)
        shares = float(int(shares))                 # whole shares
        min_size = max(self.eng.cat.markets[l.market_id].min_size for l in sig.legs)
        if shares < min_size:
            return self._skip("below_min_order_size")
        delay = self.sc.delay_s
        if delay is None:
            delay = max(self.eng.cat.markets[l.market_id].seconds_delay for l in sig.legs)
        arrival = ep.start_recv_ms + self.sc.compute_ms + self.sc.latency_ms + delay * 1000.0
        tr = Trade(self.sc.name, ep.key, ep.kind, ep.event_slug, ep.phase, ep.start_recv_ms, arrival, shares,
                   guaranteed=sig.guaranteed,
                   detected_capped_profit=sig.edge_micro / SCALE * shares)
        heapq.heappush(self._heap, (arrival, next(self._seq), "entry", tr, sig, None))
        self.eng.note_due(arrival)

    def advance(self, now_ms: float, final: bool = False) -> None:
        while self._heap and self._heap[0][0] <= now_ms:
            arrival, _, what, tr, sig, extra = heapq.heappop(self._heap)
            if what == "entry":
                self._entry(arrival, tr, sig)
            else:
                self._unwind(arrival, tr, extra)
        if final:                                   # data ended before these arrived: don't pretend
            while self._heap:
                _, _, _, tr, _, _ = heapq.heappop(self._heap)
                tr.censored = True
                tr.outcome = "censored"
                self.trades.append(tr)

    # ---- internals ----
    def _skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def _fee_rate(self, market) -> float:
        return market.fee_rate if self.sc.fee_rate is None else self.sc.fee_rate

    def _entry(self, arrival: float, tr: Trade, sig) -> None:
        fills = []
        for leg in sig.legs:
            book = self.eng.books.get(leg.token_id)
            market = self.eng.cat.markets[leg.market_id]
            taken = []
            if book is not None and book.synced:
                avail = book.asks_up_to(leg.price_micro)         # never pay more than the price we saw
                total = sum(s for _, s in avail)
                want = tr.wanted_shares
                if self.sc.order_type == "FOK":
                    want = want if total + 1e-9 >= want else 0.0
                else:
                    want = min(want, total)
                taken = walk(avail, want) if want > 0 else []
            shares = sum(t for _, t in taken)
            spent = sum(p / SCALE * t for p, t in taken)
            fee = sum(taker_fee(t, p / SCALE, self._fee_rate(market), market.fee_exponent) for p, t in taken)
            fills.append((leg.token_id, shares, spent, fee))
        tr.fills = [(t, s, (c / s if s else 0.0)) for t, s, c, _ in fills]
        tr.cost = sum(c for _, _, c, _ in fills)
        tr.fees = sum(f for _, _, _, f in fills)
        s0, s1 = fills[0][1], fills[1][1]
        tr.matched_shares = min(s0, s1)
        if s0 == 0 and s1 == 0:
            tr.outcome = "no_fill"
            tr.cost = tr.fees = 0.0
            return self._finish(tr)
        if abs(s0 - s1) < 1e-9:
            tr.outcome = "balanced"
            return self._finish(tr)
        tr.outcome = "leg_failure"
        over = 0 if s0 > s1 else 1
        excess = abs(s0 - s1)
        lat = self.sc.unwind_latency_ms if self.sc.unwind_latency_ms is not None else self.sc.latency_ms
        when = arrival + self.sc.compute_ms + lat
        heapq.heappush(self._heap, (when, next(self._seq), "unwind", tr, sig, (sig.legs[over], excess)))
        self.eng.note_due(when)

    def _unwind(self, now_ms: float, tr: Trade, extra) -> None:
        leg, excess = extra
        book = self.eng.books.get(leg.token_id)
        market = self.eng.cat.markets[leg.market_id]
        sold = []
        if book is not None and book.synced:
            sold = walk(book.bids_down_to(0), excess)
        got = sum(t for _, t in sold)
        tr.unwind_proceeds = sum(p / SCALE * t for p, t in sold)
        tr.unwind_fees = sum(taker_fee(t, p / SCALE, self._fee_rate(market), market.fee_exponent) for p, t in sold)
        tr.stuck_shares = max(excess - got, 0.0)
        if tr.stuck_shares > 0 and book is not None and book.best_bid is not None:
            tr.stuck_value = tr.stuck_shares * book.best_bid / SCALE      # mark what's left at the best bid
        self._finish(tr)

    def _finish(self, tr: Trade) -> None:
        tr.finalize()
        self.trades.append(tr)
        day = _utc_day(tr.arrival_ms)
        self._daily[day] = self._daily.get(day, 0.0) + tr.pnl
        if self.eng.on_trade_hook:
            self.eng.on_trade_hook(tr)


def _utc_day(ms: float) -> str:
    import time
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000.0))


def default_scenarios(measured_rtt_ms: float | None = None) -> list[Scenario]:
    """A small grid. 'fast' is optimistic (a server next to Polymarket), 'home' is a laptop on a
    a long-distance connection. If we measured our own REST round trip, that one becomes the 'measured' scenario."""
    out = [
        Scenario("fast_200ms", latency_ms=200, order_type="FOK"),
        Scenario("home_800ms", latency_ms=800, order_type="FOK"),
        Scenario("home_800ms_FAK", latency_ms=800, order_type="FAK"),
        Scenario("slow_1500ms", latency_ms=1500, order_type="FOK"),
        Scenario("paper_nofee_800ms", latency_ms=800, order_type="FOK", fee_rate=0.0),
    ]
    if measured_rtt_ms:
        out.append(Scenario("measured_rtt", latency_ms=measured_rtt_ms * 1.5, order_type="FOK"))
    return out
