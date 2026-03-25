"""
Order Manager v5 — urmareste DOAR tradurile acestui bot
"""
import logging
import hmac
import hashlib
import time as t
import requests as req
from urllib.parse import urlencode
from binance.client import Client
from binance.exceptions import BinanceAPIException
from detector import FVGSetup
import config
from config import LEVERAGE, USDT_PER_TRADE

logger = logging.getLogger("FVGBot")
FAPI = "https://fapi.binance.com"


class OrderManager:
    def __init__(self, client: Client):
        self.client = client
        self._precision_cache = {}

        # Ordine LIMIT plasate, asteptand umplere
        self.pending_orders: dict = {}

        # Pozitii ACTIVE (umplute, cu SL/TP)
        # {symbol: {direction, entry, sl, tp, qty, open_time, open_ts, rsi, slope}}
        self.active_positions: dict = {}

        # Trade-uri INCHISE ale ACESTUI bot (nu alte boturi/manuale)
        self.closed_trades: list = []

    # ─────────────────────────────────────────
    #  UTILS
    # ─────────────────────────────────────────

    def _get_symbol_info(self, symbol: str) -> dict:
        if symbol not in self._precision_cache:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                if s["symbol"] == symbol:
                    tick_size = float(next(
                        f["tickSize"] for f in s["filters"]
                        if f["filterType"] == "PRICE_FILTER"
                    ))
                    self._precision_cache[symbol] = {
                        "price_prec": int(s["pricePrecision"]),
                        "qty_prec":   int(s["quantityPrecision"]),
                        "tick_size":  tick_size,
                    }
                    break
        return self._precision_cache.get(symbol, {})

    def _round_price(self, price: float, tick: float, decimals: int) -> float:
        return round(round(price / tick) * tick, decimals)

    def _calc_qty(self, entry: float, info: dict) -> float:
        return round((USDT_PER_TRADE * LEVERAGE) / entry, info["qty_prec"])

    def _algo_signed_post(self, params: dict) -> dict:
        """POST la /fapi/v1/algoOrder cu parametrii in body (data=)."""
        params["timestamp"] = int(t.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            config.API_SECRET.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        try:
            resp = req.post(
                url     = FAPI + "/fapi/v1/algoOrder",
                data    = params,
                headers = {"X-MBX-APIKEY": config.API_KEY},
                timeout = 15
            )
            if not resp.text.strip():
                return {"error": f"Empty response HTTP {resp.status_code}"}
            return resp.json()
        except Exception as e:
            return {"error": str(e)}

    # ─────────────────────────────────────────
    #  SL / TP PLACEMENT
    # ─────────────────────────────────────────

    def _place_conditional(self, symbol: str, side: str,
                           order_type: str, trigger_price: float,
                           qty: float) -> bool:
        label = "SL" if "STOP" in order_type else "TP"
        params = {
            "algoType":     "CONDITIONAL",
            "symbol":       symbol,
            "side":         side,
            "type":         order_type,
            "triggerPrice": str(trigger_price),
            "quantity":     str(qty),
            "reduceOnly":   "true",
            "workingType":  "CONTRACT_PRICE",
        }
        data = self._algo_signed_post(params)
        if "algoId" in data or "orderId" in data:
            logger.info(f"[{symbol}] {label} plasat @ {trigger_price} | algoId={data.get('algoId','?')}")
            return True
        logger.error(f"[{symbol}] {label} ESUAT: {data}")
        return False

    # ─────────────────────────────────────────
    #  VERIFICA ORDINE PENDING
    # ─────────────────────────────────────────

    def check_filled_orders(self):
        self._check_pending()
        self._check_active_positions()
        self._expire_old_orders()

    def _check_pending(self):
        to_remove = []
        for symbol, data in list(self.pending_orders.items()):
            try:
                order  = self.client.futures_get_order(
                    symbol=symbol, orderId=data["order_id"]
                )
                status = order.get("status", "")

                if status == "FILLED":
                    filled_price = float(order.get("avgPrice", data["entry"]))
                    logger.info(f"[{symbol}] Ordin UMPLUT la {filled_price} — plasez SL + TP...")
                    t.sleep(0.5)

                    close_side = data["close_side"]
                    sl_ok = self._place_conditional(symbol, close_side, "STOP_MARKET",        data["sl"], data["qty"])
                    t.sleep(0.3)
                    tp_ok = self._place_conditional(symbol, close_side, "TAKE_PROFIT_MARKET", data["tp"], data["qty"])

                    if sl_ok and tp_ok:
                        logger.info(f"[{symbol}] SL + TP plasate cu succes!")
                    elif not sl_ok:
                        logger.warning(f"[{symbol}] SL a esuat — PERICOL!")
                    elif not tp_ok:
                        logger.warning(f"[{symbol}] TP a esuat!")

                    # Muta in active_positions pentru tracking
                    self.active_positions[symbol] = {
                        "direction": data.get("direction", "?"),
                        "entry":     filled_price,
                        "sl":        data["sl"],
                        "tp":        data["tp"],
                        "qty":       data["qty"],
                        "open_time": data.get("open_time", ""),
                        "open_ts":   data.get("open_ts", int(t.time() * 1000)),
                        "rsi":       data.get("rsi", 0.0),
                        "slope":     data.get("slope", 0.0),
                    }
                    to_remove.append(symbol)

                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.info(f"[{symbol}] Ordin {status}")
                    # Log ordin expirat
                    self.closed_trades.append({
                        "symbol":     symbol,
                        "direction":  data.get("direction", "?"),
                        "entry":      data.get("entry", 0),
                        "sl":         data["sl"],
                        "tp":         data["tp"],
                        "result":     "EXPIRED",
                        "pnl":        0.0,
                        "open_time":  data.get("open_time", ""),
                        "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    })
                    to_remove.append(symbol)

            except Exception as e:
                logger.error(f"[{symbol}] check_pending error: {e}")

        for sym in to_remove:
            self.pending_orders.pop(sym, None)

    def _check_active_positions(self):
        """
        Verifica daca pozitiile ACESTUI BOT au fost inchise.
        Citeste PNL real din Binance doar pentru simbolurile pe care botul le-a deschis.
        """
        if not self.active_positions:
            return

        try:
            real_positions = self.client.futures_position_information()
            real_open = {
                p["symbol"] for p in real_positions
                if abs(float(p["positionAmt"])) > 0
            }
        except Exception as e:
            logger.error(f"_check_active_positions error: {e}")
            return

        to_close = []
        for symbol, pos in list(self.active_positions.items()):
            if symbol in real_open:
                continue  # pozitia inca e deschisa

            # Pozitia s-a inchis — citeste PNL real din Binance
            try:
                income = self.client.futures_income_history(
                    symbol     = symbol,
                    incomeType = "REALIZED_PNL",
                    startTime  = pos["open_ts"],
                    limit      = 10
                )
                pnl = sum(float(x["income"]) for x in income) if income else 0.0
                result = "TP" if pnl > 0 else "SL"
                close_time = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())

                sign = "+" if pnl >= 0 else ""
                logger.info(
                    f"[{symbol}] {'✅ TP' if result == 'TP' else '❌ SL'} | "
                    f"PNL real: {sign}{pnl:.4f} USDT"
                )

                # Salveaza in lista botului
                self.closed_trades.append({
                    "symbol":     symbol,
                    "direction":  pos["direction"],
                    "entry":      pos["entry"],
                    "sl":         pos["sl"],
                    "tp":         pos["tp"],
                    "result":     result,
                    "pnl":        round(pnl, 4),
                    "open_time":  pos["open_time"],
                    "close_time": close_time,
                    "rsi":        pos.get("rsi", 0),
                    "slope":      pos.get("slope", 0),
                })

                # Salveaza si in jurnal CSV
                try:
                    import journal
                    journal.log_trade(
                        symbol=symbol, direction=pos["direction"],
                        entry=pos["entry"], sl=pos["sl"], tp=pos["tp"],
                        result=result, pnl_usdt=pnl,
                        usdt_per_trade=USDT_PER_TRADE,
                        open_time=pos["open_time"], close_time=close_time,
                        rsi=pos.get("rsi", 0), ema_slope=pos.get("slope", 0),
                    )
                except Exception:
                    pass

                to_close.append(symbol)

            except Exception as e:
                logger.error(f"[{symbol}] get PNL error: {e}")

        for sym in to_close:
            self.active_positions.pop(sym, None)

    def _expire_old_orders(self):
        expiry_sec = config.ORDER_EXPIRY_HOURS * 3600
        now        = t.time()
        to_expire  = []
        for symbol, order_info in list(self.pending_orders.items()):
            if now - order_info.get("open_ts", now * 1000) / 1000 >= expiry_sec:
                logger.info(f"[{symbol}] Ordin expirat — anulez...")
                try:
                    self.client.futures_cancel_order(
                        symbol=symbol, orderId=order_info["order_id"]
                    )
                except Exception as e:
                    logger.error(f"[{symbol}] Cancel error: {e}")
                to_expire.append(symbol)
        for sym in to_expire:
            self.pending_orders.pop(sym, None)

    # ─────────────────────────────────────────
    #  STATISTICI DOAR PENTRU ACEST BOT
    # ─────────────────────────────────────────

    def get_bot_stats(self) -> dict:
        """Returneaza statistici EXCLUSIV pentru trade-urile acestui bot."""
        closed = [x for x in self.closed_trades if x["result"] in ("TP", "SL")]
        expired = [x for x in self.closed_trades if x["result"] == "EXPIRED"]

        if not closed:
            return {
                "total": 0, "wins": 0, "losses": 0, "expired": len(expired),
                "pnl_total": 0.0, "pnl_today": 0.0,
                "win_rate": 0.0, "best": 0.0, "worst": 0.0,
                "active": len(self.active_positions),
                "pending": len(self.pending_orders),
            }

        wins   = [x for x in closed if x["result"] == "TP"]
        losses = [x for x in closed if x["result"] == "SL"]
        pnls   = [x["pnl"] for x in closed]
        wr     = len(wins) / len(closed) * 100

        today_str = t.strftime("%Y-%m-%d", t.gmtime())
        pnl_today = sum(x["pnl"] for x in closed
                       if x.get("close_time", "")[:10] == today_str)

        return {
            "total":     len(closed),
            "wins":      len(wins),
            "losses":    len(losses),
            "expired":   len(expired),
            "pnl_total": round(sum(pnls), 4),
            "pnl_today": round(pnl_today, 4),
            "win_rate":  round(wr, 1),
            "best":      round(max(pnls), 4),
            "worst":     round(min(pnls), 4),
            "active":    len(self.active_positions),
            "pending":   len(self.pending_orders),
        }

    # ─────────────────────────────────────────
    #  LEVERAGE & TRADE PLACEMENT
    # ─────────────────────────────────────────

    def set_leverage(self, symbol: str):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except BinanceAPIException as e:
            logger.warning(f"[{symbol}] Leverage error: {e}")

    def place_fvg_trade(self, setup: FVGSetup) -> bool:
        symbol = setup.symbol
        try:
            info = self._get_symbol_info(symbol)
            tick = info["tick_size"]
            pp   = info["price_prec"]

            if setup.entry < tick or setup.sl < tick or setup.tp < tick:
                logger.warning(f"[{symbol}] SKIP — pret sub tick_size")
                return False

            entry_r = self._round_price(setup.entry, tick, pp)
            sl_r    = self._round_price(setup.sl,    tick, pp)
            tp_r    = self._round_price(setup.tp,    tick, pp)

            if entry_r <= 0 or sl_r <= 0 or tp_r <= 0:
                return False

            qty = self._calc_qty(entry_r, info)
            if qty <= 0:
                return False

            self.set_leverage(symbol)

            side       = "BUY"  if setup.direction == "BULL" else "SELL"
            close_side = "SELL" if setup.direction == "BULL" else "BUY"

            order = self.client.futures_create_order(
                symbol=symbol, side=side, type="LIMIT",
                timeInForce="GTC", quantity=qty, price=entry_r,
            )
            order_id    = order["orderId"]
            open_ts     = int(t.time() * 1000)
            open_time   = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())

            logger.info(
                f"[{symbol}] LIMIT {side} plasat | orderId={order_id} | "
                f"qty={qty} | entry={entry_r} | sl={sl_r} | tp={tp_r}"
            )

            self.pending_orders[symbol] = {
                "order_id":   order_id,
                "sl":         sl_r,
                "tp":         tp_r,
                "qty":        qty,
                "close_side": close_side,
                "entry":      entry_r,
                "direction":  side,
                "open_time":  open_time,
                "open_ts":    open_ts,
                "rsi":        getattr(setup, "rsi", 0.0),
                "slope":      getattr(setup, "slope_fast", 0.0),
            }
            return True

        except BinanceAPIException as e:
            if e.code == -2019:
                logger.warning(f"[{symbol}] SKIP — margin insuficient")
            else:
                logger.error(f"[{symbol}] BinanceAPIException: {e}")
            return False
        except Exception as e:
            logger.error(f"[{symbol}] Eroare: {e}")
            return False

    # ─────────────────────────────────────────
    #  UTILITARE COMPATIBILITATE
    # ─────────────────────────────────────────

    def get_open_positions(self) -> list:
        return [p["symbol"] for p in self.client.futures_position_information()
                if float(p["positionAmt"]) != 0]

    def get_open_orders(self) -> list:
        return list(set(o["symbol"] for o in self.client.futures_get_open_orders()))

    def count_active_trades(self) -> int:
        return len(self.pending_orders) + len(self.active_positions)

    def count_open_positions(self) -> int:
        return len(self.active_positions)

    def count_pending_orders(self) -> int:
        return len(self.pending_orders)

    def is_at_capacity(self) -> bool:
        return self.count_active_trades() >= config.MAX_OPEN_TRADES

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self.pending_orders or symbol in self.active_positions
