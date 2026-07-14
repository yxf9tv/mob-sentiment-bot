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

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# --- State ---
BOT_RUNNING = False
BOT_THREAD = None
STOP_EVENT = threading.Event()

# --- File paths ---
STATE_PATH = DATA_DIR / "bot_state.json"
TRADE_LOG_PATH = DATA_DIR / "trade_log.jsonl"
LIVE_SIGNALS_PATH = DATA_DIR / "live_signals.json"
TRIGGER_EVENTS_PATH = DATA_DIR / "trigger_events.jsonl"


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


def answer_callback(callback_query_id, text=None):
    """Acknowledge a callback query."""
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return api("answerCallbackQuery", **payload)


# --- Menu builders ---

def main_menu_keyboard():
    """Build the main menu inline keyboard."""
    state = load_state()
    status = "START" if not state.get("running") else "STOP"
    status_emoji = "🟢" if state.get("running") else "🔴"

    return {
        "inline_keyboard": [
            [
                {"text": f"{status_emoji} {status} Bot", "callback_data": f"bot_{status.lower()}"},
                {"text": "📊 Dashboard", "callback_data": "dashboard"},
            ],
            [
                {"text": "📈 Metrics", "callback_data": "metrics"},
                {"text": "📋 Trades", "callback_data": "trades"},
            ],
            [
                {"text": "🔗 Polymarket Profile", "url": f"https://polymarket.com/profile/{POLY_FUNDER}" if POLY_FUNDER else "https://polymarket.com"},
            ],
        ]
    }


def build_main_menu():
    """Send or edit to main menu."""
    state = load_state()
    status = "RUNNING" if state.get("running") else "STOPPED"
    uptime = ""
    if state.get("started_at"):
        started = datetime.fromisoformat(state["started_at"])
        delta = datetime.now(timezone.utc) - started
        hours = delta.total_seconds() / 3600
        uptime = f"\n⏱ Uptime: {hours:.1f}h"

    text = (
        f"⚾ *MLB Sentiment Bot*\n"
        f"Status: *{status}*{uptime}\n"
        f"Trades: {state.get('trades', 0)}\n"
        f"PnL: ${state.get('pnl', 0):+.2f}\n"
        f"\n_Select an option:_"
    )
    return text, main_menu_keyboard()


def get_metrics():
    """Compute current metrics from trade log and signals."""
    metrics = {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "pending": 0,
        "total_cost": 0,
        "total_return": 0,
        "pnl": 0,
        "accuracy": 0,
        "avg_score": 0,
        "markets_seen": 0,
    }

    if TRADE_LOG_PATH.exists():
        trades = []
        with open(TRADE_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))

        metrics["total_trades"] = len(trades)
        if trades:
            scores = [t.get("composite_score", 0) for t in trades]
            metrics["avg_score"] = sum(scores) / len(scores) if scores else 0
            metrics["total_cost"] = sum(t.get("cost", 0) for t in trades)

    if LIVE_SIGNALS_PATH.exists():
        with open(LIVE_SIGNALS_PATH) as f:
            signals = json.load(f)
        metrics["markets_seen"] = len(signals.get("live_consensus", []))

    if TRIGGER_EVENTS_PATH.exists():
        events = []
        with open(TRIGGER_EVENTS_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        metrics["signals_fired"] = len(events)

    return metrics


def build_dashboard():
    """Build dashboard view."""
    state = load_state()
    metrics = get_metrics()
    status = "🟢 RUNNING" if state.get("running") else "🔴 STOPPED"

    text = (
        f"📊 *Dashboard*\n"
        f"Status: {status}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Markets tracked: {metrics['markets_seen']}\n"
        f"Signals fired: {metrics.get('signals_fired', 0)}\n"
        f"Trades placed: {metrics['total_trades']}\n"
        f"Avg score: {metrics['avg_score']:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Total cost: ${metrics['total_cost']:.2f}\n"
        f"PnL: ${state.get('pnl', 0):+.2f}\n"
    )
    return text, main_menu_keyboard()


def build_metrics():
    """Build detailed metrics view."""
    metrics = get_metrics()

    text = (
        f"📈 *Performance Metrics*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Total trades: {metrics['total_trades']}\n"
        f"Wins: {metrics['wins']}\n"
        f"Losses: {metrics['losses']}\n"
        f"Pending: {metrics['pending']}\n"
        f"Accuracy: {metrics['accuracy']:.1%}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Total cost: ${metrics['total_cost']:.2f}\n"
        f"Total return: ${metrics['total_return']:.2f}\n"
        f"Net PnL: ${metrics['pnl']:+.2f}\n"
        f"ROI: {metrics['pnl']/metrics['total_cost']*100 if metrics['total_cost'] > 0 else 0:.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Avg composite score: {metrics['avg_score']:.2f}\n"
    )
    return text, main_menu_keyboard()


def build_trades():
    """Build recent trades view."""
    trades = []
    if TRADE_LOG_PATH.exists():
        with open(TRADE_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))

    if not trades:
        text = "📋 *No trades yet*\n\nTrades will appear here once the bot starts trading."
    else:
        recent = trades[-5:]  # Last 5 trades
        lines = ["📋 *Recent Trades*\n"]
        for t in reversed(recent):
            slug = t.get("slug", "?")
            outcome = t.get("outcome", "?")
            price = t.get("price", 0)
            size = t.get("size", 0)
            cost = t.get("cost", 0)
            score = t.get("composite_score", 0)
            ts = t.get("ts", "")[:16]
            lines.append(f"• `{ts}` {slug[:30]}")
            lines.append(f"  → {outcome} @ {price:.2f} × {size} = ${cost:.2f}")
            lines.append(f"  Score: {score:.2f}")
            lines.append("")

        lines.append(f"_Total: {len(trades)} trades_")
        text = "\n".join(lines)

    return text, main_menu_keyboard()


# --- Callback handlers ---

def handle_start_bot(callback_query_id):
    """Start the bot."""
    global BOT_RUNNING
    state = load_state()
    state["running"] = True
    state["started_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    BOT_RUNNING = True

    answer_callback(callback_query_id, "✅ Bot started")
    text, kb = build_main_menu()
    return text, kb


def handle_stop_bot(callback_query_id):
    """Stop the bot."""
    global BOT_RUNNING
    state = load_state()
    state["running"] = False
    state["stopped_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    BOT_RUNNING = False
    STOP_EVENT.set()

    answer_callback(callback_query_id, "⏹ Bot stopped")
    text, kb = build_main_menu()
    return text, kb


HANDLERS = {
    "bot_start": handle_start_bot,
    "bot_stop": handle_stop_bot,
    "dashboard": lambda cq: build_dashboard(),
    "metrics": lambda cq: build_metrics(),
    "trades": lambda cq: build_trades(),
}


def handle_callback(update):
    """Handle a callback query (button press)."""
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
        answer_callback(callback_id, f"Unknown action: {data}")


def handle_message(update):
    """Handle a text message."""
    msg = update.get("message", {})
    text = msg.get("text", "").lower()

    if text in ("/start", "/menu", "menu"):
        menu_text, kb = build_main_menu()
        send_message(menu_text, reply_markup=kb)
    elif text in ("/status", "status"):
        state = load_state()
        status = "RUNNING" if state.get("running") else "STOPPED"
        send_message(f"Bot status: *{status}*")
    elif text in ("/metrics", "metrics"):
        menu_text, kb = build_metrics()
        send_message(menu_text, reply_markup=kb)
    elif text in ("/trades", "trades"):
        menu_text, kb = build_trades()
        send_message(menu_text, reply_markup=kb)
    elif text in ("/help", "help"):
        send_message(
            "⚾ *MLB Sentiment Bot Commands*\n\n"
            "/menu — Main menu with buttons\n"
            "/status — Bot status\n"
            "/metrics — Performance metrics\n"
            "/trades — Recent trades\n"
            "/help — This message"
        )


def poll_updates(offset=0):
    """Long-poll for updates."""
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
    """Main polling loop."""
    print("Telegram bot started. Listening for commands...")
    offset = 0

    while not STOP_EVENT.is_set():
        data = poll_updates(offset)
        if not data.get("ok"):
            time.sleep(5)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1

            if "callback_query" in update:
                handle_callback(update)
            elif "message" in update:
                handle_message(update)

    print("Bot stopped.")


def send_trade_alert(trade, market_info):
    """Send a trade execution alert."""
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
        f"→ *{outcome}*\n"
        f"\n"
        f"Price:\t{price:.2f}\n"
        f"Size:\t{size} shares\n"
        f"Cost:\t${cost:.2f}\n"
        f"Score:\t{score:.2f} ({confidence})\n"
    )
    send_message(text)


def send_test():
    """Send a test message."""
    return send_message("✅ *MLB Sentiment Bot — connection successful*")


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

    # Handle graceful shutdown
    def signal_handler(sig, frame):
        STOP_EVENT.set()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    run_bot()


if __name__ == "__main__":
    main()
