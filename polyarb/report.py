"""Turn an Engine's results into numbers a human can read, and into the comparison with the paper.

The most important function here is `polling_emulation`. The paper watched the markets by polling every
3.6-5.5 s and credited each sighting with the gap to the next snapshot, so a median "duration" near
that interval is partly an artifact of the sampling. We know each episode's lifetime at event
resolution, so we can re-sample it the paper's way and see what the paper would have reported.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict

import numpy as np
import pandas as pd

from . import config

PAPER = {
    "single_in_game_episodes": 7, "single_median_duration_s": 3.614, "single_capped_profit": 210.19,
    "combo_in_game_episodes": 279, "combo_total_episodes": 290, "combo_median_duration_s": 16.0,
    "combo_median_yield_bps": 101.01, "combo_capped_profit": 559.59, "combo_share_not_fully_deployable": 0.769,
    "combo_avg_executable_shares": 14.79, "combo_jackpots": 0, "games": 173,
    "post_game_share_single": 30 / 37, "post_game_share_combo": 233 / 523,
}


# ----------------------------------------------------------------------------------------------
def episodes_df(eng) -> pd.DataFrame:
    rows = []
    for e in eng.episodes:
        d = asdict(e)
        d.pop("legs_start", None)
        d["duration_ms"] = e.duration_ms
        rows.append(d)
    return pd.DataFrame(rows)


def trades_df(sim) -> pd.DataFrame:
    rows = []
    for t in sim.trades:
        d = asdict(t)
        d["fills"] = json.dumps(d["fills"])
        rows.append(d)
    return pd.DataFrame(rows)


def dur_stats(ms: pd.Series) -> dict:
    s = ms.dropna()
    if s.empty:
        return {"n": 0}
    return {"n": int(s.size), "median_ms": float(s.median()), "p10_ms": float(s.quantile(0.1)),
            "p90_ms": float(s.quantile(0.9)), "share_le_100ms": float((s <= 100).mean()),
            "share_le_1s": float((s <= 1000).mean()), "share_le_4s": float((s <= 4000).mean())}


def polling_emulation(df: pd.DataFrame, period_s: float, trials: int = 40, seed: int = 1) -> dict:
    """Re-observe our event-resolution episodes the way the paper's poller would have."""
    d = df.dropna(subset=["duration_ms"])
    if d.empty:
        return {"period_s": period_s, "n_true": 0}
    start = d["start_recv_ms"].to_numpy() / 1000.0
    end = start + d["duration_ms"].to_numpy() / 1000.0
    rng = np.random.default_rng(seed)
    recalls, medians, share_one = [], [], []
    for _ in range(trials):
        phi = rng.uniform(0, period_s)
        n = np.ceil((end - phi) / period_s) - np.ceil((start - phi) / period_s)   # poll instants inside the episode
        seen = n >= 1
        recalls.append(seen.mean())
        if seen.any():
            credited = n[seen] * period_s                    # each sighting is credited one full gap
            medians.append(float(np.median(credited)))
            share_one.append(float((n[seen] == 1).mean()))
    return {"period_s": period_s, "n_true": int(len(d)), "recall": float(np.mean(recalls)),
            "median_credited_s": float(np.mean(medians)) if medians else None,
            "share_seen_exactly_once": float(np.mean(share_one)) if share_one else None}


def _in_game(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["phase"] == "in"] if not df.empty else df


# ----------------------------------------------------------------------------------------------
def coverage(eng) -> dict:
    cat = eng.cat
    games, days = [], set()
    for slug, st in eng.ev_stats.items():
        ev = cat.events.get(slug)
        if ev is None or ev.start_ms is None:
            continue
        end = ev.end_ms()
        cov = None
        if end and st["in_first"] is not None and end > ev.start_ms:
            cov = (st["in_last"] - st["in_first"]) / (end - ev.start_ms)
        ok = cov is not None and cov >= 0.6 and st["in_msgs"] >= 200
        games.append({"slug": slug, "msgs": st["msgs"], "in_msgs": st["in_msgs"],
                      "in_game_coverage": None if cov is None else round(cov, 3), "counted": ok})
        if ok:
            days.add(pd.to_datetime(ev.start_ms, unit="ms", utc=True).strftime("%Y-%m-%d"))
    in_progress = [g["slug"] for g in games
                   if g["in_msgs"] > 0 and cat.events[g["slug"]].end_ms() is None]
    return {"games_seen": len(games), "games_counted": sum(1 for g in games if g["counted"]),
            "game_days": len(days), "games_in_progress": len(in_progress), "per_game": games}


def scenario_results(eng, counted: set[str]) -> dict:
    out = {}
    for sim in eng.sims:
        tr = [t for t in sim.trades if not t.censored]
        res = {"skipped": dict(sim.skipped), "censored": sum(1 for t in sim.trades if t.censored)}
        for label, sel in (("in_game", [t for t in tr if t.phase == "in"]), ("all_phases", tr)):
            n = len(sel)
            oc = {k: sum(1 for t in sel if t.outcome == k) for k in ("no_fill", "balanced", "leg_failure")}
            filled = oc["balanced"] + oc["leg_failure"]
            res[label] = {
                "trades": n, "outcomes": oc,
                "fill_rate": (filled / n) if n else None,
                "leg_failure_share_of_filled": (oc["leg_failure"] / filled) if filled else None,
                "detected_capped_profit": round(sum(t.detected_capped_profit for t in sel), 2),
                "net_pnl": round(sum(t.pnl for t in sel), 2),
                "fees_paid": round(sum(t.fees + t.unwind_fees for t in sel), 2),
                "mean_pnl_per_trade": round(sum(t.pnl for t in sel) / n, 4) if n else None,
            }
        per_game = {slug: 0.0 for slug in counted}
        for t in tr:
            if t.phase == "in" and t.event_slug in per_game:
                per_game[t.event_slug] += t.pnl
        res["per_game_pnl"] = per_game
        res["net_per_game"] = round(float(np.mean(list(per_game.values()))), 4) if per_game else None
        out[sim.sc.name] = res
    return out


def summarize(eng) -> dict:
    df = episodes_df(eng)
    cov = coverage(eng)
    counted = {g["slug"] for g in cov["per_game"] if g["counted"]}
    done = df[df["reason_closed"] == "state_changed"] if not df.empty else df     # skip censored lifetimes
    ig = _in_game(done)
    S: dict = {"coverage": cov, "integrity": dict(eng.integrity), "messages": dict(eng.msg_counts),
               "closest_approach_cents": {k: round(v / config.PRICE_SCALE * 100, 3) for k, v in eng.min_slack.items()},
               "evaluations": {"pairs": eng.pair_evals, "singles": eng.single_evals},
               "median_rtt_ms": eng.median_rtt_ms(), "dust_signals": dict(eng.dust_signals),
               "paper": PAPER, "episodes": {}, "durations": {}, "polling_emulation": []}
    if not df.empty:
        S["episodes"]["by_kind_phase"] = {f"{k}|{p}": int(n) for (k, p), n in df.groupby(["kind", "phase"]).size().items()}
        S["episodes"]["closed_by"] = df["reason_closed"].value_counts().to_dict()
        S["episodes"]["total"] = int(len(df))
        for kind, label in (("single", "single"), ("ml_spread", "ml_spread"), ("spread_ladder", "spread_ladder"),
                            ("totals_ladder", "totals_ladder")):
            sub = ig[ig["kind"] == kind]
            S["durations"][label] = dur_stats(sub["duration_ms"])
        combos = ig[ig["kind"] != "single"]
        S["durations"]["all_combos"] = dur_stats(combos["duration_ms"])
        if not combos.empty:
            pair_cost = combos["cost_start"]
            S["combo_in_game"] = {
                "episodes": int(len(combos)),
                "median_yield_bps": float((combos["edge_start"] / pair_cost * 1e4).median()),
                "capped_profit_total": round(float(combos["max_capped_profit"].sum()), 2),
                "share_not_fully_deployable": float(((combos["shares_start"] * pair_cost) < config.CAP_USD).mean()),
                "avg_executable_shares": float(combos["shares_start"].mean()),
                "middle_possible_share": float((combos["max_payoff"] > 1).mean()),
            }
        post = df[df["phase"] == "post"]
        S["episodes"]["post_game_share"] = float(len(post) / len(df))
        for P in (3.6, 4.5, 5.5):
            S["polling_emulation"].append({"scope": "all in-game episodes", **polling_emulation(ig, P)})
    S["scenarios"] = scenario_results(eng, counted)
    return S


# ----------------------------------------------------------------------------------------------
def write_outputs(eng, out_dir: str, plots: bool = True) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    df = episodes_df(eng)
    df.to_csv(os.path.join(out_dir, "episodes.csv"), index=False)
    for sim in eng.sims:
        trades_df(sim).to_csv(os.path.join(out_dir, f"trades_{sim.sc.name}.csv"), index=False)
    S = summarize(eng)
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump(S, fh, indent=1, default=_json_default)
    with open(os.path.join(out_dir, "summary.md"), "w") as fh:
        fh.write(render_markdown(S))
    if plots and not df.empty:
        try:
            make_plots(eng, S, out_dir)
        except Exception as exc:          # plotting must never break the analysis
            print("plotting skipped:", exc)
    return S


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    return str(o)


def _fmt(x, nd=2):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.{nd}f}"


def render_markdown(S: dict) -> str:
    L = ["# polyarb summary", ""]
    c = S["coverage"]
    prog = c.get("games_in_progress", 0)
    L += [f"**Data**: {c['games_seen']} game(s) seen, {c['games_counted']} with usable in-game coverage "
          f"({c['game_days']} game-day(s)). Median REST round trip: {_fmt(S.get('median_rtt_ms'), 0)} ms."
          + (f" {prog} game(s) were still in progress when the recording ended (end time unknown), so they are not counted yet."
             if prog else ""), ""]
    ig = S["integrity"]
    if ig.get("checked"):
        L += [f"**Book integrity**: {ig['checked']} feed best-bid/ask claims checked; "
              f"{100 * ig.get('bid_mismatch', 0) / ig['checked']:.2f}% bid mismatches, "
              f"{100 * ig.get('ask_mismatch', 0) / ig['checked']:.2f}% ask mismatches "
              f"({ig.get('delta_before_snapshot', 0)} deltas arrived before a snapshot).", ""]
    if S.get("closest_approach_cents"):
        L += ["**Closest the market ever got to an arbitrage** (cents above break-even; negative = arbitrage): "
              + ", ".join(f"`{k}` {v:+.2f}¢" for k, v in sorted(S["closest_approach_cents"].items())) + "", ""]
    L += ["## Episodes (event resolution)", ""]
    for k, v in sorted(S["episodes"].get("by_kind_phase", {}).items()):
        L.append(f"- `{k}`: {v}")
    L.append("")
    L += ["| kind (in-game) | n | median | p10 | p90 | <=100ms | <=1s | <=4s |", "|---|---|---|---|---|---|---|---|"]
    for k, d in S["durations"].items():
        if d.get("n"):
            L.append(f"| {k} | {d['n']} | {d['median_ms'] / 1000:.2f}s | {d['p10_ms'] / 1000:.2f}s | {d['p90_ms'] / 1000:.2f}s | "
                     f"{100 * d['share_le_100ms']:.0f}% | {100 * d['share_le_1s']:.0f}% | {100 * d['share_le_4s']:.0f}% |")
    L.append("")
    if S["polling_emulation"]:
        L += ["## What the paper's poller would have reported on OUR episodes", "",
              "| poll period | episodes (true) | caught | median credited duration | seen exactly once |", "|---|---|---|---|---|"]
        for p in S["polling_emulation"]:
            if p.get("n_true"):
                L.append(f"| {p['period_s']}s | {p['n_true']} | {100 * p['recall']:.0f}% | "
                         f"{_fmt(p['median_credited_s'])}s | {_fmt(100 * (p['share_seen_exactly_once'] or 0), 0)}% |")
        L.append("")
    if "combo_in_game" in S:
        c = S["combo_in_game"]
        L += ["## Cross-market arbitrage vs the paper", "",
              f"- episodes (in-game): **{c['episodes']}** (paper: 279 in 173 games)",
              f"- median yield: **{c['median_yield_bps']:.0f} bps** (paper: 101)",
              f"- paper-style capped profit ($100/episode): **${c['capped_profit_total']:.2f}** (paper: $559.59 / 290 episodes)",
              f"- share that could not deploy $100: **{100 * c['share_not_fully_deployable']:.0f}%** (paper: 76.9%)",
              f"- average executable size: **{c['avg_executable_shares']:.1f} shares** (paper: 14.8)", ""]
    L += ["## Execution model (what a bot would really have made)", "",
          "| scenario | trades | no fill | balanced | leg failure | detected $ | net $ | fees $ | net $/game |",
          "|---|---|---|---|---|---|---|---|---|"]
    for name, r in S["scenarios"].items():
        g = r["in_game"]
        L.append(f"| {name} | {g['trades']} | {g['outcomes']['no_fill']} | {g['outcomes']['balanced']} | "
                 f"{g['outcomes']['leg_failure']} | {g['detected_capped_profit']} | {g['net_pnl']} | {g['fees_paid']} | "
                 f"{_fmt(r['net_per_game'], 3)} |")
    L += ["", "_Detected $ = what the paper would count (edge x size, $100 cap). Net $ = after reaction time, "
          "book movement, leg failures, unwinds and fees. Sample sizes on short recordings are tiny: read the "
          "shape, not the level._"]
    return "\n".join(L) + "\n"


def make_plots(eng, S: dict, out_dir: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = episodes_df(eng)
    ig = _in_game(df[df["reason_closed"] == "state_changed"])
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.2))
    if not ig.empty:
        d = ig["duration_ms"].clip(lower=1) / 1000.0
        bins = np.logspace(math.log10(max(d.min(), 1e-3)), math.log10(max(d.max(), 1.0)) + 0.1, 30)
        ax[0].hist(d, bins=bins, alpha=0.7, label="event-resolution (truth)")
        ax[0].set_xscale("log")
        for P, col in ((4.5, "tab:red"),):
            ax[0].axvline(P, color=col, ls="--", label=f"paper's polling gap ~{P}s")
        ax[0].set_xlabel("episode duration (s, log)"); ax[0].set_ylabel("episodes")
        ax[0].set_title("How long do arbitrage windows really last?"); ax[0].legend(fontsize=8)
    names = list(S["scenarios"])
    if names:
        det = [S["scenarios"][n]["in_game"]["detected_capped_profit"] for n in names]
        net = [S["scenarios"][n]["in_game"]["net_pnl"] for n in names]
        x = np.arange(len(names))
        ax[1].bar(x - 0.2, det, 0.4, label="detected (paper-style)")
        ax[1].bar(x + 0.2, net, 0.4, label="net after frictions")
        ax[1].set_xticks(x); ax[1].set_xticklabels(names, rotation=30, ha="right", fontsize=8)
        ax[1].axhline(0, color="k", lw=0.5); ax[1].set_ylabel("$"); ax[1].legend(fontsize=8)
        ax[1].set_title("Detected vs. realised (in-game)")
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, "overview.png"), dpi=120)
    plt.close(fig)
