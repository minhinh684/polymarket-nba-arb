"""Stage 1: re-run the engine over recorded raw logs (no network, instant to repeat)."""
from __future__ import annotations

import json
import os
import time

from .catalog import Catalog
from .engine import Engine
from .execution import Scenario, default_scenarios
from .logio import iter_log, list_logs
from . import report


def load_catalog(path: str) -> Catalog:
    if os.path.isdir(path):
        path = os.path.join(path, "catalog.json")
    with open(path) as fh:
        return Catalog.from_json(fh.read())


def measured_rtt_ms(paths) -> float | None:
    """Median REST round trip our own recorder measured (rows of event_type _rtt)."""
    vals = []
    for _, raw in iter_log(paths):
        if raw.startswith('{"event_type":"_rtt"'):
            try:
                vals.append(float(json.loads(raw)["rtt_ms"]))
            except (ValueError, KeyError):
                pass
    vals.sort()
    return vals[len(vals) // 2] if vals else None


def run_replay(log_root: str, catalog: Catalog, scenarios: list[Scenario] | None = None, *,
               quiet: bool = False) -> Engine:
    paths = list_logs(log_root)
    if not paths:
        raise SystemExit(f"no *.tsv.gz logs under {log_root}")
    if scenarios is None:
        scenarios = default_scenarios(measured_rtt_ms(paths))
    eng = Engine(catalog, scenarios)
    t0, n = time.time(), 0
    for recv_ms, raw in iter_log(paths):
        if '"best_bid_ask"' in raw[-40:]:         # redundant with book / price_change: count it, skip the parsing
            eng.msg_counts["best_bid_ask"] += 1
            n += 1
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        for item in (obj if isinstance(obj, list) else [obj]):
            eng.on_message(recv_ms, item)
        n += 1
    eng.finish()
    if not quiet:
        print(f"replayed {n:,} messages from {len(paths)} file(s) in {time.time() - t0:.1f}s; "
              f"{len(eng.episodes)} episodes")
    return eng


def replay_and_report(log_root: str, catalog_path: str, out_dir: str, scenarios=None, quiet=False) -> dict:
    cat = load_catalog(catalog_path)
    eng = run_replay(log_root, cat, scenarios, quiet=quiet)
    return report.write_outputs(eng, out_dir)
