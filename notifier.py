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


def send_statistics_report(stats: dict):
    """
    Raport complet la fiecare 4 ore.
    stats = {
        total_trades, wins, losses, pending,
        open_positions, pnl_total, pnl_today,
        win_rate, best_trade, worst_trade,
        commission_paid, start_time
    }
    """
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

    # Citeste statistici din jurnal
    j = {}
    if _journal:
        try:
            j = _journal.get_stats()
        except Exception:
            j = {}

    j_total   = j.get("total", total)
    j_wins    = j.get("wins",  wins)
    j_losses  = j.get("losses", losses)
    j_expired = j.get("expired", expired)
    j_pnl     = j.get("pnl_total", pnl)
    j_wr      = j.get("win_rate", wr)
    j_best    = j.get("best", best)
    j_worst   = j.get("worst", worst)
    j_wr_emoji = "🔥" if j_wr >= 65 else ("✅" if j_wr >= 50 else "⚠️")
    j_pnl_sign = "+" if j_pnl >= 0 else ""
    j_pnl_emoji = "📈" if j_pnl >= 0 else "📉"

    # Top simboluri
    top_sym_text = ""
    for sym, spnl in j.get("top_symbols", []):
        sign = "+" if spnl >= 0 else ""
        top_sym_text += f"  • {sym}: {sign}{spnl:.2f} USDT\n"

    # Ore cele mai bune
    best_h_text = ""
    for h, hwr in j.get("best_hours", []):
        best_h_text += f"  • {h:02d}:00 UTC → {hwr:.0f}% WR\n"

    msg = (
        f"<b>📊 RAPORT BOT FVG — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>STATISTICI REALE (jurnal live)</b>\n"
        f"Trade-uri totale: <b>{j_total}</b>\n"
        f"{j_wr_emoji} Win Rate: <b>{j_wr:.1f}%</b> ({j_wins}✅ / {j_losses}❌ / {j_expired}⏰)\n"
        f"{j_pnl_emoji} PNL Total: <b>{j_pnl_sign}{j_pnl:.4f} USDT</b>\n"
        f"Best: +{j_best:.4f} | Worst: {j_worst:.4f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>SITUATIE CURENTA</b>\n"
        f"Pozitii deschise: <b>{open_pos}</b> / {config.MAX_OPEN_TRADES}\n"
        f"Ordine pending:   <b>{pending}</b>\n"
        f"De la: {started}\n"
    )
    if top_sym_text:
        msg += f"━━━━━━━━━━━━━━━━━━━━\n<b>TOP SIMBOLURI</b>\n{top_sym_text}"
    if best_h_text:
        msg += f"<b>CELE MAI BUNE ORE</b>\n{best_h_text}"
    msg += f"━━━━━━━━━━━━━━━━━━━━"
    
    _send(msg)
