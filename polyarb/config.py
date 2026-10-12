"""Central settings. Everything you might want to tweak lives here, with plain-English notes."""
from __future__ import annotations

# ---- public endpoints (no account / keys needed for any of these) ----
CLOB_REST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# NBA series id comes from GET {GAMMA_API}/sports (the entry with sport == "nba").
NBA_SERIES_ID = 10345

# Market types we record. Player props are skipped: the paper's detectors don't use them
# and they are tiny/illiquid (the paper reports a mean volume of about $253 per prop market).
RECORD_TYPES = (
    "moneyline", "spreads", "totals",
    "first_half_moneyline", "first_half_spreads", "first_half_totals",
)

# Prices are stored as integers in millionths of a dollar so that sums like 0.995 + 0.005
# are exact (floats can give 0.9999999999999999).
PRICE_SCALE = 1_000_000

# ---- detector filters, copied from the paper (Appendix A.3 / B.1) ----
MIN_NOTIONAL_USD = 10.0      # bottleneck leg must offer at least $10 of size
MIN_ORDER_SHARES = 5.0       # Polymarket's minimum order size seen in the book metadata

# ---- recorder behaviour ----
LEAD_HOURS = 3.0             # start recording an NBA game this long before tip-off
POST_MINUTES = 30.0          # keep recording this long after the game is finished (post-game ghosts!)
REFRESH_SECONDS = 60.0       # how often to re-discover games and markets
PING_SECONDS = 10.0          # the docs ask for a text "PING" at least every 10 s
STALE_SECONDS = 45.0         # no frame (not even PONG) for this long -> reconnect
SUBSCRIBE_CHUNK = 100        # tokens per subscribe frame
RTT_SECONDS = 20.0           # how often to time a REST call (gives us our own real latency)

# ---- execution model defaults (stage 1 / stage 2) ----
DEFAULT_FEE_RATE = 0.05      # sports taker fee rate; each market's real value is read from the API
CAP_USD = 100.0              # paper's per-episode budget
DAILY_LOSS_CAP_USD = 50.0    # hypothetical daily stop used inside the simulator
COMPUTE_MS = 5.0             # time our own code needs to react after a message arrives

# ---- go / no-go gate (docs/STAGE3.md explains each line) ----
GATE_MIN_GAMES = 20
GATE_MIN_GAME_DAYS = 3
GATE_MIN_EPISODES = 30
GATE_MIN_NET_PER_GAME_USD = 2.0
GATE_MAX_LEG_FAILURE_SHARE = 0.25
GATE_BOOTSTRAP_DRAWS = 2000
GATE_MIN_SHADOW_GAMES = 10   # stage 2 evidence needed before stage 3 is even discussed
