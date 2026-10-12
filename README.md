# polymarket-nba-arb

A **read-only** study of arbitrage in Polymarket's NBA markets. It replicates and stress-tests the paper
*Arbitrage Analysis in Polymarket NBA Markets* (Cheng, Yang & Zou, arXiv:2605.00864) with an event feed
instead of polling, and asks the question the paper does not: **after reaction time, fees and failed second legs,
is anything left?**

No account, no keys, no orders. Everything uses Polymarket's public data endpoints. Nothing in this repository can
place a trade.

> **Disclaimer.** Research and education only, not financial advice. Not affiliated with or endorsed by Polymarket.
> Prediction markets can be restricted where you live: check the terms and your local rules yourself.

## The idea in plain words

On Polymarket, "Team A wins" and "Team A wins by more than 4.5" are separate markets with separate order books.
Because the second can never be more likely than the first, their prices must respect that. When they don't,
you can buy two tokens that together pay at least $1 in every possible final score for less than $1.
The paper found this does happen in the last minutes of games, but that the amount of money available is tiny.
This project measures it again, more precisely, and checks whether a bot could really have taken it.

## Quick start

```bash
git clone <URL of this repository>        # the green "Code" button on GitHub shows it
cd polymarket-nba-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests      # 28 tests, a couple of seconds, no network
python -m polyarb discover                # which NBA games exist right now (needs internet)
```

Tested on Python 3.11. Then record a game night (see below) and replay it.

## Stages

| stage | command | what it does | risk |
|---|---|---|---|
| 0 | `python -m polyarb record` | listens to the public WebSocket feed and saves every message | none |
| 1 | `python -m polyarb replay data/raw` | rebuilds the books from the recording, runs the paper's two detectors, simulates what a bot would have got | none |
| 2 | `python -m polyarb shadow` | same engine live; logs hypothetical orders only | none (no order can be sent) |
| gate | `python -m polyarb gate ...` | numbers fixed in advance that decide whether stage 3 is even discussed | none |
| 3 | not built | see [`docs/STAGE3.md`](docs/STAGE3.md) | n/a |

## Run a game night

```bash
bash scripts/record_night.sh shadow    # stage 0 + 2 together; stops itself 15 min after the last game
python -m polyarb replay data/raw --out data/analysis/night1
```

Open `data/analysis/night1/summary.md`. Keep the machine awake (the script uses `caffeinate` on macOS; leave the lid open).
To stop new hypothetical orders at once, create a file named `KILL` inside the output folder.

**Cost, measured on one live game:** about 480 messages per second on average (peaks near 900), roughly 20% of one CPU core
in shadow mode, and about 180 MB per live game-hour on disk (gzip). A busy regular-season night can reach 5-10 GB, so check
free disk. Shadow mode does not scale to a full slate in one process (10 live games would need more than two cores): for big
nights run `record` (much lighter) and replay afterwards. Replays are offline and can be repeated: about 18,000 messages per CPU-second, so one full game (~4 million messages) is roughly 4 CPU-minutes. Wall-clock time depends on what else the computer is doing, and a sleeping laptop pauses it entirely.

## What is in the box

```
polyarb/
  catalog.py    games, markets, tokens; payoff vectors that decide which token pairs are guaranteed to pay >= $1
  gamma.py      find NBA games (Gamma API)
  recorder.py   stage 0: WebSocket listener, reconnects, hourly gzip logs, latency probe; analysis can never stop recording
  book.py       order book rebuilt from snapshots + deltas (integer prices, exact arithmetic)
  engine.py     detectors (single-market + combinatorial), episode tracking; the same code runs live and in replay
  execution.py  the pessimistic execution model: latency, book movement, FOK/FAK, leg failure, unwind, fees
  report.py     numbers, the paper comparison, and `polling_emulation` (see below)
  replay.py     stage 1 driver          shadow.py   stage 2 driver          gate.py   the go / no-go checks
tests/          28 unit tests with hand-checkable numbers
```

## Two questions it answers that the paper cannot

1. **Is "3.6 seconds" real?** The paper polled every 3.6-5.5 s and credited each sighting with the gap to the next
   snapshot, so a median near that interval is partly an artifact of sampling. `polling_emulation` re-observes our
   event-resolution episodes the way their poller would and prints both side by side.
2. **Detected is not executed.** The execution model replays the REAL book at the moment a simulated order would
   arrive, charges the taker fee read from each market's own `feeSchedule`, and handles one leg filling while the other
   does not.

## Checks done so far

- Rebuilt order books agree with the feed's own best bid/ask claims in about 99.9% of 1.2 million live checks (a live game at ~500 messages per second).
- The detectors and the execution model are unit-tested on hand-made books; real in-game results are still being collected.

## Honest limits

- One night proves nothing: the gate asks for 20+ games over 3+ days before it will even look positive.
- Number of markets per game varies a lot (a handful in preseason, more in the regular season), so early results
  understate the regular-season opportunity set.
- The model ignores other bots except through what is visible in the recorded book, and assumes your own orders
  do not move the market (true at 5-100 shares).
- `secondsDelay` was 0 on NBA markets when checked; if Polymarket adds a matching delay, the model reads it per market.
- Facts about fees, geography and endpoints were checked on 2026-10-07 and can change.

## Reference

Cheng, G., Yang, J., Zou, H. (2026). *Arbitrage Analysis in Polymarket NBA Markets.* arXiv:2605.00864 (preprint).
