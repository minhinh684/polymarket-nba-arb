"""Unit tests. Run:  python3 -m unittest discover -s tests -v

They use made-up games and hand-written order books so that every expected number can be checked
with a calculator. (The one real-data check lives in the README: the book-integrity percentage.)
"""
import os
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from polyarb import gamma, gate, report                                        # noqa: E402
from polyarb.book import Book, to_micro                                        # noqa: E402
from polyarb.catalog import Catalog, Event, Market                             # noqa: E402
from polyarb.engine import Engine                                              # noqa: E402
from polyarb.execution import Scenario, taker_fee                              # noqa: E402
from polyarb.logio import LogWriter, iter_log, list_logs                       # noqa: E402

START = 1_000_000.0          # tip-off (ms)
T0 = 2_000_000.0             # a moment during the game


def mk(mid, slug, mtype, line, outs, toks, period="full"):
    return Market(mid, "c" + mid, slug, f"{mtype} {line}", mtype, period, line, outs, toks)


def make_catalog(end_ms=None, extra=()):
    cat = Catalog()
    ev = Event(slug="g", title="Jazz vs Nuggets", game_id="1", start_ms=START, finished_ms=end_ms,
               teams=["Jazz", "Nuggets"])
    mkts = [
        mk("ml", "g", "moneyline", None, ["Jazz", "Nuggets"], ["tJ", "tN"]),
        mk("sp45", "g", "spreads", -4.5, ["Nuggets", "Jazz"], ["sN45", "sJ45"]),
        mk("to", "g", "totals", 220.5, ["Over", "Under"], ["oO", "oU"]),
    ]
    mkts.extend(extra)
    cat.add_event(ev, mkts)
    return cat


def book(token, bids=(), asks=(), ts=0):
    return {"event_type": "book", "asset_id": token, "timestamp": str(ts),
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


def pc(token, side, price, size, ts=0, claim=None):
    ch = {"asset_id": token, "price": str(price), "size": str(size), "side": side}
    if claim:
        ch["best_bid"], ch["best_ask"] = claim
    return {"event_type": "price_change", "timestamp": str(ts), "price_changes": [ch]}


def tick(ts):
    return {"event_type": "_rtt", "rtt_ms": 300}


class TestMoney(unittest.TestCase):
    def test_fee_matches_the_docs_example(self):
        self.assertAlmostEqual(taker_fee(100, 0.50, 0.05), 1.25)

    def test_fee_shrinks_near_the_extremes(self):
        self.assertLess(taker_fee(100, 0.97, 0.05), 0.2)
        self.assertGreater(taker_fee(100, 0.5, 0.05), 1.0)


class TestBook(unittest.TestCase):
    def test_snapshot_then_deltas(self):
        b = Book()
        b.reset([{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "5"}],
                [{"price": "0.45", "size": "7"}, {"price": "0.50", "size": "9"}])
        self.assertEqual((b.best_bid, b.best_ask), (to_micro(0.42), to_micro(0.45)))
        b.set_level("BUY", to_micro(0.43), 3)
        self.assertEqual(b.best_bid, to_micro(0.43))
        b.set_level("BUY", to_micro(0.43), 0)                    # level removed -> falls back
        self.assertEqual(b.best_bid, to_micro(0.42))
        b.set_level("SELL", to_micro(0.45), 0)
        self.assertEqual(b.best_ask, to_micro(0.50))
        b.set_level("SELL", to_micro(0.50), 0)
        self.assertIsNone(b.best_ask)

    def test_exact_price_arithmetic(self):
        self.assertEqual(to_micro("0.995") + to_micro("0.005"), 1_000_000)


class TestCatalogPairs(unittest.TestCase):
    def test_moneyline_plus_opposite_spread_is_the_only_cover(self):
        pairs = [p for p in make_catalog().combo_pairs() if p.kind == "ml_spread"]
        self.assertEqual(len(pairs), 1)
        p = pairs[0]
        self.assertEqual({p.token_a, p.token_b}, {"tN", "sJ45"})   # Nuggets win OR Jazz cover +4.5
        self.assertEqual((p.guaranteed, p.max_payoff), (1, 2))      # both pay if Nuggets win by 1..4

    def test_spread_ladder_and_totals_ladder(self):
        extra = [mk("sp95", "g", "spreads", -9.5, ["Nuggets", "Jazz"], ["sN95", "sJ95"]),
                 mk("to2", "g", "totals", 225.5, ["Over", "Under"], ["oO2", "oU2"])]
        pairs = make_catalog(extra=extra).combo_pairs()
        got = {(p.kind, frozenset((p.token_a, p.token_b))) for p in pairs}
        self.assertIn(("spread_ladder", frozenset(("sN45", "sJ95"))), got)         # win by >4.5 OR by <9.5
        self.assertNotIn(("spread_ladder", frozenset(("sN95", "sJ45"))), got)      # leaves 5..9 uncovered
        self.assertIn(("totals_ladder", frozenset(("oO", "oU2"))), got)            # over 220.5 OR under 225.5
        self.assertNotIn(("totals_ladder", frozenset(("oO2", "oU"))), got)

    def test_whole_number_lines_are_skipped(self):
        cat = make_catalog(extra=[mk("sp5", "g", "spreads", -5.0, ["Nuggets", "Jazz"], ["sN5", "sJ5"])])
        toks = {t for p in cat.combo_pairs() for t in (p.token_a, p.token_b)}
        self.assertNotIn("sJ5", toks)

    def test_phase_labels(self):
        cat = make_catalog(end_ms=3_000_000.0)
        self.assertEqual(cat.phase_at("g", START - 1), "pre")
        self.assertEqual(cat.phase_at("g", T0), "in")
        self.assertEqual(cat.phase_at("g", 3_000_001.0), "post")


class TestDetectors(unittest.TestCase):
    def test_single_market_episode_and_duration(self):
        eng = Engine(make_catalog())
        eng.on_message(T0, book("oO", asks=[(0.48, 100)]))
        eng.on_message(T0 + 10, book("oU", asks=[(0.50, 100)]))             # 0.98 < 1  -> arbitrage starts
        self.assertEqual(len(eng.active), 1)
        eng.on_message(T0 + 250, pc("oU", "SELL", 0.50, 0))                  # best ask disappears
        eng.on_message(T0 + 260, pc("oU", "SELL", 0.55, 100))                # now 0.48 + 0.55 > 1
        self.assertEqual(len(eng.episodes), 1)
        ep = eng.episodes[0]
        self.assertEqual((ep.kind, ep.path, ep.phase), ("single", "buy", "in"))
        self.assertAlmostEqual(ep.edge_start, 0.02)
        self.assertEqual(ep.duration_ms, 240)                                # 250 - 10

    def test_dust_is_counted_but_not_an_episode(self):
        eng = Engine(make_catalog())
        eng.on_message(T0, book("oO", asks=[(0.48, 5)]))
        eng.on_message(T0 + 1, book("oU", asks=[(0.50, 5)]))                 # 5 shares ~ $4.9 < $10
        self.assertEqual(len(eng.active) + len(eng.episodes), 0)
        self.assertEqual(eng.dust_signals["single"], 1)

    def test_combo_signal_uses_both_markets(self):
        eng = Engine(make_catalog())
        eng.on_message(T0, book("tN", asks=[(0.60, 50)]))
        eng.on_message(T0 + 1, book("sJ45", asks=[(0.39, 50)]))              # 0.60 + 0.39 = 0.99
        self.assertEqual([e.kind for e in eng.active.values()], ["ml_spread"])
        ep = next(iter(eng.active.values()))
        self.assertAlmostEqual(ep.edge_start, 0.01)
        self.assertEqual(ep.max_payoff, 2)

    def test_post_game_episode_is_labelled(self):
        eng = Engine(make_catalog(end_ms=1_500_000.0))
        eng.on_message(T0, book("oO", asks=[(0.48, 100)]))
        eng.on_message(T0 + 1, book("oU", asks=[(0.50, 100)]))
        self.assertEqual(next(iter(eng.active.values())).phase, "post")

    def test_reconnect_closes_open_episodes(self):
        eng = Engine(make_catalog())
        eng.on_message(T0, book("oO", asks=[(0.48, 100)]))
        eng.on_message(T0 + 1, book("oU", asks=[(0.50, 100)]))
        eng.on_message(T0 + 500, {"event_type": "_meta", "what": "disconnect"})
        self.assertEqual(eng.episodes[0].reason_closed, "feed_gap")

    def test_integrity_counters(self):
        eng = Engine(make_catalog())
        eng.on_message(T0, book("oO", bids=[(0.40, 10)], asks=[(0.45, 10)]))
        eng.on_message(T0 + 1, pc("oO", "BUY", 0.41, 5, claim=("0.41", "0.45")))   # right
        eng.on_message(T0 + 2, pc("oO", "BUY", 0.42, 5, claim=("0.99", "0.45")))   # wrong bid
        self.assertEqual(eng.integrity["checked"], 2)
        self.assertEqual(eng.integrity["bid_mismatch"], 1)
        self.assertEqual(eng.integrity.get("ask_mismatch", 0), 0)


def combo_engine(scenarios, fee_catalog=None):
    eng = Engine(fee_catalog or make_catalog(), scenarios)
    eng.on_message(T0, book("tN", asks=[(0.60, 50)], bids=[(0.55, 100)]))
    eng.on_message(T0, book("sJ45", asks=[(0.39, 50)]))
    return eng


class TestExecution(unittest.TestCase):
    def test_balanced_fill_fees_can_exceed_the_edge(self):
        eng = combo_engine([Scenario("fees", latency_ms=800), Scenario("nofee", latency_ms=800, fee_rate=0.0)])
        eng.on_message(T0 + 3000, tick(0))                                   # time passes, books unchanged
        fees, nofee = eng.sims[0].trades[0], eng.sims[1].trades[0]
        self.assertEqual((fees.outcome, fees.matched_shares), ("balanced", 50.0))
        self.assertAlmostEqual(nofee.pnl, 0.50)                              # 50 x $0.01
        expected_fee = taker_fee(50, 0.60, 0.05) + taker_fee(50, 0.39, 0.05)
        self.assertAlmostEqual(fees.pnl, 0.50 - expected_fee)
        self.assertLess(fees.pnl, 0)                                         # the paper's zero-fee edge is gone

    def test_quote_disappears_before_arrival_means_leg_failure_and_unwind_loss(self):
        eng = combo_engine([Scenario("home", latency_ms=800, order_type="FOK")])
        eng.on_message(T0 + 300, pc("sJ45", "SELL", 0.39, 0))               # someone else took the second leg
        eng.on_message(T0 + 1000, tick(0))                                   # order arrives: leg A fills, leg B can't
        tr = eng.sims[0].trades
        self.assertEqual(len(tr), 0)                                         # still waiting to unwind
        eng.on_message(T0 + 3000, tick(0))
        t = eng.sims[0].trades[0]
        self.assertEqual(t.outcome, "leg_failure")
        expected = -30.0 - taker_fee(50, 0.60, 0.05) + 50 * 0.55 - taker_fee(50, 0.55, 0.05)
        self.assertAlmostEqual(t.pnl, expected)                              # bought at .60, sold back at .55

    def test_fok_vs_fak_on_a_thin_second_leg(self):
        for otype, outcome, matched in (("FOK", "leg_failure", 0.0), ("FAK", "leg_failure", 20.0)):
            eng = combo_engine([Scenario("s", latency_ms=800, order_type=otype)])
            eng.on_message(T0 + 300, pc("sJ45", "SELL", 0.39, 20))          # only 20 shares left on leg B
            eng.on_message(T0 + 5000, tick(0))
            t = eng.sims[0].trades[0]
            self.assertEqual((t.outcome, t.matched_shares), (outcome, matched), otype)

    def test_below_minimum_order_size_is_skipped(self):
        eng = Engine(make_catalog(), [Scenario("s", latency_ms=100, cap_usd=3.0)])
        eng.on_message(T0, book("tN", asks=[(0.60, 50)]))
        eng.on_message(T0, book("sJ45", asks=[(0.39, 50)]))
        self.assertEqual(eng.sims[0].skipped.get("below_min_order_size"), 1)

    def test_kill_switch_blocks_new_orders(self):
        eng = Engine(make_catalog(), [Scenario("s", latency_ms=100)])
        eng.killed = True
        eng.on_message(T0, book("tN", asks=[(0.60, 50)]))
        eng.on_message(T0, book("sJ45", asks=[(0.39, 50)]))
        self.assertEqual(eng.sims[0].skipped.get("kill_switch"), 1)
        self.assertEqual(len(eng.sims[0]._heap), 0)

    def test_daily_loss_cap_stops_trading(self):
        eng = combo_engine([Scenario("s", latency_ms=100, daily_loss_cap_usd=0.10)])
        eng.on_message(T0 + 1000, tick(0))                                   # first trade loses ~$0.69 in fees
        self.assertLess(eng.sims[0].trades[0].pnl, -0.10)
        eng.on_message(T0 + 2000, pc("sJ45", "SELL", 0.39, 0))
        eng.on_message(T0 + 2100, pc("sJ45", "SELL", 0.39, 50))              # arbitrage reappears
        self.assertEqual(eng.sims[0].skipped.get("daily_loss_cap"), 1)

    def test_orders_after_the_data_ends_are_censored_not_invented(self):
        eng = combo_engine([Scenario("s", latency_ms=60_000)])
        eng.on_message(T0 + 10, tick(0))
        eng.finish()
        self.assertEqual(eng.sims[0].trades[0].outcome, "censored")


class TestAnalysis(unittest.TestCase):
    def test_polling_artifact(self):
        n = 400
        df = pd.DataFrame({"start_recv_ms": np.linspace(0, 4_000_000, n), "duration_ms": np.full(n, 100.0)})
        r = report.polling_emulation(df, period_s=4.5, trials=300, seed=3)
        self.assertAlmostEqual(r["recall"], 0.1 / 4.5, delta=0.01)           # a poller sees ~2% of 100 ms windows
        self.assertAlmostEqual(r["median_credited_s"], 4.5)                  # ...and credits each with a full gap
        self.assertAlmostEqual(r["share_seen_exactly_once"], 1.0)

    def test_gate_closed_without_evidence_and_open_with_it(self):
        replay = {"coverage": {"games_counted": 30, "game_days": 6},
                  "scenarios": {"home_800ms": {"in_game": {"trades": 80, "leg_failure_share_of_filled": 0.1},
                                               "net_per_game": 4.0,
                                               "per_game_pnl": {f"g{i}": 4.0 + (i % 3) for i in range(30)}}}}
        opened, checks = gate.evaluate(replay, None)
        self.assertFalse(opened)                                             # no shadow evidence yet
        shadow = {"coverage": {"games_counted": 12},
                  "scenarios": {"home_800ms": {"net_per_game": 3.5}}}
        opened, checks = gate.evaluate(replay, shadow)
        self.assertTrue(opened, [c for c in checks if not c.passed])
        replay["scenarios"]["home_800ms"]["per_game_pnl"] = {f"g{i}": (-30.0 if i % 2 else 38.0) for i in range(30)}
        replay["scenarios"]["home_800ms"]["net_per_game"] = 4.0
        replay["coverage"]["games_counted"] = 30
        opened, _ = gate.evaluate(replay, shadow)
        self.assertFalse(opened)                                             # mean positive but interval spans zero

    def test_bootstrap_of_constants(self):
        mean, lo, hi = gate.bootstrap_mean_ci([2.0] * 10)
        self.assertEqual((mean, lo, hi), (2.0, 2.0, 2.0))

    def test_summary_runs_end_to_end(self):
        eng = combo_engine([Scenario("home_800ms", latency_ms=800)])
        eng.on_message(T0 + 400, pc("sJ45", "SELL", 0.39, 0))
        eng.on_message(T0 + 5000, tick(0))
        eng.finish()
        with tempfile.TemporaryDirectory() as d:
            S = report.write_outputs(eng, d, plots=False)
            self.assertTrue(os.path.exists(os.path.join(d, "summary.md")))
            self.assertIn("scenarios", S)


class TestPlumbing(unittest.TestCase):
    def test_log_roundtrip_and_append(self):
        with tempfile.TemporaryDirectory() as d:
            w = LogWriter(d)
            w.write(1_790_000_000_123.456, '{"a":1}')
            w.write(1_790_000_000_200.0, '{"b":"x\ty"}')
            w.close()
            w = LogWriter(d)                                                 # restart in the same hour appends
            w.write(1_790_000_000_300.0, '{"c":3}')
            w.close()
            rows = list(iter_log(list_logs(d)))
            self.assertEqual([r[1] for r in rows], ['{"a":1}', '{"b":"x\ty"}', '{"c":3}'])
            self.assertAlmostEqual(rows[0][0], 1_790_000_000_123.456, places=2)

    def test_iso_parsing(self):
        self.assertEqual(gamma.iso_ms("2026-10-04 23:00:00+00"), gamma.iso_ms("2026-10-04T23:00:00Z"))
        self.assertAlmostEqual(gamma.iso_ms("2026-10-05T01:32:23.709461Z") % 1000, 709.461, places=2)
        self.assertIsNone(gamma.iso_ms(None))

    def test_which_games_to_listen_to(self):
        now = 10_000_000_000.0
        H = 3_600_000.0
        cat = Catalog()
        for slug, start, end in (("soon", now + 2 * H, None), ("later", now + 5 * H, None),
                                 ("just_ended", now - 3 * H, now - 10 * 60_000), ("long_over", now - 4 * H, now - 60 * 60_000)):
            ev = Event(slug=slug, title=slug, game_id="", start_ms=start, finished_ms=end)
            cat.add_event(ev, [mk(slug + "m", slug, "moneyline", None, ["A", "B"], [slug + "a", slug + "b"])])
        toks, active = gamma.select_tokens(cat, now, lead_h=3, post_min=30)
        self.assertEqual(set(active), {"soon", "just_ended"})
        self.assertEqual(len(toks), 4)


if __name__ == "__main__":
    unittest.main()
