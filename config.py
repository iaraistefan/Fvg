import os

API_KEY    = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")

TIMEFRAME  = "4h"

LEVERAGE        = 10
USDT_PER_TRADE  = 7    # redus de la 20 — evita pozitii prea mari

MIN_GAP_PCT      = 0.009
MAX_WICK_RATIO   = 0.20
AGGR_FACTOR      = 1.5
AVG_BODY_PERIOD  = 20

RSI_PERIOD = 14
RSI_BULL   = 50
RSI_BEAR   = 50

EMA_FAST         = 50
EMA_SLOW         = 100
EMA_SLOPE_BARS   = 4
EMA_MIN_SLOPE    = 0.002
EMA_PARALLEL_MIN = 0.25
EMA_PARALLEL_MAX = 4.0

MAX_CONSEC_AGGR  = 1

SCAN_INTERVAL_SEC  = 60
MAX_OPEN_TRADES    = 15
ORDER_EXPIRY_HOURS = 8

# 1000WHYUSDT adaugat — da erori repetate la plasare ordin
BLACKLIST = [
    "BTCDOMUSDT", "DEFIUSDT", "XPDUSDT",
    "1000WHYUSDT", "USDCUSDT",
]

TELEGRAM_ENABLED      = True
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_HOURS = 4

LOG_FILE = "fvg_bot.log"

# ─── FILTRE SIGURANTA ──────────────────────────────────────
# Volum minim 24h — evita simboluri micro-cap manipulabile

# Emergency close — inchide automat daca pierdere > 30%
# si SL nu s-a executat (slippage extrem pe micro-cap)
# MAX_LOSS_PCT_EMERGENCY = 0.30  # eliminat      # 30% din notional

# Blacklist extins cu simboluri problematice confirmate
BLACKLIST = [
    "BTCDOMUSDT", "DEFIUSDT", "XPDUSDT",
    "1000WHYUSDT", "USDCUSDT", "INTCUSDT",
    "PARTIUSDT", "TNSRUSDT", "DYMUSDT",
    "HIPPOUSDT", "CROSSUSDT",
]

DAILY_LOSS_LIMIT_PCT = 0.20   # 20% din capital/zi
