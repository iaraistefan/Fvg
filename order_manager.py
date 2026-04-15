"""
Order Manager v6 — fixes:
  1. State persistent in JSON (supravietuieste restart-urilor Render)
  2. Expire orders calcul corect
  3. income_history cu endTime pentru izolare corecta
"""
import logging
import hmac
import hashlib
import time as t
import json
import os
import requests as req
from urllib.parse import urlencode
from binance.client import Client
from binance.exceptions import BinanceAPIException
from detector import FVGSetup
import config
from config import LEVERAGE, USDT_PER_TRADE

logger = logging.getLogger("FVGBot")
FAPI  = "https://fapi.binance.com"

# Fisier de stare — supravietuieste restarturilor
STATE_FILE = "bot_state.json"


def _save_state(pending: dict, active: dict, closed: list):
    """Salveaza starea botului pe disk."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "pending_orders":  pending,
                "active_positions": active,
                "closed_trades":   closed,
            }, f, indent=2)
    except Exception as e:
        logger.error(f"_save_state error: {e}")


def _load_state():
    """Incarca starea botului de pe disk (dupa restart)."""
    if not os.path.exists(STATE_FILE):
        return {}, {}, []
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        pending = data.get("pending_orders", {})
        active  = data.get("active_positions", {})
        closed  = data.get("closed_trades", [])
        if pending or active:
            logger.info(
                f"State restaurat: {len(pending)} pending, "
                f"{len(active)} active, {len(closed)} closed"
            )
        return pending, active, closed
    except Exception as e:
        logger.error(f"_load_state error: {e}")
        return {}, {}, []


class OrderManager:
    def __init__(self, client: Client):
        self.client = client
        self._precision_cache = {}

        # Incarca starea de pe disk (dupa restart)
        self.pending_orders, self.active_positions, self.closed_trades = _load_state()

    def _save(self):
        _save_state(self.pending_orders, self.active_positions, self.closed_trades)

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
        """POST la /fapi/v1/algoOrder — parametrii in body (data=)."""
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
    #  SL / TP
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
    #  CHECK CYCLE
    # ─────────────────────────────────────────

    def check_filled_orders(self):
        changed = False
        changed |= self._check_pending()
        changed |= self._check_active_positions()
        changed |= self._expire_old_orders()
        if changed:
            self._save()  # salveaza pe disk doar daca ceva s-a schimbat

    def _check_pending(self) -> bool:
        if not self.pending_orders:
            return False

        to_remove = []
        changed   = False

        for symbol, data in list(self.pending_orders.items()):
            try:
                order  = self.client.futures_get_order(
                    symbol=symbol, orderId=data["order_id"]
                )
                status = order.get("status", "")

                if status == "FILLED":
                    filled_price = float(order.get("avgPrice", data["entry"]))
                    logger.info(f"[{symbol}] Ordin UMPLUT la {filled_price}")
                    t.sleep(0.5)

                    close_side = data["close_side"]
                    sl_ok = self._place_conditional(
                        symbol, close_side, "STOP_MARKET", data["sl"], data["qty"]
                    )
                    t.sleep(0.3)
                    tp_ok = self._place_conditional(
                        symbol, close_side, "TAKE_PROFIT_MARKET", data["tp"], data["qty"]
                    )

                    if sl_ok and tp_ok:
                        logger.info(f"[{symbol}] SL + TP plasate cu succes!")
                    elif not sl_ok:
                        logger.warning(f"[{symbol}] SL a esuat — PERICOL!")
                    elif not tp_ok:
                        logger.warning(f"[{symbol}] TP a esuat!")

                    # Muta in active_positions
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
                    changed = True

                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.info(f"[{symbol}] Ordin {status}")
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
                    changed = True

            except Exception as e:
                logger.error(f"[{symbol}] check_pending error: {e}")

        for sym in to_remove:
            self.pending_orders.pop(sym, None)

        return changed

    def _check_active_positions(self) -> bool:
        """Verifica pozitiile active ale botului — izolat de alte boturi."""
        if not self.active_positions:
            return False

        try:
            real_positions = self.client.futures_position_information()
            real_open = {
                p["symbol"] for p in real_positions
                if abs(float(p["positionAmt"])) > 0
            }
        except Exception as e:
            logger.error(f"_check_active_positions error: {e}")
            return False

        to_close = []
        changed  = False

        for symbol, pos in list(self.active_positions.items()):
            if symbol in real_open:
                continue  # pozitia inca e deschisa

            # Pozitia s-a inchis — citeste PNL din Binance
            try:
                open_ts  = int(pos["open_ts"])
                # FIX BUG 3: endTime = acum, pentru a nu lua income din alte sesiuni
                end_ts   = int(t.time() * 1000)

                income = self.client.futures_income_history(
                    symbol     = symbol,
                    incomeType = "REALIZED_PNL",
                    startTime  = open_ts,
                    endTime    = end_ts,
                    limit      = 20
                )
                pnl = sum(float(x["income"]) for x in income) if income else 0.0

                # Daca PNL = 0 si pozitia nu mai e in cont, probabil e o eroare temporara
                # Asteptam urmatorul ciclu
                if pnl == 0.0 and not income:
                    logger.warning(f"[{symbol}] Pozitia inchisa dar PNL = 0, verific din nou...")
                    continue

                result     = "TP" if pnl > 0 else "SL"
                close_time = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())
                sign       = "+" if pnl >= 0 else ""

                logger.info(
                    f"[{symbol}] {'✅ TP' if result == 'TP' else '❌ SL'} | "
                    f"PNL real: {sign}{pnl:.4f} USDT"
                )

                trade_record = {
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
                }
                self.closed_trades.append(trade_record)

                # Salveaza in jurnal CSV
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
                changed = True

            except Exception as e:
                logger.error(f"[{symbol}] get PNL error: {e}")

        for sym in to_close:
            self.active_positions.pop(sym, None)

        return changed

    def _expire_old_orders(self) -> bool:
        """FIX BUG 2: calcul corect al timpului de expirare."""
        expiry_ms = config.ORDER_EXPIRY_HOURS * 3600 * 1000  # in milisecunde
        now_ms    = int(t.time() * 1000)
        to_expire = []
        changed   = False

        for symbol, order_info in list(self.pending_orders.items()):
            open_ts = order_info.get("open_ts", now_ms)
            age_ms  = now_ms - open_ts  # ambele in milisecunde acum

            if age_ms >= expiry_ms:
                age_h = age_ms / 3600000
                logger.info(f"[{symbol}] Ordin expirat dupa {age_h:.1f}h — anulez...")
                try:
                    self.client.futures_cancel_order(
                        symbol=symbol, orderId=order_info["order_id"]
                    )
                    self.closed_trades.append({
                        "symbol":     symbol,
                        "direction":  order_info.get("direction", "?"),
                        "entry":      order_info.get("entry", 0),
                        "sl":         order_info["sl"],
                        "tp":         order_info["tp"],
                        "result":     "EXPIRED",
                        "pnl":        0.0,
                        "open_time":  order_info.get("open_time", ""),
                        "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    })
                    changed = True
                except Exception as e:
                    logger.error(f"[{symbol}] Cancel error: {e}")
                to_expire.append(symbol)

        for sym in to_expire:
            self.pending_orders.pop(sym, None)

        return changed

    # ─────────────────────────────────────────
    #  STATISTICI DOAR PENTRU ACEST BOT
    # ─────────────────────────────────────────

    def get_bot_stats(self) -> dict:
        closed  = [x for x in self.closed_trades if x["result"] in ("TP", "SL")]
        expired = [x for x in self.closed_trades if x["result"] == "EXPIRED"]

        if not closed:
            return {
                "total": 0, "wins": 0, "losses": 0, "expired": len(expired),
                "pnl_total": 0.0, "pnl_today": 0.0,
                "win_rate": 0.0, "best": 0.0, "worst": 0.0,
                "active":  len(self.active_positions),
                "pending": len(self.pending_orders),
            }

        wins   = [x for x in closed if x["result"] == "TP"]
        losses = [x for x in closed if x["result"] == "SL"]
        pnls   = [x["pnl"] for x in closed]
        wr     = len(wins) / len(closed) * 100

        today_str = t.strftime("%Y-%m-%d", t.gmtime())
        pnl_today = sum(
            x["pnl"] for x in closed
            if x.get("close_time", "")[:10] == today_str
        )

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
    #  PLASARE TRADE
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
            open_ts    = int(t.time() * 1000)
            open_time  = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())

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
                "entry":      entry_r,
                "direction":  side,
                "open_time":  open_time,
                "open_ts":    open_ts,
                "rsi":        getattr(setup, "rsi", 0.0),
                "slope":      getattr(setup, "slope_fast", 0.0),
            }
            self._save()  # salveaza imediat
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
    #  UTILITARE
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
