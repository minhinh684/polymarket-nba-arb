"""Find NBA games and their markets through Polymarket's public Gamma API (no account needed)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import requests

from . import config
from .catalog import Catalog, Event, Market


def iso_ms(s: str | None) -> float | None:
    """'2026-10-04 23:00:00+00' or '2026-10-05T01:32:23.709461Z' -> epoch milliseconds."""
    if not s:
        return None
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    elif len(s) > 3 and s[-3] in "+-" and ":" not in s[-3:]:
        s += ":00"                                   # '+00' -> '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def fetch_events(series_id: int = config.NBA_SERIES_ID, *, session: requests.Session | None = None,
                 closed: bool = False, page: int = 100, max_pages: int = 8, retries: int = 3) -> list[dict]:
    """All not-yet-closed events of a series (paginated)."""
    sess = session or requests.Session()
    out: list[dict] = []
    for p in range(max_pages):
        params = {"series_id": series_id, "limit": page, "offset": p * page,
                  "order": "startDate", "ascending": "true",
                  "closed": str(closed).lower()}
        if not closed:
            params["active"] = "true"
        for attempt in range(retries):
            try:
                r = sess.get(f"{config.GAMMA_API}/events", params=params, timeout=30)
                r.raise_for_status()
                chunk = r.json()
                break
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(1.5 * (attempt + 1))
        out.extend(chunk)
        if len(chunk) < page:
            break
    return out


def _loads(x):
    return json.loads(x) if isinstance(x, str) else (x or [])


def parse_event(raw: dict, types: tuple[str, ...] = config.RECORD_TYPES) -> tuple[Event, list[Market]]:
    slug = raw["slug"]
    mk_raw = raw.get("markets") or []
    start_ms = iso_ms(raw.get("startTime"))
    if start_ms is None:
        for m in mk_raw:
            start_ms = iso_ms(m.get("gameStartTime"))
            if start_ms:
                break
    teams = [t.get("name", "") for t in (raw.get("teams") or [])]
    ev = Event(
        slug=slug, title=raw.get("title", slug), game_id=str(raw.get("gameId") or ""), start_ms=start_ms,
        live=bool(raw.get("live")), ended=bool(raw.get("ended")), period=raw.get("period") or "",
        score=raw.get("score") or "", elapsed=raw.get("elapsed") or "",
        finished_ms=iso_ms(raw.get("finishedTimestamp")), teams=teams,
    )
    markets: list[Market] = []
    for m in mk_raw:
        mtype = m.get("sportsMarketType") or ""
        if mtype not in types:
            continue
        outcomes = _loads(m.get("outcomes"))
        tokens = _loads(m.get("clobTokenIds"))
        if len(outcomes) != 2 or len(tokens) != 2:
            continue
        fs = m.get("feeSchedule") or {}
        fees_on = bool(m.get("feesEnabled"))
        markets.append(Market(
            market_id=str(m["id"]), condition_id=m.get("conditionId") or "", event_slug=slug,
            question=m.get("question") or "", mtype=mtype,
            period="1h" if mtype.startswith("first_half") else "full",
            line=m.get("line"), outcomes=[str(o) for o in outcomes], token_ids=[str(t) for t in tokens],
            accepting_orders=bool(m.get("acceptingOrders")), closed=bool(m.get("closed")),
            min_size=float(m.get("orderMinSize") or config.MIN_ORDER_SHARES),
            tick=float(m.get("orderPriceMinTickSize") or 0.001),
            fee_rate=float(fs.get("rate", config.DEFAULT_FEE_RATE)) if fees_on else 0.0,
            fee_exponent=float(fs.get("exponent", 1.0)), fee_taker_only=bool(fs.get("takerOnly", True)),
            rebate_rate=float(fs.get("rebateRate", 0.0)), seconds_delay=float(m.get("secondsDelay") or 0.0),
            neg_risk=bool(m.get("negRisk")), game_start_ms=iso_ms(m.get("gameStartTime")) or start_ms,
        ))
    return ev, markets


def parse_events(raw_events: list[dict], types: tuple[str, ...] = config.RECORD_TYPES):
    return [parse_event(r, types) for r in raw_events]


def select_tokens(cat: Catalog, now_ms: float, *, lead_h: float, post_min: float,
                  slugs: set[str] | None = None, max_event_hours: float = 8.0) -> tuple[set[str], list[str]]:
    """Which tokens should we be listening to right now? Returns (token ids, event slugs)."""
    tokens: set[str] = set()
    active: list[str] = []
    for ev in cat.events.values():
        if slugs is not None:
            if ev.slug not in slugs:
                continue
        else:
            if ev.start_ms is None or now_ms < ev.start_ms - lead_h * 3_600_000:
                continue
            end = ev.end_ms()
            if end is not None and now_ms > end + post_min * 60_000:
                continue
            if end is None and now_ms > ev.start_ms + max_event_hours * 3_600_000:
                continue
        mk = [cat.markets[i] for i in ev.market_ids if i in cat.markets and not cat.markets[i].closed]
        if not mk:
            continue
        active.append(ev.slug)
        for m in mk:
            tokens.update(m.token_ids)
    return tokens, active
