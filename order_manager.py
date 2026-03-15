"""
OrderManager v4

Fix principal:
- SL/TP plasate via ordine STOP_MARKET/TAKE_PROFIT_MARKET standard
  in loc de /fapi/v1/algoOrder care nu functioneaza pe toate simbolurile
- USDT_PER_TRADE redus la 7 pentru sizing corect
- 1000WHYUSDT adaugat in blacklist automat daca da erori repetate
"""
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import config
import notifier

logger = logging.getLogger("FVGBot")


class OrderManager:
    def __init__(self, client):
        self.client = client
        self.pending_orders  = {}
        self.open_positions  = {}
        self.error_counts    = {}  # numara erorile per simbol
        self.stats = {
            "total_trades":    0,
            "wins":            0,
            "losses":          0,
            "expired_orders":  0,
            "pnl_total":       0.0,
            "pnl_today":       0.0,
            "commission_paid": 0.0,
            "best_trade":      0.0,
            "worst_trade":     0.0,
            "start_time":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "last_report_day": datetime.now(timezone.utc).date(),
        }

    # ── Contoare ──────────────────────────────────────────────

    def count_open_positions(self) -> int:
        return len(self.open_positions)

    def count_pending_orders(self) -> int:
        return len(self.pending_orders)

    def is_at_capacity(self) -> bool:
        return self.count_open_positions() >= config.MAX_OPEN_TRADES

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self.pending_orders or symbol in self.open_positions

    # ── Qty calculator ─────────────────────────────────────────

    def _calc_qty(self, symbol: str, entry: float, sl: float) -> Optional[float]:
        """Calculeaza cantitatea bazata pe risk fix."""
        risk_dist = abs(entry - sl) / entry
        if risk_dist <= 0:
            return None

        risk_usdt = config.USDT_PER_TRADE
        notional  = risk_usdt / risk_dist
        # Limita: max 5x USDT_PER_TRADE nominal pentru a evita pozitii uriase
        max_notional = config.USDT_PER_TRADE * 5
        notional = min(notional, max_notional)

        try:
            info      = self.client.futures_exchange_info()
            sym_info  = next(s for s in info["symbols"] if s["symbol"] == symbol)
            step_size = float(next(
                f["stepSize"] for f in sym_info["filters"]
                if f["filterType"] == "LOT_SIZE"
            ))
            min_qty = float(next(
                f["minQty"] for f in sym_info["filters"]
                if f["filterType"] == "LOT_SIZE"
            ))
            qty = notional / entry
            qty = round(qty - (qty % step_size), 8)
            if qty < min_qty:
                logger.warning(f"[{symbol}] Qty {qty} sub minimul {min_qty}")
                return None
            return qty
        except Exception as e:
            logger.error(f"[{symbol}] Qty calc error: {e}")
            return None

    # ── Price precision ────────────────────────────────────────

    def _round_price(self, symbol: str, price: float) -> float:
        """Rotunjeste pretul la tick size-ul simbolului."""
        try:
            info     = self.client.futures_exchange_info()
            sym_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
            tick     = float(next(
                f["tickSize"] for f in sym_info["filters"]
                if f["filterType"] == "PRICE_FILTER"
            ))
            if tick > 0:
                price = round(round(price / tick) * tick, 10)
        except Exception:
            pass
        return price

    # ── Plasare trade ──────────────────────────────────────────

    def place_fvg_trade(self, setup) -> bool:
        symbol    = setup.symbol
        direction = setup.direction
        entry     = setup.entry
        sl        = setup.sl
        tp        = setup.tp

        # Seteaza leverage
        try:
            self.client.futures_change_leverage(
                symbol=symbol, leverage=config.LEVERAGE
            )
        except Exception as e:
            logger.warning(f"[{symbol}] Leverage error: {e}")

        # Calculeaza cantitatea
        qty = self._calc_qty(symbol, entry, sl)
        if qty is None:
            return False

        # Rotunjeste pretul
        entry = self._round_price(symbol, entry)

        # Plaseaza LIMIT entry
        side = "BUY" if direction == "BULL" else "SELL"
        try:
            order = self.client.futures_create_order(
                symbol      = symbol,
                side        = side,
                type        = "LIMIT",
                timeInForce = "GTC",
                quantity    = qty,
                price       = entry,
            )
            order_id = order["orderId"]
            logger.info(
                f"[{symbol}] LIMIT {direction} plasat | "
                f"Entry={entry} SL={sl} TP={tp} Qty={qty} | "
                f"OrderID={order_id}"
            )
            self.pending_orders[symbol] = {
                "order_id":  order_id,
                "direction": direction,
                "entry":     entry,
                "sl":        sl,
                "tp":        tp,
                "qty":       qty,
                "risk_usdt": config.USDT_PER_TRADE,
                "placed_at": time.time(),
            }
            # Reseteaza error count la succes
            self.error_counts[symbol] = 0
            return True

        except Exception as e:
            logger.error(f"[{symbol}] Place order error: {e}")
            self.error_counts[symbol] = self.error_counts.get(symbol, 0) + 1
            return False

    # ── SL/TP via ordine standard ──────────────────────────────

    def _place_single_order(self, symbol: str, side: str,
                            order_type: str, price: float,
                            qty: float) -> str:
        """
        Incearca sa plaseze un ordin standard.
        Daca da -4120 (necesita algo), foloseste algo endpoint automat.
        Returneaza order_id ca string sau "ERR".
        """
        import hmac, hashlib, requests, time as _time

        # Incearca standard
        try:
            order = self.client.futures_create_order(
                symbol      = symbol,
                side        = side,
                type        = order_type,
                stopPrice   = price,
                quantity    = qty,
                reduceOnly  = True,
                timeInForce = "GTC",
                workingType = "CONTRACT_PRICE",
            )
            oid = str(order.get("orderId", "?"))
            logger.info(f"[{symbol}] {order_type} standard plasat la {price} (id={oid})")
            return oid
        except Exception as e:
            err_str = str(e)
            if "-4120" not in err_str:
                logger.error(f"[{symbol}] {order_type} error: {e}")
                return "ERR"
            logger.info(f"[{symbol}] Standard nu suportat — incerc algo endpoint...")

        # Fallback: algo endpoint
        FAPI = "https://fapi.binance.com"
        algo_type = "STOP_MARKET" if order_type == "STOP_MARKET" else "TAKE_PROFIT_MARKET"
        params = {
            "symbol":       symbol,
            "side":         side,
            "type":         algo_type,
            "algoType":     "CONDITIONAL",
            "triggerPrice": str(price),
            "quantity":     str(qty),
            "reduceOnly":   "true",
            "workingType":  "CONTRACT_PRICE",
            "timestamp":    int(_time.time() * 1000),
            "recvWindow":   5000,
        }
        query   = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig     = hmac.new(
            config.API_SECRET.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = sig
        try:
            r    = requests.post(
                url     = f"{FAPI}/fapi/v1/algoOrder",
                data    = params,
                headers = {"X-MBX-APIKEY": config.API_KEY},
                timeout = 15,
            )
            resp = r.json()
            oid  = str(resp.get("clientAlgoId", resp.get("orderId", "?")))
            logger.info(f"[{symbol}] {order_type} algo plasat la {price} (id={oid})")
            return oid
        except Exception as e2:
            logger.error(f"[{symbol}] {order_type} algo error: {e2}")
            return "ERR"

    def _place_sl_tp(self, symbol: str, direction: str,
                     sl: float, tp: float, qty: float) -> tuple:
        """
        Plaseaza SL si TP cu fallback automat:
        1. Incearca standard STOP_MARKET / TAKE_PROFIT_MARKET
        2. Daca da -4120 → foloseste /fapi/v1/algoOrder automat
        """
        close_side = "SELL" if direction == "BULL" else "BUY"

        # Rotunjeste preturile
        sl = self._round_price(symbol, sl)
        tp = self._round_price(symbol, tp)

        sl_id = self._place_single_order(symbol, close_side, "STOP_MARKET",        sl, qty)
        tp_id = self._place_single_order(symbol, close_side, "TAKE_PROFIT_MARKET", tp, qty)

        return sl_id, tp_id

    # ── Check filled orders ────────────────────────────────────

    def check_filled_orders(self):
        self._check_pending()
        self._check_open_positions()
        self._expire_old_orders()

    def _check_pending(self):
        to_remove = []
        for symbol, order_info in list(self.pending_orders.items()):
            try:
                order  = self.client.futures_get_order(
                    symbol=symbol, orderId=order_info["order_id"]
                )
                status = order.get("status", "")

                if status == "FILLED":
                    filled_price = float(order.get("avgPrice", order_info["entry"]))
                    logger.info(f"[{symbol}] Ordin UMPLUT la {filled_price}")
                    self._on_filled(symbol, order_info, filled_price)
                    to_remove.append(symbol)

                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.info(f"[{symbol}] Ordin {status} — eliminat")
                    to_remove.append(symbol)

            except Exception as e:
                logger.error(f"[{symbol}] Check order error: {e}")

        for sym in to_remove:
            self.pending_orders.pop(sym, None)

    def _on_filled(self, symbol: str, order_info: dict, filled_price: float):
        """Dupa umplere: plaseaza SL si TP, muta in open_positions."""
        direction = order_info["direction"]
        sl        = order_info["sl"]
        tp        = order_info["tp"]
        qty       = order_info["qty"]

        sl_id, tp_id = self._place_sl_tp(symbol, direction, sl, tp, qty)

        self.open_positions[symbol] = {
            "direction":  direction,
            "entry":      filled_price,
            "sl":         sl,
            "tp":         tp,
            "sl_id":      sl_id,
            "tp_id":      tp_id,
            "risk_usdt":  order_info["risk_usdt"],
            "opened_at":  datetime.now(timezone.utc).isoformat(),
        }

        notifier.notify_filled(symbol, direction, filled_price)

    def _check_open_positions(self):
        """Verifica daca pozitiile botului mai exista in cont."""
        if not self.open_positions:
            return

        try:
            real_positions = self.client.futures_position_information()
            real_symbols   = {
                p["symbol"] for p in real_positions
                if abs(float(p["positionAmt"])) > 0
            }
        except Exception as e:
            logger.error(f"position_information error: {e}")
            return

        to_close = []
        for symbol in list(self.open_positions.keys()):
            if symbol not in real_symbols:
                self._on_position_closed(symbol, self.open_positions[symbol])
                to_close.append(symbol)

        for sym in to_close:
            self.open_positions.pop(sym, None)

    def _on_position_closed(self, symbol: str, pos_info: dict):
        """Detecteaza TP sau SL si actualizeaza statisticile."""
        direction = pos_info["direction"]
        sl        = pos_info["sl"]
        tp        = pos_info["tp"]
        risk      = pos_info["risk_usdt"]

        try:
            trades     = self.client.futures_account_trades(symbol=symbol, limit=5)
            exit_price = float(trades[-1]["price"]) if trades else (sl + tp) / 2
        except Exception:
            exit_price = (sl + tp) / 2

        if direction == "BULL":
            result = "TP" if exit_price >= tp * 0.995 else "SL"
        else:
            result = "TP" if exit_price <= tp * 1.005 else "SL"

        pnl        = risk if result == "TP" else -risk
        commission = risk * 0.0004 * 2
        net_pnl    = pnl - commission

        # Update statistici
        self.stats["total_trades"]    += 1
        self.stats["commission_paid"] += commission

        today = datetime.now(timezone.utc).date()
        if today != self.stats["last_report_day"]:
            self.stats["pnl_today"]       = 0.0
            self.stats["last_report_day"] = today

        self.stats["pnl_total"] += net_pnl
        self.stats["pnl_today"] += net_pnl

        if result == "TP":
            self.stats["wins"] += 1
            if net_pnl > self.stats["best_trade"]:
                self.stats["best_trade"] = net_pnl
        else:
            self.stats["losses"] += 1
            if net_pnl < self.stats["worst_trade"]:
                self.stats["worst_trade"] = net_pnl

        total = self.stats["total_trades"]
        wr    = self.stats["wins"] / total * 100 if total > 0 else 0
        sign  = "+" if net_pnl >= 0 else ""

        logger.info(
            f"[{symbol}] {'✅ TP' if result == 'TP' else '❌ SL'} | "
            f"PNL: {sign}{net_pnl:.2f} USDT | "
            f"{self.stats['wins']}W/{self.stats['losses']}L "
            f"({wr:.1f}%) | Cumulat: {self.stats['pnl_total']:+.2f} USDT"
        )
        notifier.notify_closed(symbol, direction, result, net_pnl)

    def _expire_old_orders(self):
        """Anuleaza ordinele neumplute dupa ORDER_EXPIRY_HOURS."""
        expiry_sec = config.ORDER_EXPIRY_HOURS * 3600
        now        = time.time()
        to_expire  = []

        for symbol, order_info in list(self.pending_orders.items()):
            if now - order_info["placed_at"] >= expiry_sec:
                logger.info(f"[{symbol}] Ordin expirat — anulez...")
                try:
                    self.client.futures_cancel_order(
                        symbol=symbol, orderId=order_info["order_id"]
                    )
                    self.stats["expired_orders"] += 1
                    notifier.notify_expired(symbol, config.ORDER_EXPIRY_HOURS)
                except Exception as e:
                    logger.error(f"[{symbol}] Cancel error: {e}")
                to_expire.append(symbol)

        for sym in to_expire:
            self.pending_orders.pop(sym, None)

    # ── Stats & utils ──────────────────────────────────────────

    def get_stats_for_report(self) -> dict:
        total = self.stats["total_trades"]
        wins  = self.stats["wins"]
        wr    = wins / total * 100 if total > 0 else 0
        return {
            "total_trades":    total,
            "wins":            wins,
            "losses":          self.stats["losses"],
            "expired_orders":  self.stats["expired_orders"],
            "pending":         self.count_pending_orders(),
            "open_positions":  self.count_open_positions(),
            "pnl_total":       round(self.stats["pnl_total"], 2),
            "pnl_today":       round(self.stats["pnl_today"], 2),
            "win_rate":        round(wr, 1),
            "best_trade":      round(self.stats["best_trade"], 2),
            "worst_trade":     round(self.stats["worst_trade"], 2),
            "commission_paid": round(self.stats["commission_paid"], 2),
            "start_time":      self.stats["start_time"],
        }

    def get_open_positions(self) -> set:
        return set(self.open_positions.keys())

    def get_pending_orders(self) -> set:
        return set(self.pending_orders.keys())

    def count_active_trades(self) -> int:
        return self.count_open_positions()
