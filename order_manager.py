import time
import logging
import requests
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Optional

import config
import notifier

logger = logging.getLogger("FVGBot")

FAPI = "https://fapi.binance.com"


def _sign(params: dict) -> dict:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig   = hmac.new(
        config.API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()
    params["signature"] = sig
    return params


def _algo_post(params: dict) -> Optional[dict]:
    params["timestamp"]  = int(time.time() * 1000)
    params["recvWindow"] = 5000
    params = _sign(params)
    try:
        r = requests.post(
            url=f"{FAPI}/fapi/v1/algoOrder",
            data=params,
            headers={"X-MBX-APIKEY": config.API_KEY},
            timeout=15
        )
        resp = r.json()
        if "orderId" not in str(resp) and "clientAlgoId" not in str(resp):
            logger.error(f"Algo order error: {resp}")
            return None
        return resp
    except Exception as e:
        logger.error(f"Algo order exception: {e}")
        return None


class OrderManager:
    def __init__(self, client):
        self.client = client
        self.pending_orders  = {}
        self.open_positions  = {}
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

    def count_open_positions(self) -> int:
        return len(self.open_positions)

    def count_pending_orders(self) -> int:
        return len(self.pending_orders)

    def is_at_capacity(self) -> bool:
        return self.count_open_positions() >= config.MAX_OPEN_TRADES

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self.pending_orders or symbol in self.open_positions

    def place_fvg_trade(self, setup) -> bool:
        symbol    = setup.symbol
        direction = setup.direction
        entry     = setup.entry
        sl        = setup.sl
        qty       = None

        try:
            self.client.futures_change_leverage(
                symbol=symbol, leverage=config.LEVERAGE
            )
        except Exception as e:
            logger.warning(f"[{symbol}] Leverage error: {e}")

        risk_usdt = config.USDT_PER_TRADE
        risk_dist = abs(entry - sl) / entry
        if risk_dist <= 0:
            return False

        try:
            info      = self.client.futures_exchange_info()
            sym_info  = next(s for s in info["symbols"] if s["symbol"] == symbol)
            step_size = float(next(
                f["stepSize"] for f in sym_info["filters"]
                if f["filterType"] == "LOT_SIZE"
            ))
            qty = risk_usdt / risk_dist / entry
            qty = round(qty - (qty % step_size), 8)
            if qty <= 0:
                return False
        except Exception as e:
            logger.error(f"[{symbol}] Qty calc error: {e}")
            return False

        side = "BUY" if direction == "BULL" else "SELL"
        try:
            order = self.client.futures_create_order(
                symbol      = symbol,
                side        = side,
                type        = "LIMIT",
                timeInForce = "GTC",
                quantity    = qty,
                price       = round(entry, 8),
            )
            order_id = order["orderId"]
            logger.info(
                f"[{symbol}] LIMIT {direction} plasat | "
                f"Entry={entry:.6f} SL={sl:.6f} TP={setup.tp:.6f} | "
                f"OrderID={order_id}"
            )
            self.pending_orders[symbol] = {
                "order_id":  order_id,
                "direction": direction,
                "entry":     entry,
                "sl":        sl,
                "tp":        setup.tp,
                "qty":       qty,
                "risk_usdt": risk_usdt,
                "placed_at": time.time(),
            }
            return True
        except Exception as e:
            logger.error(f"[{symbol}] Place order error: {e}")
            return False

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
                    logger.info(f"[{symbol}] Ordin UMPLUT")
                    filled_price = float(order.get("avgPrice", order_info["entry"]))
                    self._on_filled(symbol, order_info, filled_price)
                    to_remove.append(symbol)
                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.info(f"[{symbol}] Ordin {status}")
                    to_remove.append(symbol)
            except Exception as e:
                logger.error(f"[{symbol}] Check order error: {e}")
        for sym in to_remove:
            self.pending_orders.pop(sym, None)

    def _on_filled(self, symbol: str, order_info: dict, filled_price: float):
        direction  = order_info["direction"]
        sl         = order_info["sl"]
        tp         = order_info["tp"]
        qty        = order_info["qty"]
        close_side = "SELL" if direction == "BULL" else "BUY"

        sl_params = {
            "symbol":       symbol,
            "side":         close_side,
            "type":         "STOP_MARKET",
            "algoType":     "CONDITIONAL",
            "triggerPrice": str(round(sl, 8)),
            "quantity":     str(qty),
            "reduceOnly":   "true",
            "workingType":  "CONTRACT_PRICE",
        }
        sl_resp = _algo_post(sl_params)
        sl_id   = str(sl_resp.get("clientAlgoId", "?")) if sl_resp else "ERR"

        tp_params = {
            "symbol":       symbol,
            "side":         close_side,
            "type":         "TAKE_PROFIT_MARKET",
            "algoType":     "CONDITIONAL",
            "triggerPrice": str(round(tp, 8)),
            "quantity":     str(qty),
            "reduceOnly":   "true",
            "workingType":  "CONTRACT_PRICE",
        }
        tp_resp = _algo_post(tp_params)
        tp_id   = str(tp_resp.get("clientAlgoId", "?")) if tp_resp else "ERR"

        logger.info(
            f"[{symbol}] SL={sl:.6f} (id={sl_id}) | TP={tp:.6f} (id={tp_id})"
        )

        self.open_positions[symbol] = {
            "direction":  direction,
            "entry":      filled_price,
            "sl":         sl,
            "tp":         tp,
            "sl_algo_id": sl_id,
            "tp_algo_id": tp_id,
            "risk_usdt":  order_info["risk_usdt"],
            "opened_at":  datetime.now(timezone.utc).isoformat(),
        }
        notifier.notify_filled(symbol, direction, filled_price)

    def _check_open_positions(self):
        if not self.open_positions:
            return
        try:
            real_positions = self.client.futures_position_information()
            real_symbols   = {
                p["symbol"] for p in real_positions
                if abs(float(p["positionAmt"])) > 0
            }
        except Exception as e:
            logger.error(f"futures_position_information error: {e}")
            return

        to_close = []
        for symbol in list(self.open_positions.keys()):
            if symbol not in real_symbols:
                self._on_position_closed(symbol, self.open_positions[symbol])
                to_close.append(symbol)
        for sym in to_close:
            self.open_positions.pop(sym, None)

    def _on_position_closed(self, symbol: str, pos_info: dict):
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

        self.stats["total_trades"]   += 1
        self.stats["commission_paid"] += commission

        today = datetime.now(timezone.utc).date()
        if today != self.stats["last_report_day"]:
            self.stats["pnl_today"]      = 0.0
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
