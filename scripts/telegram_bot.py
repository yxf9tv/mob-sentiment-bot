"""Telegram bot with interactive controls for the MLB sentiment bot.

Provides Start/Stop, metrics dashboard, trade log, and Polymarket link.

Usage:
    python3 scripts/telegram_bot.py          # run with polling
    python3 scripts/telegram_bot.py --test   # test connection
"""

import json, os, sys, time, threading, signal
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
load_dotenv(BASE / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLY_FUNDER = os.getenv("POLY_FUNDER", "")
POLY_LIVE = os.getenv("POLY_LIVE", "0") == "1"

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# --- State ---
BOT_RUNNING = False
STOP_EVENT = threading.Event()

# --- File paths ---
STATE_PATH = DATA_DIR / "bot_state.json"
TRADE_LOG_PATH = DATA_DIR / "trade_log.jsonl"
LIVE_SIGNALS_PATH = DATA_DIR / "live_signals.json"
TRIGGER_EVENTS_PATH = DATA_DIR / "trigger_events.jsonl"
COMPOSITE_BACKTEST_PATH = DATA_DIR / "composite_backtest_results.json"
ACCURACY_BACKTEST_PATH = DATA_DIR / "accuracy_backtest_results.json"


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"running": False, "started_at": None, "trades": 0, "pnl": 0}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def api(method, **kwargs):
    """Call Telegram Bot API method."""
    try:
        resp = requests.post(f"{API_BASE}/{method}", json=kwargs, timeout=15)
        return resp.json()
    except Exception as e:
        print(f"[telegram] API error: {e}", file=sys.stderr)
        return {"ok": False}


def send_message(text, reply_markup=None):
    """Send a message, optionally with inline keyboard."""
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("sendMessage", **payload)


def edit_message(message_id, text, reply_markup=None):
    """Edit an existing message."""
    payload = {
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("editMessageText", **payload)


def answer_callback(callback_query_id, text=None, show_alert=False):
    """Acknowledge a callback query."""
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    return api("answerCallbackQuery", **payload)


# ──────────────────────────────────────────────
#  Keyboards
# ──────────────────────────────────────────────

def main_menu_keyboard():
    state = load_state()
    running = state.get("running", False)
    status_label = "STOP" if running else "START"
    status_emoji = "🟢" if running else "🔴"

    rows = [
        [
            {"text": f"{status_emoji} {status_label}", "callback_data": "bot_stop" if running else "bot_start"},
            {"text": "📊 Dashboard", "callback_data": "dashboard"},
        ],
        [
            {"text": "📈 Metrics", "callback_data": "metrics"},
            {"text": "📋 Trades", "callback_data": "trades"},
        ],
        [
            {"text": "⚙️ Config", "callback_data": "config"},
            {"text": "🧪 Backtest", "callback_data": "backtest"},
        ],
        [
            {"text": "🔗 Polymarket", "url": f"https://polymarket.com/profile/{POLY_FUNDER}" if POLY_FUNDER else "https://polymarket.com"},
        ],
    ]
    return {"inline_keyboard": rows}


def back_button(callback_data="menu"):
    return {"inline_keyboard": [[{"text": "⬅ Back", "callback_data": callback_data}]]}


# ──────────────────────────────────────────────
#  View builders
# ──────────────────────────────────────────────

def build_welcome():
    """First /start screen."""
    text = (
        "⚾ *MLB Sentiment Bot*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "AI-powered prediction market trading\n"
        "on Polymarket MLB markets.\n\n"
        "_Select an option below:_"
    )
    return text, main_menu_keyboard()


def build_main_menu():
    state = load_state()
    running = state.get("running", False)
    status = "🟢 RUNNING" if running else "🔴 STOPPED"
    uptime = ""
    if state.get("started_at") and running:
        started = datetime.fromisoformat(state["started_at"])
        delta = datetime.now(timezone.utc) - started
        h, m = divmod(int(delta.total_seconds()) // 60, 60)
        uptime = f"\n⏱ Uptime: {h}h {m}m"

    text = (
        f"⚾ *MLB Sentiment Bot*\n"
        f"Status: *{status}*{uptime}\n"
        f"Trades: {state.get('trades', 0)}  •  PnL: ${state.get('pnl', 0):+.2f}\n"
        f"\n_Select an option:_"
    )
    return text, main_menu_keyboard()


def build_dashboard():
    state = load_state()
    m = _metrics()
    running = state.get("running", False)
    status = "🟢 RUNNING" if running else "🔴 STOPPED"

    text = (
        f"📊 *Dashboard*\n"
        f"Status: {status}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Markets tracked:  {m['markets_seen']}\n"
        f"Signals fired:    {m['signals_fired']}\n"
        f"Trades placed:    {m['total_trades']}\n"
        f"Avg score:        {m['avg_score']:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Total cost:  ${m['total_cost']:.2f}\n"
        f"PnL:         ${state.get('pnl', 0):+.2f}\n"
        f"Mode:        {'LIVE' if POLY_LIVE else 'DRY RUN'}\n"
    )
    return text, main_menu_keyboard()


def build_metrics():
    m = _metrics()
    roi = m["pnl"] / m["total_cost"] * 100 if m["total_cost"] > 0 else 0

    text = (
        f"📈 *Performance*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Trades:     {m['total_trades']}\n"
        f"Wins:       {m['wins']}\n"
        f"Losses:     {m['losses']}\n"
        f"Pending:    {m['pending']}\n"
        f"Accuracy:   {m['accuracy']:.1%}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Cost:       ${m['total_cost']:.2f}\n"
        f"Return:     ${m['total_return']:.2f}\n"
        f"PnL:        ${m['pnl']:+.2f}\n"
        f"ROI:        {roi:.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Avg score:  {m['avg_score']:.2f}\n"
    )
    return text, main_menu_keyboard()


def build_trades():
    trades = _load_trades()
    if not trades:
        text = (
            "📋 *No trades yet*\n\n"
            "Trades will appear here once the bot starts trading.\n"
            "Use /start to activate the bot."
        )
        return text, main_menu_keyboard()

    recent = trades[-5:]
    lines = ["📋 *Recent Trades*\n"]
    for t in reversed(recent):
        slug = t.get("slug", "?")
        parts = slug.split("-")
        if len(parts) >= 4 and parts[0] == "mlb":
            teams = f"{parts[1].upper()} @ {parts[2].upper()}"
        else:
            teams = slug[:30]
        outcome = t.get("outcome", "?")
        price = t.get("price", 0)
        size = t.get("size", 0)
        cost = t.get("cost", 0)
        score = t.get("composite_score", 0)
        ts = t.get("ts", "")[:16].replace("T", " ")
        live_tag = "🔴" if t.get("live") else "🟡"
        lines.append(f"{live_tag} `{ts}`")
        lines.append(f"   {teams} → *{outcome}*")
        lines.append(f"   {size} sh @ {price:.2f} = ${cost:.2f}  (score {score:.2f})")
        lines.append("")

    lines.append(f"_Showing last 5 of {len(trades)} total_")
    return "\n".join(lines), main_menu_keyboard()


def build_config():
    state = load_state()
    bet_size = float(os.getenv("BOT_MAX_BET", "100"))
    bankroll = float(os.getenv("BOT_BANKROLL", "10000"))
    fraction = float(os.getenv("BOT_BET_FRACTION", "0.02"))

    text = (
        f"⚙️ *Configuration*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Mode:        {'LIVE' if POLY_LIVE else 'DRY RUN'}\n"
        f"Bankroll:    ${bankroll:,.0f}\n"
        f"Bet size:    ${bet_size:.0f}  ({fraction:.0%} of bankroll)\n"
        f"Threshold:   0.40\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Wallet:      `{POLY_FUNDER[:10]}...{POLY_FUNDER[-4:]}`\n" if POLY_FUNDER else
        f"Wallet:      _not set_\n"
        f"\n_To change, edit_ `.env` _and restart._"
    )
    return text, main_menu_keyboard()


def build_backtest():
    text = (
        "🧪 *Backtest Results*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
    )

    if COMPOSITE_BACKTEST_PATH.exists():
        with open(COMPOSITE_BACKTEST_PATH) as f:
            data = json.load(f)
        combos = data.get("combos_tested", 0)
        text += f"Composite sweep: {combos} combos tested\n"

    if ACCURACY_BACKTEST_PATH.exists():
        with open(ACCURACY_BACKTEST_PATH) as f:
            data = json.load(f)
        best = data.get("best_config", {})
        if best.get("metrics"):
            m = best["metrics"]
            text += (
                f"\n*Best config:* {best.get('name', '?')}\n"
                f"Threshold: {best.get('threshold', '?')}\n"
                f"Accuracy:  {m.get('accuracy', 0):.1%}\n"
                f"Bets:      {m.get('total', 0)}\n"
                f"ROI:       {m.get('roi', 0):.1f}%\n"
            )
    else:
        text += "_No backtest data yet_\n"

    # Income projections
    text += (
        "\n💰 *Projections* ($100/bet)\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Weekly:   ~$1,155\n"
        "Monthly:  ~$4,950\n"
    )

    return text, main_menu_keyboard()


def build_help():
    text = (
        "❓ *Commands*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "/start      — Main menu\n"
        "/menu       — Main menu\n"
        "/status     — Quick status\n"
        "/dashboard  — Live dashboard\n"
        "/metrics    — Performance stats\n"
        "/trades     — Recent trades\n"
        "/config     — Current settings\n"
        "/backtest   — Backtest results\n"
        "/signals    — Active signals\n"
        "/stop       — Stop bot\n"
        "/help       — This message\n"
        "\n_Buttons work too — just tap!_"
    )
    return text, main_menu_keyboard()


def build_signals():
    if not LIVE_SIGNALS_PATH.exists():
        text = "📡 *No signals*\n\nRun `poll_live.py` first to generate signals."
        return text, main_menu_keyboard()

    with open(LIVE_SIGNALS_PATH) as f:
        data = json.load(f)

    consensus = data.get("live_consensus", [])
    if not consensus:
        text = "📡 *No active signals*\n\nNo markets meet the composite threshold."
        return text, main_menu_keyboard()

    lines = [f"📡 *Active Signals ({len(consensus)})*\n"]
    for m in consensus[:10]:
        slug = m.get("slug", "?")
        parts = slug.split("-")
        if len(parts) >= 4 and parts[0] == "mlb":
            teams = f"{parts[1].upper()} @ {parts[2].upper()}"
        else:
            teams = slug[:25]
        outcome = m.get("top_outcome", "?")
        score = m.get("composite_score", 0)
        conf = m.get("confidence", "")
        spike = "🔥" if m.get("confidence_spike") else ""
        lines.append(f"• *{teams}* → {outcome}")
        lines.append(f"  Score: {score:.2f} ({conf}) {spike}")
        lines.append("")

    if len(consensus) > 10:
        lines.append(f"_+{len(consensus)-10} more_")

    return "\n".join(lines), main_menu_keyboard()


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def _metrics():
    m = {
        "total_trades": 0, "wins": 0, "losses": 0, "pending": 0,
        "total_cost": 0, "total_return": 0, "pnl": 0,
        "accuracy": 0, "avg_score": 0, "markets_seen": 0, "signals_fired": 0,
    }
    trades = _load_trades()
    m["total_trades"] = len(trades)
    if trades:
        m["avg_score"] = sum(t.get("composite_score", 0) for t in trades) / len(trades)
        m["total_cost"] = sum(t.get("cost", 0) for t in trades)

    if LIVE_SIGNALS_PATH.exists():
        with open(LIVE_SIGNALS_PATH) as f:
            m["markets_seen"] = len(json.load(f).get("live_consensus", []))

    if TRIGGER_EVENTS_PATH.exists():
        with open(TRIGGER_EVENTS_PATH) as f:
            m["signals_fired"] = sum(1 for line in f if line.strip())

    if m["total_trades"] > 0:
        m["accuracy"] = m["wins"] / m["total_trades"]
    return m


def _load_trades():
    trades = []
    if TRADE_LOG_PATH.exists():
        with open(TRADE_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))
    return trades


# ──────────────────────────────────────────────
#  Callback router
# ──────────────────────────────────────────────

def _cb_dashboard(cq): return build_dashboard()
def _cb_metrics(cq):   return build_metrics()
def _cb_trades(cq):    return build_trades()
def _cb_config(cq):    return build_config()
def _cb_backtest(cq):  return build_backtest()
def _cb_help(cq):      return build_help()
def _cb_signals(cq):   return build_signals()
def _cb_menu(cq):      return build_main_menu()


def _cb_bot_start(cq):
    global BOT_RUNNING
    state = load_state()
    state["running"] = True
    state["started_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    BOT_RUNNING = True
    answer_callback(cq, "✅ Bot started")
    return build_main_menu()


def _cb_bot_stop(cq):
    global BOT_RUNNING
    state = load_state()
    state["running"] = False
    state["stopped_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    BOT_RUNNING = False
    answer_callback(cq, "⏹ Bot stopped")
    return build_main_menu()


HANDLERS = {
    "dashboard": _cb_dashboard,
    "metrics":   _cb_metrics,
    "trades":    _cb_trades,
    "config":    _cb_config,
    "backtest":  _cb_backtest,
    "help":      _cb_help,
    "signals":   _cb_signals,
    "menu":      _cb_menu,
    "bot_start": _cb_bot_start,
    "bot_stop":  _cb_bot_stop,
}


def handle_callback(update):
    query = update.get("callback_query", {})
    callback_id = query.get("id")
    data = query.get("data", "")
    message_id = query.get("message", {}).get("message_id")

    handler = HANDLERS.get(data)
    if handler:
        text, kb = handler(callback_id)
        if message_id:
            edit_message(message_id, text, reply_markup=kb)
    else:
        answer_callback(callback_id, f"Unknown: {data}")


def handle_message(update):
    msg = update.get("message", {})
    text = (msg.get("text") or "").lower().strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))

    # Only respond to configured chat
    if CHAT_ID and chat_id != CHAT_ID:
        return

    CMD_MAP = {
        "/start":    lambda: build_welcome(),
        "/menu":     lambda: build_main_menu(),
        "/status":   lambda: _status_text(),
        "/dashboard": lambda: build_dashboard(),
        "/metrics":  lambda: build_metrics(),
        "/trades":   lambda: build_trades(),
        "/config":   lambda: build_config(),
        "/backtest": lambda: build_backtest(),
        "/signals":  lambda: build_signals(),
        "/help":     lambda: build_help(),
        "/stop":     lambda: _stop_text(),
    }

    if text in CMD_MAP:
        result = CMD_MAP[text]()
        if isinstance(result, tuple):
            send_message(result[0], reply_markup=result[1])
        else:
            send_message(result)
    elif text in ("menu", "start", "help", "status"):
        # Plain text aliases
        if text == "menu" or text == "start":
            send_message(*build_welcome())
        elif text == "help":
            send_message(*build_help())
        elif text == "status":
            send_message(*_status_text())


def _status_text():
    state = load_state()
    running = state.get("running", False)
    status = "🟢 RUNNING" if running else "🔴 STOPPED"
    uptime = ""
    if state.get("started_at") and running:
        started = datetime.fromisoformat(state["started_at"])
        delta = datetime.now(timezone.utc) - started
        h, m = divmod(int(delta.total_seconds()) // 60, 60)
        uptime = f"  ⏱ {h}h {m}m"
    text = f"Status: *{status}*{uptime}\nTrades: {state.get('trades', 0)}  •  PnL: ${state.get('pnl', 0):+.2f}"
    return text, main_menu_keyboard()


def _stop_text():
    global BOT_RUNNING
    state = load_state()
    state["running"] = False
    state["stopped_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    BOT_RUNNING = False
    return "⏹ *Bot stopped.*\n\nUse /start to reactivate.", main_menu_keyboard()


# ──────────────────────────────────────────────
#  Polling
# ──────────────────────────────────────────────

def poll_updates(offset=0):
    try:
        resp = requests.get(
            f"{API_BASE}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        return resp.json()
    except Exception as e:
        print(f"[telegram] poll error: {e}", file=sys.stderr)
        return {"ok": False, "result": []}


def run_bot():
    print("Telegram bot started. Listening for commands...")
    offset = 0

    while not STOP_EVENT.is_set():
        data = poll_updates(offset)
        if not data.get("ok"):
            time.sleep(5)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    handle_callback(update)
                elif "message" in update:
                    handle_message(update)
            except Exception as e:
                print(f"[telegram] handle error: {e}", file=sys.stderr)

    print("Bot stopped.")


# ──────────────────────────────────────────────
#  Trade alert (called from bot.py)
# ──────────────────────────────────────────────

def send_trade_alert(trade, market_info):
    slug = market_info.get("slug", "")
    outcome = trade.get("outcome", "?")
    price = trade.get("price", 0)
    size = trade.get("size", 0)
    cost = trade.get("cost", 0)
    score = market_info.get("composite_score", 0)
    confidence = market_info.get("confidence", "")
    live = trade.get("live", False)

    mode = "🔴 LIVE" if live else "🟡 DRY RUN"
    text = (
        f"⚾ *{mode} — Trade Executed*\n"
        f"`{slug}`\n"
        f"→ *{outcome}*\n\n"
        f"Price:\t{price:.2f}\n"
        f"Size:\t{size} shares\n"
        f"Cost:\t${cost:.2f}\n"
        f"Score:\t{score:.2f} ({confidence})\n"
    )
    send_message(text)


def send_test():
    return send_message("✅ *MLB Sentiment Bot — connection successful*")


# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Telegram bot for MLB Sentiment")
    parser.add_argument("--test", action="store_true", help="Test connection")
    args = parser.parse_args()

    if args.test:
        ok = send_test()
        print(f"Test: {'OK' if ok else 'FAILED'}")
        return

    if not BOT_TOKEN or not CHAT_ID:
        print("ERROR: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
        sys.exit(1)

    def signal_handler(sig, frame):
        STOP_EVENT.set()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    run_bot()


if __name__ == "__main__":
    main()
