"""
Notifier — Telegram notifications + raport statistica la 4 ore
"""
import requests
import logging
from datetime import datetime, timezone
try:
    import journal as _journal
except ImportError:
    _journal = None

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
    """Semnal FVG detectat — ordin LIMIT plasat."""
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
    """Confirmare plasare ordin."""
    if not success:
        _send(f"⚠️ <b>EROARE plasare ordin</b> {setup.symbol} {setup.direction}")


def notify_filled(symbol: str, direction: str, entry: float):
    """Ordin umplut — pozitie deschisa."""
    emoji = "🟢" if direction == "BULL" else "🔴"
    msg = (
        f"{emoji} <b>POZITIE DESCHISA — {symbol}</b>\n"
        f"Directie: {'LONG' if direction == 'BULL' else 'SHORT'}\n"
        f"Entry umplut la: <code>{entry:.6f}</code>\n"
        f"SL si TP plasate automat ✅"
    )
    _send(msg)


def notify_closed(symbol: str, direction: str, result: str, pnl: float):
    """Pozitie inchisa — TP sau SL."""
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
    """Ordin LIMIT anulat — nu s-a umplut in timp."""
    msg = (
        f"⏰ <b>ORDIN ANULAT — {symbol}</b>\n"
        f"Nu s-a umplut gap-ul in {hours}h\n"
        f"Setup invalid — pozitie eliberata"
    )
    _send(msg)


def notify_error(context: str, error: str):
    _send(f"🔥 <b>EROARE BOT</b>\n{context}\n<code>{error[:200]}</code>")


def notify_trade_closed(symbol: str, direction: str, entry: float,
                        sl: float, tp: float, result: str,
                        pnl_usdt: float, open_time: str, close_time: str,
                        rsi: float = 0.0, duration_h: float = 0.0):
    """
    Trimite notificare Telegram la fiecare trade inchis.
    Acesta e jurnalul permanent — istoricul raman in chat.
    """
    if result == "TP":
        emoji  = "✅"
        r_text = "TAKE PROFIT"
        sign   = "+"
    elif result == "SL":
        emoji  = "❌"
        r_text = "STOP LOSS"
        sign   = ""
    else:
        emoji  = "⏰"
        r_text = "TIMEOUT"
        sign   = "+" if pnl_usdt >= 0 else ""

    dir_emoji = "🟢 LONG" if direction in ("BUY","LONG") else "🔴 SHORT"
    pnl_color = "📈" if pnl_usdt >= 0 else "📉"

    # Calculeaza RR realizat
    risk  = abs(entry - sl)
    rw    = abs(entry - tp)
    rr    = round(rw / risk, 2) if risk > 0 else 0

    msg = (
        f"{emoji} <b>TRADE INCHIS — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Rezultat:  <b>{r_text}</b>\n"
        f"Directie:  {dir_emoji}\n"
        f"Entry:     <code>{entry:.6f}</code>\n"
        f"SL:        <code>{sl:.6f}</code>\n"
        f"TP:        <code>{tp:.6f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{pnl_color} PNL:  <b>{sign}{pnl_usdt:.4f} USDT</b>\n"
        f"⏱ Durata: <b>{duration_h:.1f}h</b>\n"
        f"📊 RSI entry: {rsi:.1f}\n"
        f"🎯 RR: 1:{rr}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Deschis:  {open_time[:16].replace('T',' ')} UTC</i>\n"
        f"<i>Inchis:   {close_time[:16].replace('T',' ')} UTC</i>"
    )
    _send(msg)


def send_statistics_report(stats: dict):
    """
    Raport complet cu date reale din Binance.
    """
    # Conversie explicita - valorile din JSON pot fi strings
    total    = int(stats.get("total_trades", 0) or 0)
    wins     = int(stats.get("wins", 0) or 0)
    losses   = int(stats.get("losses", 0) or 0)
    pending  = int(stats.get("pending", 0) or 0)
    open_pos = int(stats.get("open_positions", 0) or 0)
    pnl      = float(stats.get("pnl_total", 0.0) or 0.0)
    pnl_today= float(stats.get("pnl_today", 0.0) or 0.0)
    wr       = float(stats.get("win_rate", 0.0) or 0.0)
    best     = float(stats.get("best_trade", 0.0) or 0.0)
    worst    = float(stats.get("worst_trade", 0.0) or 0.0)
    comm     = float(stats.get("commission_paid", 0.0) or 0.0)
    started  = str(stats.get("start_time", "?"))

    pnl_sign       = "+" if pnl >= 0 else ""
    pnl_today_sign = "+" if pnl_today >= 0 else ""
    pnl_emoji      = "📈" if pnl >= 0 else "📉"
    wr_emoji       = "🔥" if wr >= 65 else ("✅" if wr >= 50 else "⚠️")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if total == 0:
        trades_line = "Niciun trade inchis inca"
    else:
        trades_line = f"{wr_emoji} Win Rate: <b>{wr:.1f}%</b> ({wins}✅ / {losses}❌)"

    msg = (
        f"<b>📊 RAPORT BOT FVG — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>REZULTATE REALE (Binance)</b>\n"
        f"Trade-uri inchise: <b>{total}</b>\n"
        f"{trades_line}\n"
        f"{pnl_emoji} PNL Total: <b>{pnl_sign}{pnl:.4f} USDT</b>\n"
        f"   Ultimele 24h: <b>{pnl_today_sign}{pnl_today:.4f} USDT</b>\n"
        f"   Comisioane: -{comm:.4f} USDT\n"
    )

    if total > 0:
        msg += (
            f"   Cel mai bun: <b>+{best:.4f} USDT</b>\n"
            f"   Cel mai prost: <b>{worst:.4f} USDT</b>\n"
        )

    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>SITUATIE CURENTA</b>\n"
        f"Pozitii deschise: <b>{open_pos}</b> / {config.MAX_OPEN_TRADES}\n"
        f"Ordine pending:   <b>{pending}</b>\n"
        f"Bot activ de la: {started}"
    )

    _send(msg)
