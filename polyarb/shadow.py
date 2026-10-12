"""Stage 2: shadow mode. The engine runs LIVE on the real feed and logs what it WOULD have done.

No order is ever sent. There are no credentials anywhere in this package. Shadow mode also keeps
writing the raw recording, so every shadow night doubles as stage-0 data.

Safety layer that stage 3 would reuse (here it only gates hypothetical orders):
  * per-trade cap           Scenario.cap_usd
  * daily loss cap          Scenario.daily_loss_cap_usd
  * kill switch             create a file named KILL inside the output folder and no NEW hypothetical
                            order is made until you delete it
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
from dataclasses import asdict

from . import config, report
from .catalog import Catalog
from .engine import Engine
from .execution import Scenario, default_scenarios
from .recorder import Recorder


def run_shadow(out_dir: str, *, scenarios: list[Scenario] | None = None, slugs: set[str] | None = None,
               lead_h: float = config.LEAD_HOURS, post_min: float = config.POST_MINUTES,
               refresh_s: float = config.REFRESH_SECONDS, duration_s: float | None = None,
               auto_stop_idle_min: float | None = None) -> None:
    os.makedirs(os.path.join(out_dir, "shadow"), exist_ok=True)
    cat = Catalog()
    eng = Engine(cat, scenarios if scenarios is not None else default_scenarios())
    kill_path = os.path.join(out_dir, "KILL")
    ep_log = open(os.path.join(out_dir, "shadow", "episodes.jsonl"), "a", buffering=1)
    tr_log = open(os.path.join(out_dir, "shadow", "trades.jsonl"), "a", buffering=1)

    def on_ep_end(ep):
        ep_log.write(json.dumps(asdict(ep)) + "\n")

    def on_trade(tr):
        tr_log.write(json.dumps(asdict(tr)) + "\n")

    eng.on_episode_end, eng.on_trade_hook = on_ep_end, on_trade
    state = {"last_check": 0.0, "last_status": time.monotonic(), "measured": False}

    def on_tick(now_ms: float) -> None:
        eng.advance(now_ms)
        if time.monotonic() - state["last_check"] > 1.0:
            state["last_check"] = time.monotonic()
            killed = os.path.exists(kill_path)
            if killed != eng.killed:
                print(("KILL SWITCH ON: no new hypothetical orders" if killed else "kill switch off"), flush=True)
            eng.killed = killed
        if not state["measured"] and len(eng.rtt_ms) >= 3 and scenarios is None:
            med = eng.median_rtt_ms()
            eng.add_scenario(Scenario("measured_rtt", latency_ms=med * 1.5))
            state["measured"] = True
        if time.monotonic() - state["last_status"] >= 60:
            state["last_status"] = time.monotonic()
            parts = []
            for sim in eng.sims:
                done = [t for t in sim.trades if not t.censored]
                parts.append(f"{sim.sc.name}: {len(done)} trades, ${sum(t.pnl for t in done):+.2f}")
            print(f"{time.strftime('%H:%M:%S', time.gmtime())}Z  shadow | episodes={len(eng.episodes)}+{len(eng.active)} open | "
                  + " | ".join(parts), flush=True)

    rec = Recorder(out_dir, slugs=slugs, lead_h=lead_h, post_min=post_min, refresh_s=refresh_s, duration_s=duration_s,
                   on_message=eng.on_message, on_catalog=eng.set_catalog, on_tick=on_tick, catalog=cat,
                   auto_stop_idle_min=auto_stop_idle_min)
    try:
        asyncio.run(rec.run())
    finally:
        eng.finish()
        tag = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d_%H%M")
        S = report.write_outputs(eng, os.path.join(out_dir, "shadow", f"summary-{tag}"))
        ep_log.close(); tr_log.close()
        print(f"shadow summary written to {os.path.join(out_dir, 'shadow', f'summary-{tag}')}")
