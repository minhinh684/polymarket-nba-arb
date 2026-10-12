"""Command line:  python -m polyarb <command> [options]

  discover   list the NBA games Polymarket has right now (records nothing)
  record     STAGE 0  record raw order-book events to disk (read-only, no account)
  replay     STAGE 1  re-run the detectors + execution model on a recording, write a report
  shadow     STAGE 2  run live and log hypothetical orders only (nothing is ever sent)
  gate       go / no-go check in front of stage 3 (stage 3 itself is closed: docs/STAGE3.md)
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from . import config


def _csv(s):
    return tuple(x.strip() for x in s.split(",") if x.strip()) if s else None


def cmd_discover(args) -> int:
    from . import gamma
    from .catalog import Catalog
    cat = Catalog()
    for ev, mk in gamma.parse_events(gamma.fetch_events(args.series), config.RECORD_TYPES):
        cat.add_event(ev, mk)
    now = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    pairs = cat.combo_pairs()
    print(f"{'game':32s} {'tip-off (UTC)':14s} {'status':22s} markets  combo-pairs")
    for e in sorted(cat.events.values(), key=lambda e: e.start_ms or 0):
        if e.ended and not args.all:
            continue
        start = dt.datetime.fromtimestamp((e.start_ms or 0) / 1000, dt.timezone.utc).strftime("%m-%d %H:%M")
        if e.ended:
            status = "ended"
        elif e.live:
            status = f"LIVE {e.period} {e.score}"
        elif e.start_ms and e.start_ms > now:
            status = f"starts in {(e.start_ms - now) / 3.6e6:.1f}h"
        else:
            status = "?"
        n = sum(1 for p in pairs if p.event_slug == e.slug)
        print(f"{e.slug:32s} {start:14s} {status:22s} {len(e.market_ids):7d}  {n:5d}")
    return 0


def cmd_record(args) -> int:
    from .recorder import Recorder
    rec = Recorder(args.out, series_id=args.series, types=_csv(args.types) or config.RECORD_TYPES,
                   lead_h=args.lead_hours, post_min=args.post_minutes, refresh_s=args.refresh_s,
                   slugs=set(_csv(args.slugs)) if args.slugs else None,
                   duration_s=args.duration_min * 60 if args.duration_min else None,
                   auto_stop_idle_min=args.auto_stop_min)
    asyncio.run(rec.run())
    return 0


def cmd_replay(args) -> int:
    from .replay import replay_and_report
    S = replay_and_report(args.logs, args.catalog or args.logs.split("/raw")[0], args.out)
    print(open(f"{args.out}/summary.md").read())
    return 0


def cmd_shadow(args) -> int:
    from .shadow import run_shadow
    run_shadow(args.out, slugs=set(_csv(args.slugs)) if args.slugs else None, lead_h=args.lead_hours,
               post_min=args.post_minutes, refresh_s=args.refresh_s,
               duration_s=args.duration_min * 60 if args.duration_min else None,
               auto_stop_idle_min=args.auto_stop_min)
    return 0


def cmd_gate(args) -> int:
    from . import gate
    replay = gate.load_summary(args.replay)
    shadow = gate.load_summary(args.shadow) if args.shadow else None
    opened, checks = gate.evaluate(replay, shadow, args.scenario)
    print(gate.render(opened, checks))
    return 0 if opened else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="polyarb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="list NBA games and markets")
    d.add_argument("--series", type=int, default=config.NBA_SERIES_ID)
    d.add_argument("--all", action="store_true", help="include games that already ended")
    d.set_defaults(fn=cmd_discover)

    def common(sp):
        sp.add_argument("--out", default="data", help="output folder (default: data)")
        sp.add_argument("--lead-hours", type=float, default=config.LEAD_HOURS)
        sp.add_argument("--post-minutes", type=float, default=config.POST_MINUTES)
        sp.add_argument("--refresh-s", type=float, default=config.REFRESH_SECONDS)
        sp.add_argument("--slugs", help="only these game slugs, comma separated (ignores the time window)")
        sp.add_argument("--duration-min", type=float, help="stop automatically after N minutes")
        sp.add_argument("--auto-stop-min", type=float, help="stop N minutes after the last active game is over")

    r = sub.add_parser("record", help="stage 0: record raw events (read-only)")
    common(r)
    r.add_argument("--series", type=int, default=config.NBA_SERIES_ID)
    r.add_argument("--types", help="comma list of Polymarket sportsMarketType values")
    r.set_defaults(fn=cmd_record)

    rp = sub.add_parser("replay", help="stage 1: detectors + execution model over a recording")
    rp.add_argument("logs", help="folder with raw logs, e.g. data/raw")
    rp.add_argument("--catalog", help="catalog.json (default: next to the raw folder)")
    rp.add_argument("--out", default="data/analysis/latest")
    rp.set_defaults(fn=cmd_replay)

    s = sub.add_parser("shadow", help="stage 2: live, hypothetical orders only")
    common(s)
    s.set_defaults(fn=cmd_shadow)

    g = sub.add_parser("gate", help="go / no-go check in front of stage 3")
    g.add_argument("replay", help="summary.json from a replay")
    g.add_argument("--shadow", help="summary.json from a shadow run")
    g.add_argument("--scenario", default="home_800ms")
    g.set_defaults(fn=cmd_gate)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
