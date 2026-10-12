# Stage 3: tiny live trading (CLOSED)

**Nothing in this repository can place an order. There are no keys, no wallet code, no order-signing code.**
Stage 3 is intentionally not built. This page says when it may be opened and what must be true first.

## Step 1: the numbers gate (automatic)

```bash
python3 -m polyarb gate data/analysis/<replay>/summary.json --shadow data/shadow/summary-<tag>/summary.json
```

It checks, using thresholds fixed in `polyarb/config.py` BEFORE looking at results:

| check | why |
|---|---|
| >= 20 usable games over >= 3 game days | one hot night proves nothing |
| >= 30 simulated trades | too few trades = anecdotes (the paper's single-market result had n = 7) |
| net profit per game > $2 after reaction time, book movement, leg failures, unwinds and the market's real fees | the paper's edge is about $3 a game BEFORE any of that |
| 90% bootstrap interval (resampled by game) above zero | separates an edge from a lucky week |
| leg failures <= 25% of filled trades | a failed second leg turns an arbitrage into a bet on a game |
| fees are modelled, not the paper's zero | NBA markets now carry a taker fee (checked in the API: rate 0.05) |
| >= 10 games in shadow mode and shadow agrees with replay | live behaviour must match the simulation |

If it prints `CLOSED`, stage 3 stays closed. Record more, or accept the result: "no edge after frictions" is a real finding and a good blog post.

## Step 2: the manual checklist (only if the gate prints `OPEN`)

1. **Read Polymarket's current Terms and the geographic restrictions page yourself.** Check YOUR country: as of 2026-10-07 the page listed several countries (for example Germany and the UK) as close-only, meaning you may close positions but not open new ones. Rules change. Never use a VPN to get around a block.
2. **Money you can lose entirely.** Decide the amount in advance; it must not touch money you need for living costs, tuition or savings goals. Start with the minimum order size (5 shares).
3. **A dedicated wallet** holding only that amount. Never your main wallet.
4. **Keys never in files.** Environment variable or OS keychain only. Not in this repo, not in a notes app, not in any synced folder, never pasted into a chat with an AI.
5. **Limits in code**: per-trade cap, daily loss cap, max open orders, a `KILL` file that stops new orders (already implemented for shadow mode in `Scenario` / `Engine.killed`).
6. **Order type**: FOK (fill-or-kill) on both legs, sent as one batch; unwind logic tested against the simulator first.
7. **Dry run first** against the shadow executor with the exact same code path, for a full week.
8. **Review daily**: compare real fills with what shadow mode predicted for the same games. Any large gap means stop.

## What a live adapter would look like

A small class with the same interface the simulator already uses (`on_signal(episode, signal)`, `advance(now)`), whose
`entry` step sends two FOK orders instead of reading the recorded book. Writing it is about a day of work once
steps 1-2 pass; it is deliberately not written now so nobody can run it by accident.
