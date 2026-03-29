"""
Order Manager v6 — fixes:
  1. State persistent in JSON (supravietuieste restart-urilor Render)
  2. Expire orders calcul corect
  3. income_history cu endTime pentru izolare corecta
"""
import logging, hmac, hashlib, json, os
import time as t
import requests as req
from urllib.parse import urlencode
from binance.client import Client
from binance.exceptions import BinanceAPIException
from detector import FVGSetup
import config
from config import LEVERAGE, USDT_PER_TRADE

logger = logging.getLogger("FVGBot")
FAPI       = "https://fapi.binance.com"
STATE_FILE = "bot_state.json"


def _save_state(pending, active, closed):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"pending_orders": pending,
                       "active_positions": active,
                       "closed_trades": closed}, f, indent=2)
    except Exception as e:
        logger.error(f"_save_state error: {e}")


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}, {}, []
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        p = data.get("pending_orders", {})
        a = data.get("active_positions", {})
        c = data.get("closed_trades", [])
        if p or a:
            logger.info(f"State restaurat: {len(p)} pending, {len(a)} active, {len(c)} closed")
        return p, a, c
    except Exception as e:
        logger.error(f"_load_state error: {e}")
        return {}, {}, []


class OrderManager:
    def __init__(self, client: Client):
        self.client = client
        self._precision_cache = {}
        self.pending_orders, self.active_positions, self.closed_trades = _load_state()

    def _save(self):
        _save_state(self.pending_orders, self.active_positions, self.closed_trades)

    def reconcile_with_binance(self):
        """
        La startup: importa pozitiile si ordinele deschise din Binance.
        Necesar deoarece Render sterge filesystem-ul la fiecare deploy.
        """
        try:
            # Pozitii deschise
            positions = self.client.futures_position_information()
            open_pos  = [p for p in positions if abs(float(p["positionAmt"])) > 0]

            for p in open_pos:
                symbol = p["symbol"]
                if symbol in self.active_positions:
                    continue  # deja stim de ea

                amt   = float(p["positionAmt"])
                entry = float(p["entryPrice"])
                direction = "BUY" if amt > 0 else "SELL"

                # Incearca sa gaseasca SL/TP din ordinele conditionale existente
                sl_price = tp_price = 0.0
                try:
                    algo_orders = self.client.futures_get_open_orders(symbol=symbol)
                    for o in algo_orders:
                        sp = float(o.get("stopPrice", 0))
                        ot = o.get("type", "")
                        if "STOP" in ot and sp > 0:
                            sl_price = sp
                        elif "PROFIT" in ot and sp > 0:
                            tp_price = sp
                except Exception:
                    pass

                self.active_positions[symbol] = {
                    "direction": direction,
                    "entry":     entry,
                    "sl":        sl_price,
                    "tp":        tp_price,
                    "qty":       abs(amt),
                    "open_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    "open_ts":   int(t.time() * 1000) - 3600000,  # assume deschis acum 1h
                    "rsi":       0.0,
                    "slope":     0.0,
                }
                logger.info(f"[RECONCILE] Importat pozitie: {symbol} {direction} @ {entry} (SL={sl_price} TP={tp_price})")

            # Ordine LIMIT pending
            open_orders = self.client.futures_get_open_orders()
            for o in open_orders:
                symbol = o["symbol"]
                if o.get("type") != "LIMIT":
                    continue
                if symbol in self.pending_orders:
                    continue

                side = o["side"]
                close_side = "SELL" if side == "BUY" else "BUY"
                price = float(o["price"])
                qty   = float(o["origQty"])

                self.pending_orders[symbol] = {
                    "order_id":   o["orderId"],
                    "sl":         0.0,
                    "tp":         0.0,
                    "qty":        qty,
                    "close_side": close_side,
                    "entry":      price,
                    "direction":  side,
                    "open_time":  t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    "open_ts":    int(t.time() * 1000),
                    "rsi":        0.0,
                    "slope":      0.0,
                }
                logger.info(f"[RECONCILE] Importat ordin pending: {symbol} {side} LIMIT @ {price}")

            if open_pos or open_orders:
                self._save()
                logger.info(f"[RECONCILE] {len(self.active_positions)} pozitii, {len(self.pending_orders)} pending importate din Binance")
            else:
                logger.info("[RECONCILE] Nicio pozitie sau ordin deschis in Binance")

        except Exception as e:
            logger.error(f"reconcile_with_binance error: {e}")

    def _get_symbol_info(self, symbol):
        if symbol not in self._precision_cache:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                if s["symbol"] == symbol:
                    tick = float(next(f["tickSize"] for f in s["filters"] if f["filterType"] == "PRICE_FILTER"))
                    self._precision_cache[symbol] = {
                        "price_prec": int(s["pricePrecision"]),
                        "qty_prec":   int(s["quantityPrecision"]),
                        "tick_size":  tick,
                    }
                    break
        return self._precision_cache.get(symbol, {})

    def _round_price(self, price, tick, decimals):
        return round(round(price / tick) * tick, decimals)

    def _calc_qty(self, entry, info):
        return round((USDT_PER_TRADE * LEVERAGE) / entry, info["qty_prec"])

    def _algo_signed_post(self, params):
        params["timestamp"] = int(t.time() * 1000)
        qs  = urlencode(params)
        sig = hmac.new(config.API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        try:
            resp = req.post(FAPI + "/fapi/v1/algoOrder", data=params,
                           headers={"X-MBX-APIKEY": config.API_KEY}, timeout=15)
            return resp.json() if resp.text.strip() else {"error": f"Empty {resp.status_code}"}
        except Exception as e:
            return {"error": str(e)}

    def _place_conditional(self, symbol, side, order_type, trigger_price, qty):
        label = "SL" if "STOP" in order_type else "TP"
        data  = self._algo_signed_post({
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
            "type": order_type, "triggerPrice": str(trigger_price),
            "quantity": str(qty), "reduceOnly": "true", "workingType": "CONTRACT_PRICE",
        })
        if "algoId" in data or "orderId" in data:
            logger.info(f"[{symbol}] {label} plasat @ {trigger_price} | algoId={data.get('algoId','?')}")
            return True
        logger.error(f"[{symbol}] {label} ESUAT: {data}")
        return False

    def check_filled_orders(self):
        c1 = self._check_pending()
        c2 = self._check_active_positions()
        c3 = self._expire_old_orders()
        if c1 or c2 or c3:
            self._save()

    def _check_pending(self):
        if not self.pending_orders:
            return False
        to_remove = []
        changed   = False
        for symbol, data in list(self.pending_orders.items()):
            try:
                order  = self.client.futures_get_order(symbol=symbol, orderId=data["order_id"])
                status = order.get("status", "")
                if status == "FILLED":
                    filled = float(order.get("avgPrice", data["entry"]))
                    logger.info(f"[{symbol}] Ordin UMPLUT la {filled} — plasez SL + TP...")
                    t.sleep(0.5)
                    cs    = data["close_side"]
                    sl_ok = self._place_conditional(symbol, cs, "STOP_MARKET",        data["sl"], data["qty"])
                    t.sleep(0.3)
                    tp_ok = self._place_conditional(symbol, cs, "TAKE_PROFIT_MARKET", data["tp"], data["qty"])
                    if sl_ok and tp_ok:
                        logger.info(f"[{symbol}] SL + TP plasate cu succes!")
                    elif not sl_ok:
                        logger.warning(f"[{symbol}] SL a esuat — PERICOL!")
                    self.active_positions[symbol] = {
                        "direction": data.get("direction","?"), "entry": filled,
                        "sl": data["sl"], "tp": data["tp"], "qty": data["qty"],
                        "open_time": data.get("open_time",""),
                        "open_ts":   data.get("open_ts", int(t.time()*1000)),
                        "rsi":   data.get("rsi",0.0), "slope": data.get("slope",0.0),
                    }
                    to_remove.append(symbol)
                    changed = True
                elif status in ("CANCELED","EXPIRED","REJECTED"):
                    logger.info(f"[{symbol}] Ordin {status}")
                    self.closed_trades.append({
                        "symbol": symbol, "direction": data.get("direction","?"),
                        "entry": data.get("entry",0), "sl": data["sl"], "tp": data["tp"],
                        "result": "EXPIRED", "pnl": 0.0,
                        "open_time": data.get("open_time",""),
                        "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    })
                    to_remove.append(symbol)
                    changed = True
            except Exception as e:
                logger.error(f"[{symbol}] check_pending error: {e}")
        for sym in to_remove:
            self.pending_orders.pop(sym, None)
        return changed

    def _check_active_positions(self):
        if not self.active_positions:
            return False
        try:
            real_open = {p["symbol"] for p in self.client.futures_position_information()
                        if abs(float(p["positionAmt"])) > 0}
        except Exception as e:
            logger.error(f"_check_active error: {e}")
            return False
        to_close = []
        changed  = False
        for symbol, pos in list(self.active_positions.items()):
            if symbol in real_open:
                continue
            try:
                open_ts = int(pos["open_ts"])
                end_ts  = int(t.time() * 1000)
                income  = self.client.futures_income_history(
                    symbol=symbol, incomeType="REALIZED_PNL",
                    startTime=open_ts, endTime=end_ts, limit=20
                )
                pnl = sum(float(x["income"]) for x in income) if income else 0.0
                if pnl == 0.0 and not income:
                    logger.warning(f"[{symbol}] PNL=0, verific din nou la urmatorul ciclu...")
                    continue
                result = "TP" if pnl > 0 else "SL"
                close_time = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())
                sign = "+" if pnl >= 0 else ""
                logger.info(f"[{symbol}] {'✅ TP' if result=='TP' else '❌ SL'} | PNL real: {sign}{pnl:.4f} USDT")
                self.closed_trades.append({
                    "symbol": symbol, "direction": pos["direction"],
                    "entry": pos["entry"], "sl": pos["sl"], "tp": pos["tp"],
                    "result": result, "pnl": round(pnl,4),
                    "open_time": pos["open_time"], "close_time": close_time,
                    "rsi": pos.get("rsi",0), "slope": pos.get("slope",0),
                })
                try:
                    import journal
                    journal.log_trade(
                        symbol=symbol, direction=pos["direction"],
                        entry=pos["entry"], sl=pos["sl"], tp=pos["tp"],
                        result=result, pnl_usdt=pnl, usdt_per_trade=USDT_PER_TRADE,
                        open_time=pos["open_time"], close_time=close_time,
                        rsi=pos.get("rsi",0), ema_slope=pos.get("slope",0),
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

    def _expire_old_orders(self):
        # FIX: ambele valori in milisecunde
        expiry_ms = config.ORDER_EXPIRY_HOURS * 3600 * 1000
        now_ms    = int(t.time() * 1000)
        to_expire = []
        changed   = False
        for symbol, oi in list(self.pending_orders.items()):
            open_ts = oi.get("open_ts", now_ms)
            if now_ms - open_ts >= expiry_ms:
                age_h = (now_ms - open_ts) / 3600000
                logger.info(f"[{symbol}] Ordin expirat dupa {age_h:.1f}h — anulez...")
                try:
                    self.client.futures_cancel_order(symbol=symbol, orderId=oi["order_id"])
                    self.closed_trades.append({
                        "symbol": symbol, "direction": oi.get("direction","?"),
                        "entry": oi.get("entry",0), "sl": oi["sl"], "tp": oi["tp"],
                        "result": "EXPIRED", "pnl": 0.0,
                        "open_time": oi.get("open_time",""),
                        "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    })
                    changed = True
                except Exception as e:
                    logger.error(f"[{symbol}] Cancel error: {e}")
                to_expire.append(symbol)
        for sym in to_expire:
            self.pending_orders.pop(sym, None)
        return changed

    def get_bot_stats(self):
        closed  = [x for x in self.closed_trades if x["result"] in ("TP","SL")]
        expired = [x for x in self.closed_trades if x["result"] == "EXPIRED"]
        if not closed:
            return {"total":0,"wins":0,"losses":0,"expired":len(expired),
                    "pnl_total":0.0,"pnl_today":0.0,"win_rate":0.0,
                    "best":0.0,"worst":0.0,
                    "active":len(self.active_positions),"pending":len(self.pending_orders)}
        wins  = [x for x in closed if x["result"]=="TP"]
        losses= [x for x in closed if x["result"]=="SL"]
        pnls  = [x["pnl"] for x in closed]
        today = t.strftime("%Y-%m-%d", t.gmtime())
        return {
            "total":     len(closed),
            "wins":      len(wins),
            "losses":    len(losses),
            "expired":   len(expired),
            "pnl_total": round(sum(pnls),4),
            "pnl_today": round(sum(x["pnl"] for x in closed if x.get("close_time","")[:10]==today),4),
            "win_rate":  round(len(wins)/len(closed)*100,1),
            "best":      round(max(pnls),4),
            "worst":     round(min(pnls),4),
            "active":    len(self.active_positions),
            "pending":   len(self.pending_orders),
        }

    def set_leverage(self, symbol):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except BinanceAPIException as e:
            logger.warning(f"[{symbol}] Leverage error: {e}")

    def place_fvg_trade(self, setup: FVGSetup) -> bool:
        symbol = setup.symbol
        try:
            info    = self._get_symbol_info(symbol)
            tick    = info["tick_size"]
            pp      = info["price_prec"]
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
            order    = self.client.futures_create_order(
                symbol=symbol, side=side, type="LIMIT",
                timeInForce="GTC", quantity=qty, price=entry_r,
            )
            order_id = order["orderId"]
            logger.info(f"[{symbol}] LIMIT {side} | orderId={order_id} | qty={qty} | entry={entry_r} | sl={sl_r} | tp={tp_r}")
            self.pending_orders[symbol] = {
                "order_id": order_id, "sl": sl_r, "tp": tp_r, "qty": qty,
                "close_side": close_side, "entry": entry_r, "direction": side,
                "open_time": open_time, "open_ts": open_ts,
                "rsi": getattr(setup,"rsi",0.0), "slope": getattr(setup,"slope_fast",0.0),
            }
            self._save()
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

    def get_open_positions(self):
        return [p["symbol"] for p in self.client.futures_position_information() if float(p["positionAmt"]) != 0]

    def get_open_orders(self):
        return list(set(o["symbol"] for o in self.client.futures_get_open_orders()))

    def count_active_trades(self):
        return len(self.pending_orders) + len(self.active_positions)

    def count_open_positions(self):
        return len(self.active_positions)

    def count_pending_orders(self):
        return len(self.pending_orders)

    def is_at_capacity(self):
        return self.count_active_trades() >= config.MAX_OPEN_TRADES

    def has_symbol(self, symbol):
        return symbol in self.pending_orders or symbol in self.active_positions
