"""
Order Manager — Binance Futures
FIX DEFINITIV pentru eroarea -4120 (din 2025-12-09 Binance migrat ordine conditionale)

PROBLEME IDENTIFICATE SI REZOLVATE:
  1. POST body trimis ca params= (URL) in loc de data= (body) -> raspuns gol
  2. URL gresit /fapi/v1/order/algo -> corect /fapi/v1/algoOrder
  3. stopPrice in loc de triggerPrice pentru algo endpoint
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
        # {symbol: {order_id, sl, tp, qty, close_side}}
        self.pending_orders: dict = {}

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
        """
        POST semnat catre /fapi/v1/algoOrder
        FIX CRITIC: parametrii merg in request BODY (data=), nu in URL (params=)
        """
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
                data    = params,          # <- POST BODY (nu params=!)
                headers = {"X-MBX-APIKEY": config.API_KEY},
                timeout = 15
            )
            if not resp.text.strip():
                return {"error": f"Empty response HTTP {resp.status_code}"}
            return resp.json()
        except Exception as e:
            return {"error": str(e)}

    # ─────────────────────────────────────────
    #  PLASARE CONDITIONAL ORDER (SL / TP)
    # ─────────────────────────────────────────

    def _place_conditional(self, symbol: str, side: str,
                           order_type: str, trigger_price: float,
                           qty: float) -> bool:
        """
        Plaseaza SL sau TP pe noul endpoint algo (CONDITIONAL).
        order_type: "STOP_MARKET" sau "TAKE_PROFIT_MARKET"
        """
        label = "SL" if "STOP" in order_type else "TP"

        params = {
            "algoType":     "CONDITIONAL",
            "symbol":       symbol,
            "side":         side,
            "type":         order_type,
            "triggerPrice": str(trigger_price),   # FIX: triggerPrice nu stopPrice
            "quantity":     str(qty),
            "reduceOnly":   "true",
            "workingType":  "CONTRACT_PRICE",
        }

        data = self._algo_signed_post(params)

        if "algoId" in data or "orderId" in data:
            logger.info(f"[{symbol}] {label} plasat @ {trigger_price} | algoId={data.get('algoId','?')}")
            return True

        logger.error(f"[{symbol}] {label} ESUAT: {data}")

        if "code" in data:
            code = data.get("code")
            msg  = data.get("msg", "")
            logger.error(f"[{symbol}] Binance code={code}: {msg}")
            if code == -2022:
                logger.error(f"[{symbol}] -> Pozitia nu mai exista")
            elif code == -1102:
                logger.error(f"[{symbol}] -> Parametru gresit: {msg}")
            elif code == -4120:
                logger.error(f"[{symbol}] -> Inca pe endpoint gresit!")

        return False

    # ─────────────────────────────────────────
    #  VERIFICA ORDINE UMPLUTE -> SL + TP
    # ─────────────────────────────────────────

    def check_filled_orders(self):
        if not self.pending_orders:
            return

        done = []
        for symbol, data in list(self.pending_orders.items()):
            try:
                order = self.client.futures_get_order(
                    symbol=symbol, orderId=data["order_id"]
                )
                status = order.get("status", "")

                if status == "FILLED":
                    logger.info(f"[{symbol}] Ordin UMPLUT — plasez SL + TP...")
                    t.sleep(0.5)  # mic delay sa se confirme pozitia

                    sl_ok = self._place_conditional(
                        symbol, data["close_side"],
                        "STOP_MARKET",
                        data["sl"],
                        data["qty"]
                    )
                    t.sleep(0.3)
                    tp_ok = self._place_conditional(
                        symbol, data["close_side"],
                        "TAKE_PROFIT_MARKET",
                        data["tp"],
                        data["qty"]
                    )

                    if sl_ok and tp_ok:
                        logger.info(f"[{symbol}] SL + TP plasate cu succes!")
                    elif sl_ok:
                        logger.warning(f"[{symbol}] SL OK, dar TP a esuat!")
                    elif tp_ok:
                        logger.warning(f"[{symbol}] TP OK, dar SL a esuat — PERICOL!")
                    else:
                        logger.error(f"[{symbol}] AMBELE esuat — pozitie NEPROTEJATA!")

                    done.append(symbol)

                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.info(f"[{symbol}] Ordin {status} — eliminat din pending")
                    done.append(symbol)

            except Exception as e:
                logger.warning(f"[{symbol}] check_filled error: {e}")

        for sym in done:
            del self.pending_orders[sym]

    # ─────────────────────────────────────────
    #  LEVERAGE
    # ─────────────────────────────────────────

    def set_leverage(self, symbol: str):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
            logger.info(f"[{symbol}] Leverage {LEVERAGE}x setat")
        except BinanceAPIException as e:
            logger.warning(f"[{symbol}] Leverage error: {e}")

    # ─────────────────────────────────────────
    #  POZITII / ORDINE ACTIVE
    # ─────────────────────────────────────────

    def get_open_positions(self) -> list:
        return [p["symbol"] for p in self.client.futures_position_information()
                if float(p["positionAmt"]) != 0]

    def get_open_orders(self) -> list:
        return list(set(o["symbol"] for o in self.client.futures_get_open_orders()))

    def count_active_trades(self) -> int:
        """Numara DOAR ordinele/pozitiile deschise de BOT (nu manuale)."""
        return len(self.pending_orders)

    # ─────────────────────────────────────────
    #  PLASARE TRADE
    # ─────────────────────────────────────────

    def count_open_positions(self) -> int:
        """Numara pozitiile deschise de bot (pending + umplute estimate)."""
        return len(self.pending_orders)

    def count_pending_orders(self) -> int:
        return len(self.pending_orders)

    def is_at_capacity(self) -> bool:
        return len(self.pending_orders) >= config.MAX_OPEN_TRADES

    def has_symbol(self, symbol: str) -> bool:
        return (symbol in self.pending_orders or
                symbol in self.get_open_positions() or
                symbol in self.get_open_orders())

    def place_fvg_trade(self, setup: FVGSetup) -> bool:
        symbol = setup.symbol
        try:
            info = self._get_symbol_info(symbol)
            tick = info["tick_size"]
            pp   = info["price_prec"]

            if setup.entry < tick or setup.sl < tick or setup.tp < tick:
                logger.warning(f"[{symbol}] SKIP — pret sub tick_size ({tick})")
                return False

            entry_r = self._round_price(setup.entry, tick, pp)
            sl_r    = self._round_price(setup.sl,    tick, pp)
            tp_r    = self._round_price(setup.tp,    tick, pp)

            if entry_r <= 0 or sl_r <= 0 or tp_r <= 0:
                logger.warning(f"[{symbol}] SKIP — pret rotunjit invalid")
                return False

            qty = self._calc_qty(entry_r, info)
            if qty <= 0:
                logger.warning(f"[{symbol}] SKIP — cantitate invalida")
                return False

            self.set_leverage(symbol)

            side       = "BUY"  if setup.direction == "BULL" else "SELL"
            close_side = "SELL" if setup.direction == "BULL" else "BUY"

            order = self.client.futures_create_order(
                symbol=symbol, side=side, type="LIMIT",
                timeInForce="GTC", quantity=qty, price=entry_r,
            )
            order_id = order["orderId"]

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
            }
            logger.info(f"[{symbol}] Astept umplerea pentru SL/TP...")
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
