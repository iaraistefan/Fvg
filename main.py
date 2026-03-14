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
        self.client           = Client(config.API_KEY, config.API_SECRET)
        self.om               = OrderManager(self.client)
        self.last_candle_ts   = {}
        self.last_report_time = time.time()

        logger.info("═══════════════════════════════════════════════════════")
        logger.info("  FVG WITH-TREND BOT v3 pornit")
        logger.info(f"  TF: {config.TIMEFRAME} | Leverage: {config.LEVERAGE}x | USDT/trade: {config.USDT_PER_TRADE}")
        logger.info(f"  EMA: {config.EMA_FAST}/{config.EMA_SLOW} | Slope min: {config.EMA_MIN_SLOPE*100:.1f}%/{config.EMA_SLOPE_BARS}bars")
        logger.info(f"  Max pozitii: {config.MAX_OPEN_TRADES} | Expiry: {config.ORDER_EXPIRY_HOURS}h")
        logger.info(f"  Raport Telegram la fiecare {config.TELEGRAM_REPORT_HOURS}h")
        logger.info("═══════════════════════════════════════════════════════")

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

    def get_klines(self, symbol: str) -> list:
        try:
            klines = self.client.futures_klines(
                symbol=symbol,
                interval=config.TIMEFRAME,
                limit=200
            )
            return klines[:-1]
        except BinanceAPIException as e:
            if e.code != -1121:
                logger.warning(f"[{symbol}] klines error: {e}")
            return []

    def scan_symbol(self, symbol: str):
        klines = self.get_klines(symbol)
        if not klines:
            return

        df      = prepare_df(klines)
        last_ts = df.index[-1]

        if self.last_candle_ts.get(symbol) == last_ts:
            return

        setup = detect_fvg(symbol, df)
        if setup is None:
            return

        logger.info(
            f"[{symbol}] ✅ FVG {setup.direction} | "
            f"RSI={setup.rsi} | Entry={setup.entry:.6f} | "
            f"SL={setup.sl:.6f} | TP={setup.tp:.6f} | "
            f"Slope={setup.slope_fast:+.3f}%"
        )

        if self.om.has_symbol(symbol):
            logger.info(f"[{symbol}] SKIP — bot are deja ordin/pozitie")
            self.last_candle_ts[symbol] = last_ts
            return

        if self.om.is_at_capacity():
            logger.info(
                f"[{symbol}] SKIP — limita {config.MAX_OPEN_TRADES} pozitii atinsa"
            )
            return

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

    def check_and_send_report(self):
        interval_sec = config.TELEGRAM_REPORT_HOURS * 3600
        if time.time() - self.last_report_time >= interval_sec:
            stats = self.om.get_stats_for_report()
            send_statistics_report(stats)
            self.last_report_time = time.time()
            logger.info("Raport Telegram trimis.")

    def run_cycle(self):
        self.om.check_filled_orders()

        open_count    = self.om.count_open_positions()
        pending_count = self.om.count_pending_orders()

        if self.om.is_at_capacity():
            logger.info(
                f"⏸  PAUZA — {open_count}/{config.MAX_OPEN_TRADES} pozitii | "
                f"{pending_count} pending"
            )
            return

        symbols = self.get_symbols()
        logger.info(
            f"▶  Scanez {len(symbols)} perechi | "
            f"Pozitii: {open_count}/{config.MAX_OPEN_TRADES} | "
            f"Pending: {pending_count}"
        )

        for sym in symbols:
            if self.om.is_at_capacity():
                logger.info("⏸  Limita atinsa — opresc scanarea")
                break
            try:
                self.scan_symbol(sym)
            except Exception as e:
                logger.error(f"[{sym}] Eroare: {e}")
            time.sleep(0.12)

        logger.info(
            f"Ciclu complet | {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC | "
            f"Pozitii: {self.om.count_open_positions()}/{config.MAX_OPEN_TRADES} | "
            f"Pending: {self.om.count_pending_orders()}"
        )

        self.check_and_send_report()

    def run(self):
        logger.info("Bot pornit. Ctrl+C pentru oprire.")
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logger.info("Bot oprit.")
                break
            except Exception as e:
                logger.error(f"Eroare ciclu: {e}")
                notify_error("Ciclu principal", str(e))

            logger.info(f"Astept {config.SCAN_INTERVAL_SEC}s...")
            time.sleep(config.SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    FVGBot().run()
