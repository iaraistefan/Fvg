"""
═══════════════════════════════════════════════════════════
  CONFIG BOT 4H — Strategie v2 (Trailing + BE + No-TP)
═══════════════════════════════════════════════════════════
Parametri Rank #1 din backtest v4 (5,250 simulări validate):

Performance așteptată:
  WR:     63.8%
  PF:     2.46
  PnL:    +1,916 USDT (pe 365 zile, capital 175 USDT)
  DD max: 3.97%
  WF:     4/4 ROBUST (walk-forward 4 ferestre)
  Stress: 5/5 ROBUST (slippage, perturbații, BTC regime)

ATENȚIE: Strategia v2 NU plasează TP/SL pe Binance.
Toate închiderile (BE, TRAIL, SL) sunt făcute de Guardian de pe PC.
Guardian-ul TREBUIE să fie v5 (Trailing+BE pentru 4H) — NU v4!
"""
import os

# ─── API Keys ─────────────────────────────────────────────
API_KEY    = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")

# ─── Telegram ─────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_HOURS = 6  # raport la 6 ore (4 pe zi)

# ─── Trading ──────────────────────────────────────────────
TIMEFRAME           = "4h"        # ← cheia diferenței față de 1H
LEVERAGE            = 10
USDT_PER_TRADE      = 7.0
MAX_OPEN_TRADES     = 25
ORDER_EXPIRY_HOURS  = 8

# ─── Detector v2 — Parametri 4H (Rank #1 backtest) ────────
# DIFERIT de 1H — nu copia parametrii 1H!
MIN_GAP_PCT         = 0.012   # 1.2% gap minim (1H avea 0.009)
MIN_GAP_ATR_MULT    = 0.0     # NU mai filtrează ATR (1H avea 0.8)
AGGR_FACTOR         = 1.3     # candela aggressive ratio (1H avea 2.0)
MAX_WICK_RATIO      = 0.20    # wick max 20% (1H avea 0.30)
MAX_CONSEC_AGGR     = 1       # max 1 candelă aggressive consecutivă (1H avea 3)

# ─── Detector — RSI + EMA (la fel ca 1H) ───────────────────
RSI_PERIOD          = 14
RSI_BULL_MIN        = 55
RSI_BULL_MAX        = 100
RSI_BEAR_MIN        = 0
RSI_BEAR_MAX        = 45

EMA_FAST_PERIOD     = 21
EMA_SLOW_PERIOD     = 50
EMA_MIN_SLOPE       = 0.002   # 0.2% slope minim

ATR_PERIOD          = 14

# ─── Entry — mid-gap (la fel ca 1H) ────────────────────────
ENTRY_FILL_RATIO    = 0.5

# ─── Risk Management ───────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 0.20    # 20% din capital/zi

# ─── Blacklist (TradFi-Perps care cer acord separat) ───────
BLACKLIST = [
    # Stable coins / tokens cu issues
    "USDCUSDT", "BUSDUSDT", "FDUSDUSDT",
    
    # TradFi Perpetuals (cer acord MiCA)
    "MUUSDT", "MSFTUSDT", "AAPLUSDT", "TSMUSDT", "MSTRUSDT",
    "BABAUSDT", "QQQUSDT", "SPYUSDT", "BARDUSDT", "CRCLUSDT",
    "HOODUSDT", "OPGUSDT", "NATGASUSDT", "AVGOUSDT",
    "CHIPUSDT", "SNDKUSDT", "GENIUSUSDT",
]

# ─── State files ──────────────────────────────────────────
STATE_FILE = "bot_state_4h.json"
LOG_FILE   = "bot_4h.log"
