"""
FVG WITH-TREND BOT 4H — v4
Fix-uri aplicate:
  1. Double-loop: CHECK la 10s + SCAN la 60s
  2. last_candle_ts setat indiferent de rezultatul plasarii
  3. Offset 30s la startup fata de botul 1H
"""
import sys, io, time, logging
from datetime import datetime, timezone

from binance.client import Client
from binance.exceptions import BinanceAPIException

import config
from detector import detect_fvg, prepare_df
from order_manager import OrderManager
from notifier import notify_setup, notify_trade, notify_error, send_statistics_report

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("FVGBot")


class FVGBot:
    def __init__(self):
        self.client           = Client(config.API_KEY, config.API_SECRET)
        self.om               = OrderManager(self.client)
        self.last_candle_ts   = {}
        self.last_report_time = time.time()
        self.stats = {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG WITH-TREND BOT 4H — v4")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  EMA: {config.EMA_FAST}/{config.EMA_SLOW} | Slope: {config.EMA_MIN_SLOPE*100:.1f}%/{config.EMA_SLOPE_BARS}bars")
        logger.info(f"  Max pozitii: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info("═══════════════════════════════════════════════════════")

    # ─────────────────────────────────────────────
    #  SIMBOLURI + KLINES
    # ─────────────────────────────────────────────

    def get_symbols(self) -> list:
        now_ts = time.time()
        cache  = getattr(self, "_symbols_cache", [])
        if cache and (now_ts - getattr(self, "_symbols_ts", 0) < 900):
            return cache
        try:
            info = self.client.futures_exchange_info()
            syms = [s["symbol"] for s in info["symbols"]
                    if s["symbol"].endswith("USDT")
                    and s["status"] == "TRADING"
                    and s["symbol"] not in config.BLACKLIST]
            self._symbols_cache = syms
            self._symbols_ts    = now_ts
            logger.info(f"Simboluri actualizate: {len(syms)}")
            return syms
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("Rate limit get_symbols — astept 60s si retry...")
                time.sleep(60)
                try:
                    info = self.client.futures_exchange_info()
                    syms = [s["symbol"] for s in info["symbols"]
                            if s["symbol"].endswith("USDT")
                            and s["status"]=="TRADING"
                            and s["symbol"] not in config.BLACKLIST]
                    self._symbols_cache = syms
                    self._symbols_ts = time.time()
                    return syms
                except Exception:
                    return cache
            else:
                logger.error(f"get_symbols error: {e}")
            return cache
        except Exception as e:
            logger.error(f"get_symbols error: {e}")
            return cache

    def get_klines(self, symbol: str) -> list:
        try:
            klines = self.client.futures_klines(
                symbol=symbol, interval=config.TIMEFRAME, limit=200
            )
            return klines[:-1]
        except BinanceAPIException as e:
            if e.code == -1003:
                raise  # propagheaza in sus → break din for loop
            if e.code != -1121:
                logger.warning(f"[{symbol}] klines: {e}")
            return []
        except Exception as e:
            logger.warning(f"[{symbol}] klines: {e}")
            return []

    # ─────────────────────────────────────────────
    #  SCAN SIMBOL
    # ─────────────────────────────────────────────

    def scan_symbol(self, symbol: str):
        klines = self.get_klines(symbol)
        if not klines:
            return

        df      = prepare_df(klines)
        last_ts = df.index[-1]

        if self.last_candle_ts.get(symbol) == last_ts:
            return

        setup = detect_fvg(symbol, df)

        # FIX: seteaza last_candle_ts INDIFERENT de rezultat
        # Evita detectarea repetata a aceluiasi semnal
        self.last_candle_ts[symbol] = last_ts

        if setup is None:
            return

        logger.info(f"[{symbol}] FVG {setup.direction} | RSI={setup.rsi} | "
                    f"Entry={setup.entry:.6f} | SL={setup.sl:.6f} | "
                    f"TP={setup.tp:.6f} | Slope={setup.slope_fast:+.3f}%")

        if self.om.has_symbol(symbol):
            return

        if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
            logger.info(f"[{symbol}] SKIP — limita {config.MAX_OPEN_TRADES} atinsa")
            return

        notify_setup(setup)
        success = self.om.place_fvg_trade(setup)
        notify_trade(setup, success)

    # ─────────────────────────────────────────────
    #  RAPORT
    # ─────────────────────────────────────────────

    def check_and_send_report(self):
        if time.time() - self.last_report_time >= config.TELEGRAM_REPORT_HOURS * 3600:
            bstats = self.om.get_bot_stats()
            send_statistics_report({
                "total_trades":   bstats["total"],
                "wins":           bstats["wins"],
                "losses":         bstats["losses"],
                "expired_orders": bstats["expired"],
                "pending":        bstats["pending"],
                "open_positions": bstats["active"],
                "pnl_total":      bstats["pnl_total"],
                "pnl_today":      bstats["pnl_today"],
                "win_rate":       bstats["win_rate"],
                "best_trade":     bstats["best"],
                "worst_trade":    bstats["worst"],
                "commission_paid":0.0,
                "start_time":     self.stats["start"],
            })
            self.last_report_time = time.time()
            logger.info("Raport Telegram trimis.")

    # ─────────────────────────────────────────────
    #  RUN — DOUBLE LOOP
    # ─────────────────────────────────────────────

    def run(self):
        """
        Double-loop:
        - CHECK LOOP (10s): check_filled_orders — detecteaza umpleri rapid,
          plaseaza SL/TP imediat ce ordinul e umplut
        - SCAN LOOP (60s): scaneaza simboluri, deschide ordine noi

        Offset 30s la startup fata de botul 1H — evita
        scanare simultana pe acelasi IP Render.
        """
        logger.info("Startup offset 30s (evita rate limit cu botul 1H)...")
        time.sleep(30)

        logger.info("Reconciliere cu Binance...")
        self.om.reconcile_with_binance()
        logger.info("Bot pornit. Ctrl+C pentru oprire.")

        CHECK_INTERVAL = 10   # verifica umpleri la fiecare 10s
        SCAN_INTERVAL  = 60   # scaneaza simboluri la fiecare 60s

        last_check = 0
        last_scan  = 0

        while True:
            try:
                now = time.time()

                # ── CHECK LOOP (10s) ──────────────────────────
                if now - last_check >= CHECK_INTERVAL:
                    try:
                        self.om.check_filled_orders()
                    except BinanceAPIException as e:
                        if e.code == -1003:
                            logger.warning("Rate limit check — astept 30s...")
                            time.sleep(30)
                        else:
                            logger.error(f"Check error: {e}")
                    except Exception as e:
                        logger.error(f"Check error: {e}")
                    last_check = time.time()

                # ── SCAN LOOP (60s) ───────────────────────────
                if now - last_scan >= SCAN_INTERVAL:
                    active  = self.om.count_active_trades()
                    pending = len(self.om.pending_orders)

                    if active >= config.MAX_OPEN_TRADES:
                        logger.info(f"PAUZA — {active}/{config.MAX_OPEN_TRADES} pozitii")
                    else:
                        symbols = self.get_symbols()[:100]
                        logger.info(f"Scanez {len(symbols)} perechi | "
                                    f"Pozitii: {active}/{config.MAX_OPEN_TRADES} | "
                                    f"Pending: {pending}")

                        for sym in symbols:
                            if self.om.count_active_trades() >= config.MAX_OPEN_TRADES:
                                logger.info("Limita atinsa — opresc scanarea")
                                break
                            try:
                                self.scan_symbol(sym)
                            except BinanceAPIException as e:
                                if e.code == -1003:
                                    logger.warning("Rate limit scan — astept 60s...")
                                    time.sleep(60)
                                    break
                                else:
                                    logger.error(f"[{sym}] BinanceError: {e}")
                            except Exception as e:
                                logger.error(f"[{sym}] Eroare: {e}")
                            time.sleep(0.15)

                        logger.info(f"Ciclu complet | {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
                                    f"Pozitii: {self.om.count_active_trades()}/{config.MAX_OPEN_TRADES} | "
                                    f"Pending: {len(self.om.pending_orders)}")

                    self.check_and_send_report()
                    last_scan = time.time()

                time.sleep(2)  # sleep mic — loop rapid

            except KeyboardInterrupt:
                logger.info("Bot oprit.")
                break
            except Exception as e:
                logger.error(f"Eroare loop: {e}")
                notify_error("Loop 4H", str(e))
                time.sleep(10)


if __name__ == "__main__":
    FVGBot().run()
