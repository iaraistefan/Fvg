"""
FVG WITH-TREND BOT v3

Modificari:
- MAX_OPEN_TRADES = 15 (doar pozitii UMPLUTE)
- Scanare se opreste cand sunt 15 pozitii, reia cand scade la 14
- Monitorizeaza DOAR tradurile deschise de bot (nu manual)
- Ordine LIMIT expirate dupa ORDER_EXPIRY_HOURS
- Raport Telegram la fiecare TELEGRAM_REPORT_HOURS ore
"""
import sys
import io
import time
import logging
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
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("FVGBot")


class FVGBot:
    def __init__(self):
        self.client = Client(config.API_KEY, config.API_SECRET)
        self.om     = OrderManager(self.client)

        # Tracking lumanari pentru a evita semnale duplicate
        self.last_candle_ts = {}

        # Tracking raport Telegram
        self.last_report_time = time.time()

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG WITH-TREND BOT v3 pornit")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  EMA: {config.EMA_FAST}/{config.EMA_SLOW} | Slope min: {config.EMA_MIN_SLOPE*100:.1f}%/{config.EMA_SLOPE_BARS}bars")
        logger.info(f"  Max pozitii: {config.MAX_OPEN_TRADES} | Expiry ordin: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  Raport Telegram la fiecare {config.TELEGRAM_REPORT_HOURS}h")
        logger.info("═══════════════════════════════════════════════════════")

    # ── Symbols ───────────────────────────────────────────────

    def get_symbols(self) -> list:
        try:
            info = self.client.futures_exchange_info()
            return [
                s["symbol"] for s in info["symbols"]
                if s["symbol"].endswith("USDT")
                and s["status"] == "TRADING"
                and s["symbol"] not in config.BLACKLIST
            ]
        except Exception as e:
            logger.error(f"get_symbols error: {e}")
            return []

    # ── Klines ────────────────────────────────────────────────

    def get_klines(self, symbol: str) -> list:
        try:
            klines = self.client.futures_klines(
                symbol=symbol,
                interval=config.TIMEFRAME,
                limit=200
            )
            return klines[:-1]  # exclude lumanarea curenta (inca deschisa)
        except BinanceAPIException as e:
            if e.code != -1121:
                logger.warning(f"[{symbol}] klines error: {e}")
            return []

    # ── Scan symbol ───────────────────────────────────────────

    def scan_symbol(self, symbol: str):
        """Scaneaza un simbol pentru setup FVG."""
        klines = self.get_klines(symbol)
        if not klines:
            return

        df      = prepare_df(klines)
        last_ts = df.index[-1]

        # Nu rescanam aceeasi lumanare
        if self.last_candle_ts.get(symbol) == last_ts:
            return

        setup = detect_fvg(symbol, df)
        if setup is None:
            return

        logger.info(
            f"[{symbol}] ✅ FVG {setup.direction} detectat | "
            f"RSI={setup.rsi} | Entry={setup.entry:.6f} | "
            f"SL={setup.sl:.6f} | TP={setup.tp:.6f} | "
            f"EMA Slope={setup.slope_fast:+.3f}%"
        )

        # Verifica daca avem deja ordin/pozitie pe acest simbol (ale botului)
        if self.om.has_symbol(symbol):
            logger.info(f"[{symbol}] SKIP — bot are deja ordin/pozitie pe acest simbol")
            self.last_candle_ts[symbol] = last_ts
            return

        # Verifica limita de 15 pozitii UMPLUTE
        # (ordinele pending NU se numara in limita)
        if self.om.is_at_capacity():
            logger.info(
                f"[{symbol}] SKIP — limita {config.MAX_OPEN_TRADES} pozitii atinsa "
                f"({self.om.count_open_positions()} deschise)"
            )
            return

        # Plaseaza ordinul
        notify_setup(setup)
        success = self.om.place_fvg_trade(setup)
        notify_trade(setup, success)

        if success:
            self.last_candle_ts[symbol] = last_ts
            logger.info(
                f"[{symbol}] Ordin plasat | "
                f"Pozitii: {self.om.count_open_positions()}/{config.MAX_OPEN_TRADES} | "
                f"Pending: {self.om.count_pending_orders()}"
            )

    # ── Raport Telegram ───────────────────────────────────────

    def check_and_send_report(self):
        """Trimite raport statistic la fiecare TELEGRAM_REPORT_HOURS ore."""
        interval_sec = config.TELEGRAM_REPORT_HOURS * 3600
        if time.time() - self.last_report_time >= interval_sec:
            stats = self.om.get_stats_for_report()
            send_statistics_report(stats)
            self.last_report_time = time.time()
            logger.info("Raport Telegram trimis.")

    # ── Ciclu principal ───────────────────────────────────────

    def run_cycle(self):
        # 1. Verifica ordine umplute, SL/TP atinse, ordine expirate
        self.om.check_filled_orders()

        open_count    = self.om.count_open_positions()
        pending_count = self.om.count_pending_orders()

        # 2. Daca suntem la capacitate maxima (15 pozitii umplute) — nu scana
        if self.om.is_at_capacity():
            logger.info(
                f"⏸  Scanare PAUZA — {open_count}/{config.MAX_OPEN_TRADES} pozitii deschise "
                f"| {pending_count} pending"
            )
            return

        # 3. Scaneaza toate simbolurile
        symbols = self.get_symbols()
        logger.info(
            f"▶  Scanez {len(symbols)} perechi | "
            f"Pozitii: {open_count}/{config.MAX_OPEN_TRADES} | "
            f"Pending: {pending_count}"
        )

        for sym in symbols:
            # Re-verifica limita in bucla (poate fi atinsa in cursul scanarii)
            if self.om.is_at_capacity():
                logger.info(f"⏸  Limita atinsa in timpul scanarii — opresc")
                break
            try:
                self.scan_symbol(sym)
            except Exception as e:
                logger.error(f"[{sym}] Eroare scan: {e}")
            time.sleep(0.25)  # 0.25s = max 240 req/min, sub limita de 2400

        logger.info(
            f"Ciclu complet | {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
            f"Pozitii: {self.om.count_open_positions()}/{config.MAX_OPEN_TRADES} | "
            f"Pending: {self.om.count_pending_orders()}"
        )

        # 4. Verifica daca e timpul pentru raport Telegram
        self.check_and_send_report()

    # ── Run ───────────────────────────────────────────────────

    def run(self):
        logger.info("Bot pornit. Ctrl+C pentru oprire.")
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logger.info("Bot oprit de utilizator.")
                break
            except Exception as e:
                logger.error(f"Eroare ciclu principal: {e}")
                notify_error("Ciclu principal", str(e))

            logger.info(f"Astept {config.SCAN_INTERVAL_SEC}s...")
            time.sleep(config.SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    FVGBot().run()
