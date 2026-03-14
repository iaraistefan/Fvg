import requests
import logging
from datetime import datetime, timezone

logger = logging.getLogger("FVGBot")

try:
    import config
except ImportError:
    config = None


def _send(text: str):
    if not config or not config.TELEGRAM_ENABLED:
        return
    try:
        url  = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
        data = {
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
        }
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram send error: {e}")


def notify_setup(setup):
    direction_emoji = "🟢 LONG" if setup.direction == "BULL" else "🔴 SHORT"
    msg = (
        f"<b>📡 FVG SEMNAL — {setup.symbol}</b>\n"
        f"Directie: {direction_emoji}\n"
        f"Entry (LIMIT): <code>{setup.entry:.6f}</code>\n"
        f"Stop Loss:     <code>{setup.sl:.6f}</code>\n"
        f"Take Profit:   <code>{setup.tp:.6f}</code>\n"
        f"RSI: {setup.rsi} | Slope EMA50: {setup.slope_fast:+.3f}%\n"
        f"⏰ Expira in {config.ORDER_EXPIRY_HOURS}h daca nu se umple"
    )
    _send(msg)


def notify_trade(setup, success: bool):
    if not success:
        _send(f"⚠️ <b>EROARE plasare ordin</b> {setup.symbol} {setup.direction}")


def notify_filled(symbol: str, direction: str, entry: float):
    emoji = "🟢" if direction == "BULL" else "🔴"
    msg = (
        f"{emoji} <b>POZITIE DESCHISA — {symbol}</b>\n"
        f"Directie: {'LONG' if direction == 'BULL' else 'SHORT'}\n"
        f"Entry umplut la: <code>{entry:.6f}</code>\n"
        f"SL si TP plasate automat ✅"
    )
    _send(msg)


def notify_closed(symbol: str, direction: str, result: str, pnl: float):
    if result == "TP":
        emoji = "✅"
        result_text = "TAKE PROFIT"
    else:
        emoji = "❌"
        result_text = "STOP LOSS"
    sign = "+" if pnl >= 0 else ""
    msg = (
        f"{emoji} <b>{result_text} — {symbol}</b>\n"
        f"Directie: {'LONG' if direction == 'BULL' else 'SHORT'}\n"
        f"PNL: <b>{sign}{pnl:.2f} USDT</b>"
    )
    _send(msg)


def notify_expired(symbol: str, hours: int):
    msg = (
        f"⏰ <b>ORDIN ANULAT — {symbol}</b>\n"
        f"Nu s-a umplut gap-ul in {hours}h\n"
        f"Setup invalid — pozitie eliberata"
    )
    _send(msg)


def notify_error(context: str, error: str):
    _send(f"🔥 <b>EROARE BOT</b>\n{context}\n<code>{error[:200]}</code>")


def send_statistics_report(stats: dict):
    total    = stats.get("total_trades", 0)
    wins     = stats.get("wins", 0)
    losses   = stats.get("losses", 0)
    pending  = stats.get("pending", 0)
    open_pos = stats.get("open_positions", 0)
    pnl      = stats.get("pnl_total", 0.0)
    pnl_today= stats.get("pnl_today", 0.0)
    wr       = stats.get("win_rate", 0.0)
    best     = stats.get("best_trade", 0.0)
    worst    = stats.get("worst_trade", 0.0)
    comm     = stats.get("commission_paid", 0.0)
    started  = stats.get("start_time", "?")
    expired  = stats.get("expired_orders", 0)

    pnl_sign       = "+" if pnl >= 0 else ""
    pnl_today_sign = "+" if pnl_today >= 0 else ""
    pnl_emoji      = "📈" if pnl >= 0 else "📉"
    wr_emoji       = "🔥" if wr >= 65 else ("✅" if wr >= 50 else "⚠️")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    msg = (
        f"<b>📊 RAPORT BOT FVG — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>STATISTICI TOTALE (de la {started})</b>\n"
        f"Trade-uri inchise: <b>{total}</b>\n"
        f"{wr_emoji} Win Rate: <b>{wr:.1f}%</b> ({wins}✅ / {losses}❌)\n"
        f"Ordine expirate: {expired}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>SITUATIE CURENTA</b>\n"
        f"Pozitii deschise: <b>{open_pos}</b> / {config.MAX_OPEN_TRADES}\n"
        f"Ordine pending:   <b>{pending}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>PNL</b>\n"
        f"{pnl_emoji} Total:  <b>{pnl_sign}{pnl:.2f} USDT</b>\n"
        f"   Azi:   <b>{pnl_today_sign}{pnl_today:.2f} USDT</b>\n"
        f"   Comisioane platite: -{comm:.2f} USDT\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>TRADE-URI</b>\n"
        f"Cel mai bun:  +{best:.2f} USDT\n"
        f"Cel mai prost: {worst:.2f} USDT\n"
    )
    _send(msg)
