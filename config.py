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
  WF:     4/4 ROBUST
  Stress: 5/5 ROBUST

ATENȚIE 1: Strategia v2 NU plasează TP/SL pe Binance.
Toate închiderile (BE, TRAIL, SL) sunt făcute de Guardian de pe PC.
Guardian-ul TREBUIE să fie v5 (Trailing+BE pentru 4H) — NU v4!

ATENȚIE 2: Acest config conține TOATE constantele cerute de
detector.py și order_manager.py existente în repo.
"""
import os

# ─── API Keys ─────────────────────────────────────────────
API_KEY    = os.environ.get("API_KEY", "")
API_SECRET = os.environ.get("API_SECRET", "")

# ─── Telegram ─────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_HOURS = 6

# ─── Trading ──────────────────────────────────────────────
TIMEFRAME           = "4h"
LEVERAGE            = 10
USDT_PER_TRADE      = 7.0
MAX_OPEN_TRADES     = 25
ORDER_EXPIRY_HOURS  = 8

# ═══════════════════════════════════════════════════════════
#  DETECTOR — Parametri 4H specifici (Rank #1 backtest v4)
# ═══════════════════════════════════════════════════════════
# DIFERIT de 1H — au fost optimizați separat pentru TF 4h
MIN_GAP_PCT         = 0.012   # 1.2% gap minim (1H avea 0.009)
MAX_WICK_RATIO      = 0.20    # 1H avea 0.30
AGGR_FACTOR         = 1.3     # 1H avea 2.0
AVG_BODY_PERIOD     = 50      # nr bare pentru calcul corp mediu
MAX_CONSEC_AGGR     = 1       # max 1 candelă consecutivă agresivă (1H avea 3)

# ─── RSI (la fel ca 1H) ───────────────────────────────────
RSI_PERIOD          = 14
RSI_BULL            = 55      # prag MINIM pentru BULL (RSI >= 55)
RSI_BEAR            = 45      # prag MAXIM pentru BEAR (RSI <= 45)

# ─── EMA ──────────────────────────────────────────────────
EMA_FAST            = 21      # period EMA rapid
EMA_SLOW            = 50      # period EMA lent
EMA_SLOPE_BARS      = 5       # nr bare pentru calcul pantă
EMA_MIN_SLOPE       = 0.002   # 0.2% pantă minimă
EMA_PARALLEL_MIN    = 0.5     # ratio min pentru paralelism EMA
EMA_PARALLEL_MAX    = 3.0     # ratio max pentru paralelism EMA

# ─── Risk Management ───────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 0.20   # 20% din capital/zi

# ─── Blacklist (TradFi-Perps care cer acord separat) ───────
BLACKLIST = [
    # Stable coins / tokens cu issues
    "USDCUSDT", "BUSDUSDT", "FDUSDUSDT",
    
    # TradFi Perpetuals (cer acord separat MiCA)
    "MUUSDT", "MSFTUSDT", "AAPLUSDT", "TSMUSDT", "MSTRUSDT",
    "BABAUSDT", "QQQUSDT", "SPYUSDT", "BARDUSDT", "CRCLUSDT",
    "HOODUSDT", "OPGUSDT", "NATGASUSDT", "AVGOUSDT",
    "CHIPUSDT", "SNDKUSDT", "GENIUSUSDT",
]

# ─── State files ──────────────────────────────────────────
STATE_FILE = "bot_state_4h.json"
LOG_FILE   = "bot_4h.log"
