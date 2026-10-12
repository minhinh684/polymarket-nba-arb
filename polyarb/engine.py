"""The heart of stages 1 and 2: rebuild order books from the feed, spot arbitrage, track how long it lasts.

The SAME class runs in both stages. In replay it is fed recorded messages; in shadow mode it is fed live
messages. Only the source of the messages differs, so a shadow run and a replay of its own recording
produce the same answer (a good test that the two stages agree).
"""
from __future__ import annotations

import collections
import statistics
from dataclasses import asdict, dataclass, field

from . import config
from .book import Book, to_micro
from .catalog import Catalog, ComboPair
from .execution import Scenario, ScenarioSim

SCALE = config.PRICE_SCALE


@dataclass
class Leg:
    token_id: str
    market_id: str
    price_micro: int          # best ask (buy path) or best bid (mint-and-sell path) at detection
    size: float               # shares resting at that price


@dataclass
class Signal:
    key: str
    kind: str                 # single | ml_spread | spread_ladder | totals_ladder
    event_slug: str
    legs: list[Leg]
    edge_micro: int           # guaranteed payout minus cost, per share pair, in millionths of a dollar
    guaranteed: int
    max_payoff: int
    path: str                 # buy | mint_sell
    shares: float             # bottleneck size across the two legs

    @property
    def pair_cost(self) -> float:
        return sum(l.price_micro for l in self.legs) / SCALE


@dataclass
class Episode:
    key: str
    kind: str
    event_slug: str
    path: str
    phase: str
    start_recv_ms: float
    start_server_ms: float | None
    edge_start: float                  # $ per share pair at the first qualifying state
    edge_max: float
    shares_start: float
    shares_max: float
    cost_start: float
    guaranteed: int
    max_payoff: int
    legs_start: list = field(default_factory=list)
    n_updates: int = 1
    end_recv_ms: float | None = None
    end_server_ms: float | None = None
    phase_end: str = ""
    reason_closed: str = ""
    max_capped_profit: float = 0.0     # the paper's "one-shot" profit: best snapshot, $100 budget

    @property
    def duration_ms(self) -> float | None:
        return None if self.end_recv_ms is None else self.end_recv_ms - self.start_recv_ms


class Engine:
    def __init__(self, catalog: Catalog, scenarios: list[Scenario] | None = None, *,
                 min_notional: float = config.MIN_NOTIONAL_USD, cap_usd: float = config.CAP_USD) -> None:
        self.cat = catalog
        self.min_notional = min_notional
        self.cap_usd = cap_usd
        self.books: dict[str, Book] = {}
        self.pairs_by_token: dict[str, list[ComboPair]] = collections.defaultdict(list)
        self._catalog_sig: frozenset | None = None
        self.set_catalog(catalog)
        self.active: dict[str, Episode] = {}
        self._refs: dict[str, object] = {}
        self.episodes: list[Episode] = []
        self._dust: set[str] = set()
        self.dust_signals: collections.Counter = collections.Counter()
        self.integrity: collections.Counter = collections.Counter()
        self.rtt_ms: list[float] = []
        self.msg_counts: collections.Counter = collections.Counter()
        self.first_ms: float | None = None
        self.last_ms: float = 0.0
        self._next_due = float("inf")            # earliest simulated order arrival (fast path for advance())
        self._skeys: dict[str, str] = {}
        self.pair_evals = 0
        self.single_evals = 0
        self.sims: list[ScenarioSim] = [ScenarioSim(sc, self) for sc in (scenarios or [])]
        self.on_episode_start = None            # optional hooks (shadow mode logs with these)
        self.on_episode_end = None
        self.on_trade_hook = None
        self.killed = False                     # kill switch: True -> no NEW hypothetical orders
        self.ev_stats: dict[str, dict] = {}     # per game: how much data we really have
        self.min_slack: dict[str, int] = {}     # per kind: smallest (cost - guaranteed payout) ever seen, micro-$

    def add_scenario(self, sc: Scenario) -> ScenarioSim:
        sim = ScenarioSim(sc, self)
        self.sims.append(sim)
        return sim

    # ------------------------------------------------------------------ catalog
    def set_catalog(self, cat: Catalog) -> None:
        sig = frozenset(cat.markets)
        self.cat = cat
        if sig == self._catalog_sig:
            return
        self._catalog_sig = sig
        self.pairs_by_token = collections.defaultdict(list)
        for p in cat.combo_pairs():
            self.pairs_by_token[p.token_a].append(p)
            self.pairs_by_token[p.token_b].append(p)

    # ------------------------------------------------------------------ feed
    def on_message(self, recv_ms: float, msg: dict) -> None:
        if self.first_ms is None:
            self.first_ms = recv_ms
        self.last_ms = recv_ms
        self.advance(recv_ms)                    # orders that "arrived" earlier see the PRE-message book
        et = msg.get("event_type")
        self.msg_counts[et] += 1
        touched: set[str] = set()
        server_ms = _to_float(msg.get("timestamp"))
        if et == "book":
            aid = msg.get("asset_id")
            if aid:
                self._book(aid).reset(msg.get("bids", []), msg.get("asks", []), server_ms)
                touched.add(aid)
        elif et == "price_change":
            claimed: dict[str, tuple] = {}
            for ch in msg.get("price_changes", []):
                aid = ch.get("asset_id")
                b = self.books.get(aid)
                if b is None or not b.synced:
                    self.integrity["delta_before_snapshot"] += 1
                    continue
                b.set_level(ch["side"], to_micro(ch["price"]), float(ch["size"]))
                touched.add(aid)
                if "best_bid" in ch and "best_ask" in ch:
                    claimed[aid] = (to_micro(ch["best_bid"]), to_micro(ch["best_ask"]))
            for aid, (cb, ca) in claimed.items():     # compare our book with the feed's own claim
                b = self.books[aid]
                mb = b.best_bid if b.best_bid is not None else 0
                ma = b.best_ask if b.best_ask is not None else SCALE
                self.integrity["checked"] += 1
                if mb != cb:
                    self.integrity["bid_mismatch"] += 1
                if ma != ca:
                    self.integrity["ask_mismatch"] += 1
        elif et == "_status":
            ev = self.cat.events.get(msg.get("slug"))
            if ev is not None:
                ev.live, ev.ended = bool(msg.get("live")), bool(msg.get("ended"))
                ev.period, ev.score, ev.elapsed = msg.get("period", ""), msg.get("score", ""), msg.get("elapsed", "")
                if msg.get("finished_ms"):
                    ev.finished_ms = msg["finished_ms"]
                if ev.ended and ev.ended_seen_ms is None:
                    ev.ended_seen_ms = recv_ms
        elif et == "_rtt":
            self.rtt_ms.append(float(msg.get("rtt_ms", 0)))
        elif et == "_meta":
            if msg.get("what") in ("connect", "disconnect"):
                for b in self.books.values():
                    b.invalidate()
                for key in list(self.active):
                    self._reeval(key, recv_ms, server_ms, reason="feed_gap")
            return
        if not touched:
            return
        markets = {self.cat.tokens[t].market_id for t in touched if t in self.cat.tokens}
        for slug in {self.cat.markets[m].event_slug for m in markets if m in self.cat.markets}:
            st = self.ev_stats.setdefault(slug, {"msgs": 0, "in_msgs": 0, "first": recv_ms, "last": recv_ms,
                                                 "in_first": None, "in_last": None})
            st["msgs"] += 1
            st["last"] = recv_ms
            if self.cat.phase_at(slug, recv_ms) == "in":
                st["in_msgs"] += 1
                st["in_first"] = recv_ms if st["in_first"] is None else st["in_first"]
                st["in_last"] = recv_ms
        self.single_evals += len(markets)
        for mid in markets:
            self._check_single(mid, recv_ms, server_ms)
        pairs = {p for t in touched for p in self.pairs_by_token.get(t, ())}
        self.pair_evals += len(pairs)
        for p in pairs:
            self._check_pair(p, recv_ms, server_ms)

    def advance(self, now_ms: float, final: bool = False) -> None:
        if not final and now_ms < self._next_due:
            return
        for sim in self.sims:
            sim.advance(now_ms, final=final)
        self._next_due = min((sim._heap[0][0] for sim in self.sims if sim._heap), default=float("inf"))

    def note_due(self, when_ms: float) -> None:
        if when_ms < self._next_due:
            self._next_due = when_ms

    def finish(self) -> None:
        """Call once at the end of a replay: close open episodes and settle pending orders."""
        for key in list(self.active):
            self._close(key, self.last_ms, None, reason="end_of_data")
        self.advance(self.last_ms, final=True)

    # ------------------------------------------------------------------ detectors
    def _book(self, token_id: str) -> Book:
        b = self.books.get(token_id)
        if b is None:
            b = self.books[token_id] = Book()
        return b

    def _check_single(self, mid: str, recv_ms: float, server_ms) -> None:
        m = self.cat.markets.get(mid)
        if m is None or len(m.token_ids) != 2:
            return
        ta, tb = m.token_ids
        sig = self._single_signal(m, ta, tb)
        key = self._skeys.get(mid)
        if key is None:
            key = self._skeys[mid] = f"single|{mid}"
        if sig is None and key not in self.active and key not in self._dust:
            return
        self._track(key, "single", mid, sig, m.event_slug, recv_ms, server_ms)

    def _single_signal(self, m, ta, tb) -> Signal | None:
        a, b = self.books.get(ta), self.books.get(tb)
        if a is None or b is None or not (a.synced and b.synced):
            return None
        if a.best_ask is not None and b.best_ask is not None:
            slack = a.best_ask + b.best_ask - SCALE
            cur = self.min_slack.get("single")
            if cur is None or slack < cur:
                self.min_slack["single"] = slack
        if a.best_ask is not None and b.best_ask is not None and a.best_ask + b.best_ask < SCALE:
            return Signal(f"single|{m.market_id}", "single", m.event_slug,
                          [Leg(ta, m.market_id, a.best_ask, a.ask_size()), Leg(tb, m.market_id, b.best_ask, b.ask_size())],
                          SCALE - a.best_ask - b.best_ask, 1, 1, "buy", min(a.ask_size(), b.ask_size()))
        # In a mirrored book the two conditions are the same event seen twice; we only keep one.
        if a.best_bid is not None and b.best_bid is not None and a.best_bid + b.best_bid > SCALE:
            return Signal(f"single|{m.market_id}", "single", m.event_slug,
                          [Leg(ta, m.market_id, a.best_bid, a.bid_size()), Leg(tb, m.market_id, b.best_bid, b.bid_size())],
                          a.best_bid + b.best_bid - SCALE, 1, 1, "mint_sell", min(a.bid_size(), b.bid_size()))
        return None

    def _check_pair(self, p: ComboPair, recv_ms: float, server_ms) -> None:
        sig = self._pair_signal(p)
        if sig is None and p.key not in self.active and p.key not in self._dust:
            return
        self._track(p.key, p.kind, p, sig, p.event_slug, recv_ms, server_ms)

    def _pair_signal(self, p: ComboPair) -> Signal | None:
        a, b = self.books.get(p.token_a), self.books.get(p.token_b)
        if a is None or b is None or not (a.synced and b.synced):
            return None
        if a.best_ask is None or b.best_ask is None:
            return None
        cost = a.best_ask + b.best_ask
        slack = cost - p.guaranteed * SCALE
        cur = self.min_slack.get(p.kind)
        if cur is None or slack < cur:
            self.min_slack[p.kind] = slack
        if slack >= 0:
            return None
        return Signal(p.key, p.kind, p.event_slug,
                      [Leg(p.token_a, p.market_a, a.best_ask, a.ask_size()), Leg(p.token_b, p.market_b, b.best_ask, b.ask_size())],
                      p.guaranteed * SCALE - cost, p.guaranteed, p.max_payoff, "buy",
                      min(a.ask_size(), b.ask_size()))

    def _reeval(self, key: str, recv_ms: float, server_ms, reason: str) -> None:
        ref = self._refs.get(key)
        if ref is None:
            return
        if isinstance(ref, ComboPair):
            sig = self._pair_signal(ref)
            slug = ref.event_slug
        else:
            m = self.cat.markets.get(ref)
            sig = self._single_signal(m, *m.token_ids) if m else None
            slug = m.event_slug if m else ""
        self._track(key, "", ref, sig, slug, recv_ms, server_ms, reason=reason)

    # ------------------------------------------------------------------ episode bookkeeping
    def _qualifies(self, sig: Signal) -> bool:
        if sig.edge_micro <= 0:
            return False
        min_size = max(self.cat.markets[l.market_id].min_size for l in sig.legs)
        return sig.shares >= min_size and sig.shares * sig.pair_cost >= self.min_notional

    def _track(self, key, kind, ref, sig, slug, recv_ms, server_ms, reason: str = "state_changed") -> None:
        ep = self.active.get(key)
        if sig is not None and self._qualifies(sig):
            self._dust.discard(key)
            capped = sig.edge_micro / SCALE * min(sig.shares, self.cap_usd / sig.pair_cost)
            if ep is None:
                ep = Episode(
                    key=key, kind=sig.kind, event_slug=slug, path=sig.path,
                    phase=self.cat.phase_at(slug, recv_ms), start_recv_ms=recv_ms, start_server_ms=server_ms,
                    edge_start=sig.edge_micro / SCALE, edge_max=sig.edge_micro / SCALE,
                    shares_start=sig.shares, shares_max=sig.shares, cost_start=sig.pair_cost,
                    guaranteed=sig.guaranteed, max_payoff=sig.max_payoff,
                    legs_start=[asdict(l) for l in sig.legs], max_capped_profit=capped)
                self.active[key] = ep
                self._refs[key] = ref
                for sim in self.sims:
                    sim.on_signal(ep, sig)
                if self.on_episode_start:
                    self.on_episode_start(ep, sig)
            else:
                ep.n_updates += 1
                ep.edge_max = max(ep.edge_max, sig.edge_micro / SCALE)
                ep.shares_max = max(ep.shares_max, sig.shares)
                ep.max_capped_profit = max(ep.max_capped_profit, capped)
            return
        if sig is not None:                                  # crossed, but too small to trade
            if key not in self._dust:
                self._dust.add(key)
                self.dust_signals[sig.kind] += 1
        else:
            self._dust.discard(key)
        if ep is not None:
            self._close(key, recv_ms, server_ms, reason)

    def _close(self, key: str, recv_ms: float, server_ms, reason: str) -> None:
        ep = self.active.pop(key, None)
        self._refs.pop(key, None)
        if ep is None:
            return
        ep.end_recv_ms, ep.end_server_ms, ep.reason_closed = recv_ms, server_ms, reason
        ep.phase_end = self.cat.phase_at(ep.event_slug, recv_ms)
        self.episodes.append(ep)
        if self.on_episode_end:
            self.on_episode_end(ep)

    # ------------------------------------------------------------------ small summary
    def median_rtt_ms(self) -> float | None:
        return statistics.median(self.rtt_ms) if self.rtt_ms else None


def _to_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
