"""
Order Manager 4H — v7
Fix-uri aplicate:
  1. open_ts = acum - 24h (nu -1h) → income_history gaseste PNL corect
  2. SL/TP replasate dupa reconciliere (pozitii cu sl=0 sau tp=0)
  3. Handler -1003 in _check_pending
  4. MARK_PRICE + quantity + reduceOnly=True + GTC pentru SL/TP
  5. Micro-pauze adaugate in bucle pentru compatibilitate Multi-Bot (evitare Rate Limit -1003 pe Render)
"""
import logging, json, os
import time as t

from binance.client import Client
from binance.exceptions import BinanceAPIException
from detector import FVGSetup
import config
from config import LEVERAGE, USDT_PER_TRADE

logger = logging.getLogger("FVGBot")


def _save_state(pending, active, closed):
    try:
        sf = getattr(config, "STATE_FILE", "bot_state_4h.json")
        with open(sf, "w", encoding="utf-8") as f:
            json.dump({"pending_orders": pending,
                       "active_positions": active,
                       "closed_trades": closed}, f, indent=2)
    except Exception as e:
        logger.error(f"_save_state error: {e}")


def _load_state():
    try:
        sf = getattr(config, "STATE_FILE", "bot_state_4h.json")
        if not os.path.exists(sf):
            return {}, {}, []
        with open(sf, encoding="utf-8") as f:
            data = json.load(f)
        p = data.get("pending_orders", {})
        a = data.get("active_positions", {})
        c = data.get("closed_trades", [])
        if p or a:
            logger.info(f"[STATE] Restaurat: {len(p)} pending, {len(a)} active, {len(c)} closed")
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

    # ─────────────────────────────────────────────
    #  RECONCILIERE LA STARTUP
    # ─────────────────────────────────────────────

    def reconcile_with_binance(self):
        """
        La startup: importa pozitiile deschise din Binance.
        FIX: open_ts = acum - 24h (nu -1h) ca income_history
             sa gaseasca PNL-ul indiferent cat de veche e pozitia.
        FIX: Dupa import, replaseaza SL/TP pentru pozitiile cu sl=0/tp=0.
        """
        try:
            positions = self.client.futures_position_information()
            open_pos  = [p for p in positions if abs(float(p["positionAmt"])) > 0]

            for p in open_pos:
                symbol = p["symbol"]
                if symbol in self.active_positions:
                    continue

                amt       = float(p["positionAmt"])
                entry     = float(p["entryPrice"])
                direction = "BUY" if amt > 0 else "SELL"
                sl_p = tp_p = 0.0

                try:
                    orders = self.client.futures_get_open_orders(symbol=symbol)
                    for o in orders:
                        sp = float(o.get("stopPrice", 0))
                        ot = o.get("type", "")
                        if "STOP" in ot and sp > 0:
                            sl_p = sp
                        elif "PROFIT" in ot and sp > 0:
                            tp_p = sp
                except Exception:
                    pass
                
                # PROTECȚIE RATE LIMIT
                t.sleep(0.2) 

                # FIX: open_ts = acum - 24h (nu -1h!)
                self.active_positions[symbol] = {
                    "direction": direction,
                    "entry":     entry,
                    "sl":        sl_p,
                    "tp":        tp_p,
                    "qty":       abs(amt),
                    "open_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    "open_ts":   int(t.time() * 1000) - 86400000,  # FIX: -24h
                    "rsi":       0.0,
                    "slope":     0.0,
                }
                logger.info(f"[RECONCILE] Importat: {symbol} {direction} @ {entry} (SL={sl_p} TP={tp_p})")

            # Ordine LIMIT pending
            open_orders = self.client.futures_get_open_orders()
            for o in open_orders:
                symbol = o["symbol"]
                if o.get("type") != "LIMIT":
                    continue
                if symbol in self.pending_orders:
                    continue
                side = o["side"]
                self.pending_orders[symbol] = {
                    "order_id":   o["orderId"],
                    "sl":         0.0, "tp": 0.0,
                    "qty":        float(o["origQty"]),
                    "close_side": "SELL" if side == "BUY" else "BUY",
                    "entry":      float(o["price"]),
                    "direction":  side,
                    "open_time":  t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    "open_ts":    int(t.time() * 1000),
                    "rsi":        0.0, "slope": 0.0,
                }
                logger.info(f"[RECONCILE] Pending: {symbol} {side} LIMIT @ {o['price']}")

            if open_pos or open_orders:
                self._save()
                logger.info(f"[RECONCILE] {len(self.active_positions)} pozitii, {len(self.pending_orders)} pending")

                # FIX: Replaseaza SL/TP pentru pozitiile fara protectie
                self._fix_missing_sl_tp()
            else:
                logger.info("[RECONCILE] Nicio pozitie deschisa")

        except Exception as e:
            if "-1003" in str(e):
                logger.warning(f"reconcile: rate limit — astept 60s...")
                t.sleep(60)
            else:
                logger.error(f"reconcile_with_binance error: {e}")

    def _fix_missing_sl_tp(self):
        """
        FIX CRITIC: Dupa reconciliere, pozitiile cu SL=0 sau TP=0
        nu au protectie. Calculeaza si plaseaza SL/TP bazat pe entry.
        Foloseste 1:1 RR si SL la 1.5% din entry (aproximare).
        """
        for symbol, pos in list(self.active_positions.items()):
            if pos["sl"] > 0 and pos["tp"] > 0:
                continue  # deja protejata

            entry     = pos["entry"]
            direction = pos["direction"]
            qty       = pos["qty"]

            if entry <= 0:
                continue

            # Calculeaza SL/TP aproximativ (1.5% risk, 1:1 RR)
            risk_pct = 0.015
            if direction == "BUY":
                sl = entry * (1 - risk_pct)
                tp = entry * (1 + risk_pct)
                cs = "SELL"
            else:
                sl = entry * (1 + risk_pct)
                tp = entry * (1 - risk_pct)
                cs = "BUY"

            # Rotunjire
            try:
                info = self._get_symbol_info(symbol)
                tick = info.get("tick_size", 0.0001)
                pp   = info.get("price_prec", 4)
                sl = self._round_price(sl, tick, pp)
                tp = self._round_price(tp, tick, pp)
            except Exception:
                pass

            logger.warning(f"[RECONCILE-FIX] {symbol} SL/TP lipsa — plasez aproximativ @ SL={sl} TP={tp}")

            sl_ok = self._place_sl_tp(symbol, cs, "STOP_MARKET", sl, qty)
            t.sleep(0.3)
            tp_ok = self._place_sl_tp(symbol, cs, "TAKE_PROFIT_MARKET", tp, qty)

            if sl_ok:
                self.active_positions[symbol]["sl"] = sl
            if tp_ok:
                self.active_positions[symbol]["tp"] = tp

            if sl_ok and tp_ok:
                logger.info(f"[RECONCILE-FIX] {symbol} SL+TP plasate ✅")
            else:
                logger.warning(f"[RECONCILE-FIX] {symbol} SL/TP partial esuat — Guardian protejeaza")
                
            # PROTECȚIE RATE LIMIT
            t.sleep(0.2)

        self._save()

    # ─────────────────────────────────────────────
    #  UTILS
    # ─────────────────────────────────────────────

    def _get_symbol_info(self, symbol):
        if symbol not in self._precision_cache:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                if s["symbol"] == symbol:
                    tick = float(next(f["tickSize"] for f in s["filters"]
                                     if f["filterType"] == "PRICE_FILTER"))
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

    # ─────────────────────────────────────────────
    #  SL / TP
    # ─────────────────────────────────────────────

    def _place_sl_tp(self, symbol, side, order_type, trigger_price, qty) -> bool:
        """
        MARK_PRICE: trigger pe Mark Price = acelasi ca ROI afisat in app
        quantity+reduceOnly=True: standard endpoint (fara -4120)
        GTC: evita eroarea -4129 (GTE_GTC e legacy)
        """
        label = "SL" if "STOP" in order_type else "TP"
        try:
            order = self.client.futures_create_order(
                symbol        = symbol,
                side          = side,
                type          = order_type,
                stopPrice     = str(trigger_price),
                quantity    = str(qty),
                reduceOnly  = True,
                workingType = "MARK_PRICE",
                timeInForce = "GTC",
            )
            logger.info(f"[{symbol}] {label} @ {trigger_price} | id={order.get('orderId','?')} | MARK_PRICE")
            return True
        except BinanceAPIException as e:
            if e.code == -2021:
                # Pret deja trecut — inchide MARKET
                logger.warning(f"[{symbol}] {label} -2021 — inchid MARKET!")
                try:
                    self.client.futures_create_order(
                        symbol=symbol, side=side,
                        type="MARKET", quantity=qty, reduceOnly=True
                    )
                    logger.info(f"[{symbol}] Inchis MARKET dupa -2021")
                    return True
                except Exception as ce:
                    logger.error(f"[{symbol}] Market close error: {ce}")
                    return False
            elif e.code == -1111:
                # Precizie — retry cu rotunjire
                try:
                    info = self._get_symbol_info(symbol)
                    tp_r = self._round_price(trigger_price,
                                             info.get("tick_size", 0.0001),
                                             info.get("price_prec", 4))
                    self.client.futures_create_order(
                        symbol=symbol, side=side, type=order_type,
                        stopPrice=str(tp_r), quantity=str(qty),
                        reduceOnly=True, workingType="MARK_PRICE", timeInForce="GTC",
                    )
                    logger.info(f"[{symbol}] {label} retry @ {tp_r} ✅")
                    return True
                except Exception as re:
                    logger.error(f"[{symbol}] {label} retry esuat: {re}")
                    return False
            elif e.code == -4120:
                logger.warning(f"[{symbol}] {label} -4120 — retry fara MARK_PRICE...")
                try:
                    order = self.client.futures_create_order(
                        symbol=symbol, side=side, type=order_type,
                        stopPrice=str(trigger_price), quantity=str(qty),
                        reduceOnly=True, timeInForce="GTC",
                    )
                    logger.info(f"[{symbol}] {label} plasat (CONTRACT_PRICE fallback) ✅")
                    return True
                except Exception as fe:
                    logger.error(f"[{symbol}] {label} fallback esuat: {fe}")
                    return False
            else:
                logger.error(f"[{symbol}] {label} error {e.code}: {e.message}")
                return False
        except Exception as e:
            logger.error(f"[{symbol}] {label} error: {e}")
            return False

    # ─────────────────────────────────────────────
    #  CHECK CYCLE
    # ─────────────────────────────────────────────

    def check_filled_orders(self):
        c1 = self._check_pending()
        c2 = self._check_active_positions()
        c3 = self._expire_old_orders()
        if c1 or c2 or c3:
            self._save()

    def _check_pending(self) -> bool:
        """
        FIX RATE LIMIT: Un singur futures_get_open_orders() in loc de
        cate un futures_get_order() per simbol. Reduce N calls → 1 call.
        """
        if not self.pending_orders:
            return False
        try:
            # UN singur call pentru TOATE ordinele deschise
            open_orders = self.client.futures_get_open_orders()
            open_ids    = {str(o["orderId"]) for o in open_orders}
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("_check_pending: rate limit — skip")
                return False
            logger.error(f"_check_pending batch error: {e}")
            return False
        except Exception as e:
            logger.error(f"_check_pending batch error: {e}")
            return False

        to_remove = []; changed = False
        for symbol, data in list(self.pending_orders.items()):
            order_id = str(data["order_id"])
            if order_id in open_ids:
                continue  # inca deschis, nu s-a umplut

            # Ordinul nu mai e deschis — verifica daca e FILLED sau CANCELED
            try:
                order  = self.client.futures_get_order(symbol=symbol, orderId=data["order_id"])
                status = order.get("status", "")
                
                # PROTECȚIE RATE LIMIT
                t.sleep(0.2)
                
            except BinanceAPIException as e:
                if e.code == -1003:
                    logger.warning(f"[{symbol}] check status rate limit — skip")
                    break
                logger.error(f"[{symbol}] get_order error: {e}")
                continue
            except Exception as e:
                logger.error(f"[{symbol}] get_order error: {e}")
                continue

            if status == "FILLED":
                filled = float(order.get("avgPrice", data["entry"]))
                logger.info(f"[{symbol}] UMPLUT la {filled} — plasez SL+TP...")
                t.sleep(0.5)
                cs    = data["close_side"]
                sl_ok = self._place_sl_tp(symbol, cs, "STOP_MARKET",        data["sl"], data["qty"])
                t.sleep(0.3)
                tp_ok = self._place_sl_tp(symbol, cs, "TAKE_PROFIT_MARKET", data["tp"], data["qty"])
                if sl_ok and tp_ok:
                    logger.info(f"[{symbol}] SL+TP plasate ✅")
                elif not sl_ok:
                    logger.warning(f"[{symbol}] SL ESUAT — PERICOL!")

                self.active_positions[symbol] = {
                    "direction": data.get("direction", "?"),
                    "entry":     filled,
                    "sl":        data["sl"],
                    "tp":        data["tp"],
                    "qty":       data["qty"],
                    "open_time": data.get("open_time", ""),
                    "open_ts":   data.get("open_ts", int(t.time() * 1000)),
                    "rsi":       data.get("rsi", 0.0),
                    "slope":     data.get("slope", 0.0),
                }
                to_remove.append(symbol); changed = True

            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                logger.info(f"[{symbol}] Ordin {status}")
                self.closed_trades.append({
                    "symbol": symbol, "direction": data.get("direction","?"),
                    "entry": data.get("entry",0), "sl": data["sl"], "tp": data["tp"],
                    "result": "EXPIRED", "pnl": 0.0,
                    "open_time": data.get("open_time",""),
                    "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                })
                to_remove.append(symbol); changed = True

        for sym in to_remove:
            self.pending_orders.pop(sym, None)
        return changed


    def _check_active_positions(self) -> bool:
        if not self.active_positions:
            return False
        try:
            real_open = {p["symbol"] for p in self.client.futures_position_information()
                         if abs(float(p["positionAmt"])) > 0}
        except BinanceAPIException as e:
            if e.code == -1003:
                logger.warning("_check_active: rate limit — skip acest ciclu")
                return False  # nu spam, asteapta urmatorul ciclu de 10s
            logger.error(f"_check_active error: {e}"); return False
        except Exception as e:
            logger.error(f"_check_active error: {e}"); return False

        to_close = []; changed = False
        for symbol, pos in list(self.active_positions.items()):
            if symbol in real_open:
                continue  # inca deschisa

            try:
                open_ts = int(pos["open_ts"])
                end_ts  = int(t.time() * 1000)
                
                # API Call cu "greutate" mare (30 Weight)
                income  = self.client.futures_income_history(
                    symbol=symbol, incomeType="REALIZED_PNL",
                    startTime=open_ts, endTime=end_ts, limit=20
                )
                
                # PROTECȚIE CRITICĂ RATE LIMIT
                t.sleep(0.5) 
                
                pnl = sum(float(x["income"]) for x in income) if income else 0.0
                if pnl == 0.0 and not income:
                    logger.warning(f"[{symbol}] PNL=0 — retry urmator ciclu")
                    continue

                result     = "TP" if pnl > 0 else "SL"
                close_time = t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime())
                sign       = "+" if pnl >= 0 else ""
                logger.info(f"[{symbol}] {'✅ TP' if result=='TP' else '❌ SL'} | PNL: {sign}{pnl:.4f} USDT")

                trade_record = {
                    "symbol": symbol, "direction": pos["direction"],
                    "entry": pos["entry"], "sl": pos["sl"], "tp": pos["tp"],
                    "result": result, "pnl": round(pnl, 4),
                    "open_time": pos["open_time"], "close_time": close_time,
                    "rsi": pos.get("rsi",0), "slope": pos.get("slope",0),
                }
                self.closed_trades.append(trade_record)

                try:
                    from notifier import notify_trade_closed
                    dur_h = (end_ts - open_ts) / 3600000
                    notify_trade_closed(
                        symbol=symbol, direction=pos["direction"],
                        entry=pos["entry"], sl=pos["sl"], tp=pos["tp"],
                        result=result, pnl_usdt=pnl,
                        open_time=pos["open_time"], close_time=close_time,
                        rsi=pos.get("rsi", 0.0), duration_h=dur_h,
                    )
                except Exception as ne:
                    logger.warning(f"[{symbol}] notify error: {ne}")

                try:
                    import journal
                    journal.log_trade(
                        symbol=symbol, direction=pos["direction"],
                        entry=pos["entry"], sl=pos["sl"], tp=pos["tp"],
                        result=result, pnl_usdt=pnl, usdt_per_trade=USDT_PER_TRADE,
                        open_time=pos["open_time"], close_time=close_time,
                        rsi=pos.get("rsi",0), ema_slope=pos.get("slope",0),
                    )
                except Exception: pass

                to_close.append(symbol); changed = True

            except Exception as e:
                logger.error(f"[{symbol}] get PNL error: {e}")

        for sym in to_close:
            self.active_positions.pop(sym, None)
        return changed

    def _expire_old_orders(self) -> bool:
        expiry_ms = config.ORDER_EXPIRY_HOURS * 3600 * 1000
        now_ms    = int(t.time() * 1000)
        to_expire = []; changed = False
        for symbol, oi in list(self.pending_orders.items()):
            if now_ms - oi.get("open_ts", now_ms) >= expiry_ms:
                age_h = (now_ms - oi.get("open_ts", now_ms)) / 3600000
                logger.info(f"[{symbol}] Expirat dupa {age_h:.1f}h — anulez...")
                try:
                    self.client.futures_cancel_order(symbol=symbol, orderId=oi["order_id"])
                    
                    # PROTECȚIE RATE LIMIT
                    t.sleep(0.2)
                    
                    self.closed_trades.append({
                        "symbol": symbol, "direction": oi.get("direction","?"),
                        "entry": oi.get("entry",0), "sl": oi["sl"], "tp": oi["tp"],
                        "result": "EXPIRED", "pnl": 0.0,
                        "open_time": oi.get("open_time",""),
                        "close_time": t.strftime("%Y-%m-%dT%H:%M:%SZ", t.gmtime()),
                    })
                    changed = True
                except Exception as e:
                    logger.error(f"[{symbol}] cancel error: {e}")
                to_expire.append(symbol)
        for sym in to_expire:
            self.pending_orders.pop(sym, None)
        return changed

    # ─────────────────────────────────────────────
    #  STATISTICI
    # ─────────────────────────────────────────────

    def get_bot_stats(self) -> dict:
        closed  = [x for x in self.closed_trades if x["result"] in ("TP","SL")]
        expired = [x for x in self.closed_trades if x["result"] == "EXPIRED"]
        if not closed:
            return {"total":0,"wins":0,"losses":0,"expired":len(expired),
                    "pnl_total":0.0,"pnl_today":0.0,"win_rate":0.0,
                    "best":0.0,"worst":0.0,
                    "active":len(self.active_positions),"pending":len(self.pending_orders)}
        wins   = [x for x in closed if x["result"]=="TP"]
        losses = [x for x in closed if x["result"]=="SL"]
        pnls   = [x["pnl"] for x in closed]
        today  = t.strftime("%Y-%m-%d", t.gmtime())
        return {
            "total":     len(closed),
            "wins":      len(wins),
            "losses":    len(losses),
            "expired":   len(expired),
            "pnl_total": round(sum(pnls), 4),
            "pnl_today": round(sum(x["pnl"] for x in closed
                                   if x.get("close_time","")[:10]==today), 4),
            "win_rate":  round(len(wins)/len(closed)*100, 1),
            "best":      round(max(pnls), 4),
            "worst":     round(min(pnls), 4),
            "active":    len(self.active_positions),
            "pending":   len(self.pending_orders),
        }

    # ─────────────────────────────────────────────
    #  PLASARE TRADE
    # ─────────────────────────────────────────────

    def set_leverage(self, symbol):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
        except BinanceAPIException as e:
            logger.warning(f"[{symbol}] leverage: {e}")

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
            logger.info(f"[{symbol}] LIMIT {side} | id={order_id} | qty={qty} | "
                        f"entry={entry_r} | sl={sl_r} | tp={tp_r}")
            self.pending_orders[symbol] = {
                "order_id":   order_id,
                "sl":         sl_r, "tp": tp_r, "qty": qty,
                "close_side": close_side, "entry": entry_r,
                "direction":  side, "open_time": open_time,
                "open_ts":    open_ts,
                "rsi":        getattr(setup, "rsi", 0.0),
                "slope":      getattr(setup, "slope_fast", 0.0),
            }
            self._save()
            return True

        except BinanceAPIException as e:
            if e.code == -2019:
                logger.warning(f"[{symbol}] margin insuficient")
            else:
                logger.error(f"[{symbol}] BinanceAPIException: {e}")
            return False
        except Exception as e:
            logger.error(f"[{symbol}] Eroare: {e}")
            return False

    # ─────────────────────────────────────────────
    #  UTILITARE
    # ─────────────────────────────────────────────

    def count_active_trades(self):
        return len(self.pending_orders) + len(self.active_positions)

    def has_symbol(self, symbol):
        return symbol in self.pending_orders or symbol in self.active_positions

    def is_at_capacity(self):
        return self.count_active_trades() >= config.MAX_OPEN_TRADES
