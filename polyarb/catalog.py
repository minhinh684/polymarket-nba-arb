"""What exists on Polymarket for an NBA game: events, markets, tokens, and how they pay out.

A *token* is one side of a market (e.g. the "Lakers" share of the moneyline). Each token pays
$1 if its outcome happens and $0 otherwise. Two tokens are an arbitrage if, together, they pay
at least $1 in EVERY possible final score but cost less than $1 to buy.

To check "pays at least $1 in every possible score" without hand-written rules, every token gets a
payoff vector over a grid of possible final margins (or total points). Two tokens cover the game
when the sum of their vectors is >= 1 everywhere. That one idea covers all of:
  * moneyline + the opposite side of a spread          (the paper's combinatorial arbitrage)
  * two spread lines from the same game                (my extension: a "ladder")
  * two totals lines from the same game                (my extension)
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import numpy as np

# Final margins from the point of view of "team A" (first outcome of the moneyline). A basketball
# game cannot end in a tie, so 0 is excluded.
MARGIN_GRID = np.array([d for d in range(-100, 101) if d != 0])
TOTAL_GRID = np.arange(0, 401)


@dataclass
class Market:
    market_id: str
    condition_id: str
    event_slug: str
    question: str
    mtype: str                       # Polymarket's sportsMarketType, e.g. "moneyline", "spreads"
    period: str                      # "full" or "1h"
    line: float | None
    outcomes: list[str]
    token_ids: list[str]
    accepting_orders: bool = True
    closed: bool = False
    min_size: float = 5.0
    tick: float = 0.001
    fee_rate: float = 0.05
    fee_exponent: float = 1.0
    fee_taker_only: bool = True
    rebate_rate: float = 0.15
    seconds_delay: float = 0.0
    neg_risk: bool = False
    game_start_ms: float | None = None


@dataclass
class Event:
    slug: str
    title: str
    game_id: str | None
    start_ms: float | None
    live: bool = False
    ended: bool = False
    period: str = ""
    score: str = ""
    elapsed: str = ""
    finished_ms: float | None = None        # exact end time from Polymarket, once known
    ended_seen_ms: float | None = None      # when OUR polling first saw ended=True (fallback)
    teams: list[str] = field(default_factory=list)
    market_ids: list[str] = field(default_factory=list)

    def end_ms(self) -> float | None:
        return self.finished_ms or self.ended_seen_ms


@dataclass(frozen=True)
class TokenRef:
    token_id: str
    market_id: str
    outcome_index: int


@dataclass(frozen=True, eq=False)      # eq=False -> cheap identity hash (pairs are shared objects)
class ComboPair:
    """Two tokens from different markets of the same game that together always pay >= $1."""
    key: str
    kind: str                # "ml_spread" | "spread_ladder" | "totals_ladder"
    event_slug: str
    period: str
    token_a: str
    token_b: str
    market_a: str
    market_b: str
    guaranteed: int          # payout in dollars no matter what happens (1)
    max_payoff: int          # payout in the best case (2 if the "middle" lands)


class Catalog:
    def __init__(self) -> None:
        self.events: dict[str, Event] = {}
        self.markets: dict[str, Market] = {}
        self.tokens: dict[str, TokenRef] = {}

    # ---- building ----
    def add_event(self, ev: Event, markets: Iterable[Market]) -> None:
        old = self.events.get(ev.slug)
        if old is not None:
            # keep facts we learned earlier that the API may stop returning
            ev.ended_seen_ms = ev.ended_seen_ms or old.ended_seen_ms
            ev.finished_ms = ev.finished_ms or old.finished_ms
            if ev.ended and ev.ended_seen_ms is None:
                ev.ended_seen_ms = _now_ms()
        elif ev.ended and ev.ended_seen_ms is None:
            ev.ended_seen_ms = _now_ms()
        self.events[ev.slug] = ev
        ev.market_ids = []
        for m in markets:
            self.markets[m.market_id] = m
            ev.market_ids.append(m.market_id)
            for i, t in enumerate(m.token_ids):
                self.tokens[t] = TokenRef(t, m.market_id, i)

    def market_of(self, token_id: str) -> Market | None:
        ref = self.tokens.get(token_id)
        return self.markets.get(ref.market_id) if ref else None

    def event_of_market(self, market_id: str) -> Event | None:
        m = self.markets.get(market_id)
        return self.events.get(m.event_slug) if m else None

    def phase_at(self, event_slug: str, t_ms: float) -> str:
        """'pre' before tip-off, 'in' during play, 'post' after the game is finished."""
        ev = self.events.get(event_slug)
        if ev is None or ev.start_ms is None:
            return "unknown"
        if t_ms < ev.start_ms:
            return "pre"
        end = ev.end_ms()
        if end is not None and t_ms >= end:
            return "post"
        return "in"

    # ---- combinatorial structure ----
    def combo_pairs(self) -> list[ComboPair]:
        pairs: list[ComboPair] = []
        for ev in self.events.values():
            pairs.extend(_event_pairs(ev, [self.markets[m] for m in ev.market_ids if m in self.markets]))
        return pairs

    # ---- persistence ----
    def to_json(self) -> str:
        return json.dumps(
            {"events": [asdict(e) for e in self.events.values()],
             "markets": [asdict(m) for m in self.markets.values()]},
            indent=1,
        )

    @staticmethod
    def from_json(text: str) -> "Catalog":
        raw = json.loads(text)
        cat = Catalog()
        by_event: dict[str, list[Market]] = {}
        for m in raw["markets"]:
            by_event.setdefault(m["event_slug"], []).append(Market(**m))
        for e in raw["events"]:
            ev = Event(**e)
            cat.events[ev.slug] = ev
            for m in by_event.get(ev.slug, []):
                cat.markets[m.market_id] = m
                for i, t in enumerate(m.token_ids):
                    cat.tokens[t] = TokenRef(t, m.market_id, i)
        return cat


def _now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000.0


# ----------------------------------------------------------------------------------------------
# payoff vectors
# ----------------------------------------------------------------------------------------------
def _is_half_point(x: float | None) -> bool:
    return x is not None and abs(x * 2 - round(x * 2)) < 1e-9 and abs(x - round(x)) > 1e-9


def margin_payoff(ml: Market, mk: Market, idx: int) -> np.ndarray | None:
    """1/0 payoff of token `idx` of market `mk` for each final margin d = points(A) - points(B),
    where A is the first outcome of the moneyline `ml`. Returns None if we can't represent it
    (whole-number spread lines can 'push', and then the payout rules differ, so we skip them)."""
    A, B = ml.outcomes[0], ml.outcomes[1]
    g = MARGIN_GRID
    if mk.mtype.endswith("moneyline"):
        team = mk.outcomes[idx]
        if team == A:
            return g > 0
        if team == B:
            return g < 0
        return None
    if mk.mtype.endswith("spreads"):
        if not _is_half_point(mk.line):
            return None
        t0 = mk.outcomes[0]
        line = float(mk.line)
        # outcome 0 = team t0 covering `line` (a negative line means t0 is the favourite)
        if t0 == A:
            return (g > -line) if idx == 0 else (g < -line)
        if t0 == B:
            return (g < line) if idx == 0 else (g > line)
        return None
    return None


def total_payoff(mk: Market, idx: int) -> np.ndarray | None:
    if not mk.mtype.endswith("totals") or not _is_half_point(mk.line):
        return None
    line = float(mk.line)
    out = mk.outcomes[idx].lower()
    if out.startswith("over"):
        return TOTAL_GRID > line
    if out.startswith("under"):
        return TOTAL_GRID < line
    return None


def _event_pairs(ev: Event, markets: list[Market]) -> list[ComboPair]:
    pairs: list[ComboPair] = []
    for period in ("full", "1h"):
        ms = [m for m in markets if m.period == period and len(m.token_ids) == 2]
        mls = [m for m in ms if m.mtype.endswith("moneyline")]
        # --- margin-based pairs: moneyline and spreads ---
        if mls:
            ml = mls[0]
            legs = []
            for m in ms:
                if m.mtype.endswith("moneyline") or m.mtype.endswith("spreads"):
                    for i in (0, 1):
                        v = margin_payoff(ml, m, i)
                        if v is not None:
                            legs.append((m, i, v.astype(int)))
            pairs.extend(_cover(ev.slug, period, legs, "margin"))
        # --- total-points pairs ---
        legs = []
        for m in ms:
            if m.mtype.endswith("totals"):
                for i in (0, 1):
                    v = total_payoff(m, i)
                    if v is not None:
                        legs.append((m, i, v.astype(int)))
        pairs.extend(_cover(ev.slug, period, legs, "total"))
    return pairs


def _cover(slug: str, period: str, legs, variable: str) -> list[ComboPair]:
    out: list[ComboPair] = []
    for a in range(len(legs)):
        for b in range(a + 1, len(legs)):
            ma, ia, va = legs[a]
            mb, ib, vb = legs[b]
            if ma.market_id == mb.market_id:
                continue                          # same market = the single-market detector's job
            s = va + vb
            if s.min() < 1:
                continue                          # some final score leaves us with nothing
            if variable == "total":
                kind = "totals_ladder"
            elif ma.mtype.endswith("spreads") and mb.mtype.endswith("spreads"):
                kind = "spread_ladder"
            else:
                kind = "ml_spread"
            ta, tb = ma.token_ids[ia], mb.token_ids[ib]
            out.append(ComboPair(
                key=f"{slug}|{period}|{kind}|{ta[-8:]}+{tb[-8:]}", kind=kind, event_slug=slug,
                period=period, token_a=ta, token_b=tb, market_a=ma.market_id, market_b=mb.market_id,
                guaranteed=int(s.min()), max_payoff=int(s.max()),
            ))
    return out
