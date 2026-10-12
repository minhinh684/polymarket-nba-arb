"""Stage 0: listen to Polymarket's public order-book feed and write every message to disk.

Read-only. It needs no account, no keys and no money: the market WebSocket and the Gamma/CLOB
GET endpoints are public. The same class is reused by shadow mode (stage 2), which also feeds each
message to the detector engine.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import signal
import time
from typing import Callable

import requests
import websockets

from . import config, gamma
from .catalog import Catalog
from .logio import LogWriter

_ET = re.compile(r'"event_type"\s*:\s*"(\w+)"')


def _now_ms() -> float:
    return time.time() * 1000.0


def _chunks(items, n):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


class Recorder:
    def __init__(self, out_dir: str, *, series_id: int = config.NBA_SERIES_ID,
                 types: tuple[str, ...] = config.RECORD_TYPES, lead_h: float = config.LEAD_HOURS,
                 post_min: float = config.POST_MINUTES, refresh_s: float = config.REFRESH_SECONDS,
                 slugs: set[str] | None = None, duration_s: float | None = None,
                 on_message: Callable[[float, dict], None] | None = None,
                 on_catalog: Callable[[Catalog], None] | None = None,
                 on_tick: Callable[[float], None] | None = None,
                 catalog: Catalog | None = None, write_raw: bool = True, verbose: bool = True,
                 auto_stop_idle_min: float | None = None) -> None:
        self.out_dir = out_dir
        self.series_id, self.types = series_id, types
        self.lead_h, self.post_min, self.refresh_s = lead_h, post_min, refresh_s
        self.slugs, self.duration_s = slugs, duration_s
        self.on_message, self.on_catalog, self.on_tick = on_message, on_catalog, on_tick
        self.catalog = catalog or Catalog()
        self.writer = LogWriter(os.path.join(out_dir, "raw")) if write_raw else None
        self.verbose = verbose
        self.desired: set[str] = set()
        self._sent: set[str] = set()
        self.active_slugs: list[str] = []
        self._stop = asyncio.Event()
        self.counts: collections.Counter = collections.Counter()
        self._last_counts: collections.Counter = collections.Counter()
        self.reconnects = 0
        self.hook_errors = 0
        self.rtt_ms: collections.deque = collections.deque(maxlen=20)
        self.auto_stop_idle_min = auto_stop_idle_min     # stop this long after the LAST active game is over
        self._had_games = False
        self._empty_since: float | None = None

    # ------------------------------------------------------------------ public
    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:      # pragma: no cover (Windows)
                pass
        tasks = [asyncio.create_task(c) for c in
                 (self._discovery_loop(), self._ws_loop(), self._rtt_loop(), self._stats_loop(), self._tick_loop())]
        if self.duration_s:
            tasks.append(asyncio.create_task(self._deadline()))
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._save_catalog(final=True)
        if self.writer:
            self.writer.close()
        self._say("stopped. messages: " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())))

    # ------------------------------------------------------------------ helpers
    def _say(self, msg: str) -> None:
        if self.verbose:
            print(time.strftime("%H:%M:%S", time.gmtime()) + "Z  " + msg, flush=True)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _deadline(self) -> None:
        # wall-clock based: on a Mac asyncio's timers pause while the machine sleeps
        end = time.time() + (self.duration_s or 0)
        while time.time() < end and not self._stop.is_set():
            await self._sleep(1.0)
        self.stop()

    def _safe(self, fn, *args) -> None:
        """Run an analysis hook. A bug in the analysis must NEVER stop the raw recording: the raw log is the
        source of truth and can be re-analysed after a fix."""
        try:
            fn(*args)
        except Exception as exc:
            self.hook_errors += 1
            if self.hook_errors <= 5 or self.hook_errors % 1000 == 0:
                self._say(f"analysis error #{self.hook_errors} (recording continues): {exc!r}")

    def _emit(self, recv_ms: float, obj: dict) -> None:
        """Write one of OUR OWN marker rows (status, rtt, reconnect) and pass it to the engine."""
        raw = json.dumps(obj, separators=(",", ":"))
        if self.writer:
            self.writer.write(recv_ms, raw)
        if self.on_message:
            self._safe(self.on_message, recv_ms, obj)

    def _save_catalog(self, final: bool = False) -> None:
        if not self.catalog.events:
            return
        path = os.path.join(self.out_dir, "catalog.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(self.catalog.to_json())
        os.replace(tmp, path)

    # ------------------------------------------------------------------ discovery
    async def _discovery_loop(self) -> None:
        sess = requests.Session()
        while not self._stop.is_set():
            try:
                raw = await asyncio.to_thread(gamma.fetch_events, self.series_id, session=sess)
                for ev, mk in gamma.parse_events(raw, self.types):
                    self.catalog.add_event(ev, mk)
                tokens, active = gamma.select_tokens(self.catalog, _now_ms(), lead_h=self.lead_h,
                                                     post_min=self.post_min, slugs=self.slugs)
                if tokens != self.desired:
                    self._say(f"discovery: {len(active)} game(s), {len(tokens)} tokens "
                              f"(+{len(tokens - self.desired)} / -{len(self.desired - tokens)})")
                self.desired, self.active_slugs = tokens, active
                self._maybe_auto_stop()
                now = _now_ms()
                for slug in active:
                    e = self.catalog.events[slug]
                    self._emit(now, {"event_type": "_status", "slug": slug, "live": e.live, "ended": e.ended,
                                     "period": e.period, "score": e.score, "elapsed": e.elapsed,
                                     "finished_ms": e.finished_ms})
                self._save_catalog()
                if self.on_catalog:
                    self._safe(self.on_catalog, self.catalog)
            except asyncio.CancelledError:
                raise
            except Exception as exc:     # network hiccup: log and try again next round
                self._say(f"discovery error: {exc!r}")
            await self._sleep(self.refresh_s)

    def _maybe_auto_stop(self) -> None:
        if self.auto_stop_idle_min is None:
            return
        if self.active_slugs:
            self._had_games, self._empty_since = True, None
        elif self._had_games:
            self._empty_since = self._empty_since or time.time()
            if time.time() - self._empty_since >= self.auto_stop_idle_min * 60:
                self._say(f"no active games for {self.auto_stop_idle_min:g} min: stopping (all data is saved)")
                self.stop()

    # ------------------------------------------------------------------ websocket
    async def _ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self.desired:
                await self._sleep(5)
                continue
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._say(f"websocket error: {exc!r}")
            if self._stop.is_set():
                break
            self.reconnects += 1
            self._emit(_now_ms(), {"event_type": "_meta", "what": "disconnect"})
            self._say(f"reconnecting in {backoff:.0f}s")
            await self._sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        async with websockets.connect(config.WS_MARKET, ping_interval=None, max_size=None, open_timeout=20) as ws:
            self._sent = set()
            self._emit(_now_ms(), {"event_type": "_meta", "what": "connect"})
            self._say(f"connected ({len(self.desired)} tokens wanted)")
            state = {"last_rx": time.monotonic()}

            async def sender() -> None:
                last_ping = time.monotonic()
                while True:
                    add = [t for t in self.desired if t not in self._sent]
                    rem = [t for t in self._sent if t not in self.desired]
                    for chunk in _chunks(add, config.SUBSCRIBE_CHUNK):
                        if not self._sent:
                            frame = {"assets_ids": chunk, "type": "market", "custom_feature_enabled": True}
                        else:
                            frame = {"assets_ids": chunk, "operation": "subscribe"}
                        await ws.send(json.dumps(frame))
                        self._sent.update(chunk)
                    for chunk in _chunks(rem, config.SUBSCRIBE_CHUNK):
                        await ws.send(json.dumps({"assets_ids": chunk, "operation": "unsubscribe"}))
                        self._sent.difference_update(chunk)
                    if time.monotonic() - last_ping >= config.PING_SECONDS:
                        await ws.send("PING")
                        last_ping = time.monotonic()
                    await asyncio.sleep(0.5)

            async def receiver() -> None:
                async for raw in ws:
                    state["last_rx"] = time.monotonic()
                    if raw == "PONG":
                        continue
                    self._handle(raw)

            async def watchdog() -> None:
                while True:
                    await asyncio.sleep(5)
                    if time.monotonic() - state["last_rx"] > config.STALE_SECONDS:
                        raise TimeoutError("no frames (not even PONG) for too long")

            tasks = [asyncio.create_task(c) for c in (sender(), receiver(), watchdog())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                t.result()          # re-raise whatever ended the session

    def _handle(self, raw: str) -> None:
        recv_ms = _now_ms()
        m = _ET.search(raw)
        et = m.group(1) if m else "?"
        self.counts[et] += 1
        if et == "new_market":      # fires for EVERY new Polymarket market (crypto 5-minute ones...): not ours
            return
        if self.writer:
            self.writer.write(recv_ms, raw)
        if self.on_message:
            try:
                obj = json.loads(raw)
            except ValueError:
                return
            for item in (obj if isinstance(obj, list) else [obj]):
                self._safe(self.on_message, recv_ms, item)

    # ------------------------------------------------------------------ latency probe
    async def _rtt_loop(self) -> None:
        sess = requests.Session()

        def probe():
            t0 = time.time()
            r = sess.get(f"{config.CLOB_REST}/time", timeout=10)
            t1 = time.time()
            return t0, t1, float(r.text.strip())

        while not self._stop.is_set():
            try:
                t0, t1, server_s = await asyncio.to_thread(probe)
                rtt = (t1 - t0) * 1000.0
                self.rtt_ms.append(rtt)
                self._emit(t1 * 1000.0, {"event_type": "_rtt", "rtt_ms": round(rtt, 1),
                                         "offset_ms": round(server_s * 1000.0 - (t0 + t1) / 2 * 1000.0, 1)})
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await self._sleep(config.RTT_SECONDS)

    # ------------------------------------------------------------------ housekeeping
    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            if self.on_tick:
                self._safe(self.on_tick, _now_ms())
            await self._sleep(0.05)

    async def _stats_loop(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            await self._sleep(30)
            now = time.monotonic()
            dt = max(now - last, 1e-9)
            last = now
            delta = self.counts - self._last_counts
            self._last_counts = collections.Counter(self.counts)
            rates = " ".join(f"{k}={v / dt:.1f}/s" for k, v in sorted(delta.items()))
            rtt = f"{sorted(self.rtt_ms)[len(self.rtt_ms) // 2]:.0f}ms" if self.rtt_ms else "n/a"
            where = os.path.relpath(self.writer.path) if self.writer and self.writer.path else "-"
            self._say(f"games={len(self.active_slugs)} tokens={len(self._sent)} reconnects={self.reconnects} "
                      f"rtt~{rtt} | {rates} | {where}")
