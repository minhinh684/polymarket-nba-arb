"""The go / no-go gate in front of stage 3.

It exists so that "should we trade real money?" is answered by numbers we agreed on BEFORE seeing
the results, not by excitement after a lucky night. Every threshold lives in config.py.
Even if every check passes, stage 3 still needs the manual checklist in docs/STAGE3.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np

from . import config

CONSERVATIVE = "home_800ms"      # home-laptop latency (~800 ms), FOK orders, the market's real fee rate


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def bootstrap_mean_ci(values, draws: int = config.GATE_BOOTSTRAP_DRAWS, seed: int = 7):
    """Resample whole games with replacement; return (mean, 5th percentile, 95th percentile)."""
    v = np.asarray(list(values), dtype=float)
    if v.size == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(draws, v.size), replace=True).mean(axis=1)
    return float(v.mean()), float(np.percentile(means, 5)), float(np.percentile(means, 95))


def evaluate(replay: dict, shadow: dict | None = None, scenario: str = CONSERVATIVE):
    checks: list[Check] = []
    cov = replay.get("coverage", {})
    sc = replay.get("scenarios", {}).get(scenario)

    checks.append(Check("enough games recorded", cov.get("games_counted", 0) >= config.GATE_MIN_GAMES,
                        f"{cov.get('games_counted', 0)} usable games (need {config.GATE_MIN_GAMES})"))
    checks.append(Check("enough different game days", cov.get("game_days", 0) >= config.GATE_MIN_GAME_DAYS,
                        f"{cov.get('game_days', 0)} days (need {config.GATE_MIN_GAME_DAYS})"))
    if sc is None:
        checks.append(Check(f"scenario '{scenario}' present", False, "run the replay with the default scenarios"))
        return False, checks
    g = sc["in_game"]
    checks.append(Check("enough simulated trades", g["trades"] >= config.GATE_MIN_EPISODES,
                        f"{g['trades']} in-game trades (need {config.GATE_MIN_EPISODES})"))
    npg = sc.get("net_per_game")
    checks.append(Check("net profit per game after frictions", npg is not None and npg > config.GATE_MIN_NET_PER_GAME_USD,
                        f"${npg} per game (need > ${config.GATE_MIN_NET_PER_GAME_USD})"))
    mean, p5, p95 = bootstrap_mean_ci(sc.get("per_game_pnl", {}).values())
    checks.append(Check("lower confidence bound above zero", p5 > 0,
                        f"mean ${mean:.2f}/game, 90% interval [${p5:.2f}, ${p95:.2f}] (resampled by game)"))
    lf = g.get("leg_failure_share_of_filled")
    checks.append(Check("leg failures are rare", lf is not None and lf <= config.GATE_MAX_LEG_FAILURE_SHARE,
                        f"{'n/a' if lf is None else f'{100 * lf:.0f}%'} of filled trades (max {100 * config.GATE_MAX_LEG_FAILURE_SHARE:.0f}%)"))
    checks.append(Check("fees are modelled (not the paper's zero)", "nofee" not in scenario,
                        f"scenario '{scenario}' uses each market's own fee rate"))
    if shadow is None:
        checks.append(Check("stage 2 shadow evidence", False, "no shadow summary given: run stage 2 first"))
    else:
        scov = shadow.get("coverage", {})
        ssc = shadow.get("scenarios", {}).get(scenario)
        ok_games = scov.get("games_counted", 0) >= config.GATE_MIN_SHADOW_GAMES
        checks.append(Check("enough games in shadow mode", ok_games,
                            f"{scov.get('games_counted', 0)} games (need {config.GATE_MIN_SHADOW_GAMES})"))
        s_npg = ssc.get("net_per_game") if ssc else None
        agree = s_npg is not None and npg is not None and s_npg > 0 and s_npg >= 0.5 * npg
        checks.append(Check("shadow agrees with replay", agree,
                            f"shadow ${s_npg} per game vs replay ${npg} (need positive and >= half)"))
    return all(c.passed for c in checks), checks


def render(opened: bool, checks: list[Check]) -> str:
    lines = ["GO / NO-GO GATE FOR STAGE 3", ""]
    for c in checks:
        lines.append(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
    lines += ["", "VERDICT: " + ("OPEN (numbers are not the obstacle). Stage 3 still needs the manual checklist in docs/STAGE3.md."
                                 if opened else "CLOSED. Keep recording and replaying; nothing here justifies real money yet.")]
    return "\n".join(lines)


def load_summary(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)
