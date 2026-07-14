"""MLB Sentiment Bot — automated Polymarket execution.

Reads live signals, filters by composite score, places trades via polyclob.

Usage:
    python3 scripts/bot.py              # dry run (POLY_LIVE=0)
    python3 scripts/bot.py --live       # real orders (POLY_LIVE=1)
    python3 scripts/bot.py --dry-run    # explicit dry run
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
load_dotenv(BASE / ".env")

# --- Polymarket execution ---
from polyclob import PolyConfig, make_client, place_order, dollar_floor_size, extract_book_data, preflight, print_report

# --- Our modules ---
sys.path.insert(0, str(BASE / "scripts"))
from composite import compute_composite, classify_confidence, load_trader_accuracy, get_trader_accuracy_modifier
from telegram import send_message, slug_to_readable
from telegram_bot import send_trade_alert

# --- Config ---
COMPOSITE_THRESHOLD = 0.40
MAX_BET_USD = float(os.getenv("BOT_MAX_BET", "100"))
BET_FRACTION = float(os.getenv("BOT_BET_FRACTION", "0.02"))  # 2% of bankroll
BANKROLL = float(os.getenv("BOT_BANKROLL", "10000"))
LIVE_SIGNALS_PATH = DATA_DIR / "live_signals.json"
TRADE_LOG_PATH = DATA_DIR / "trade_log.jsonl"
TOKEN_MAP_PATH = DATA_DIR / "token_map.json"


def load_live_signals():
    if not LIVE_SIGNALS_PATH.exists():
        print("No live signals found. Run poll_live.py first.")
        sys.exit(1)
    with open(LIVE_SIGNALS_PATH) as f:
        return json.load(f)


def load_token_map():
    """Load condition_id -> { outcome -> token_id } mapping."""
    if TOKEN_MAP_PATH.exists():
        with open(TOKEN_MAP_PATH) as f:
            return json.load(f)
    return {}


def save_token_map(token_map):
    with open(TOKEN_MAP_PATH, "w") as f:
        json.dump(token_map, f, indent=2)


def log_trade(trade):
    TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOG_PATH, "a") as f:
        f.write(json.dumps(trade, default=str) + "\n")


def compute_bet_size(bankroll, fraction, max_bet):
    """Kelly-inspired bet size: fraction of bankroll, capped at max."""
    raw = bankroll * fraction
    return min(raw, max_bet)


def resolve_token_id(client, condition_id, outcome, token_map):
    """Look up or fetch the token_id for a specific outcome of a market."""
    if condition_id in token_map and outcome in token_map[condition_id]:
        return token_map[condition_id][outcome]

    # Fetch from API
    try:
        book = client.get_order_book(condition_id)
        if book and "tokens" in book:
            for token in book["tokens"]:
                if token.get("outcome") == outcome:
                    tid = token.get("token_id", "")
                    if tid:
                        if condition_id not in token_map:
                            token_map[condition_id] = {}
                        token_map[condition_id][outcome] = tid
                        return tid
    except Exception as e:
        print(f"  [token lookup error: {e}]")

    return None


def execute_trade(client, token_id, outcome, price, size, tick_size, live, market_info):
    """Place an order and log the result."""
    receipt = place_order(client, token_id, price, size, tick_size, live=live)

    trade = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "slug": market_info.get("slug", ""),
        "condition_id": market_info.get("condition_id", ""),
        "outcome": outcome,
        "price": price,
        "size": size,
        "cost": round(price * size, 2),
        "composite_score": market_info.get("composite_score", 0),
        "confidence": market_info.get("confidence", ""),
        "live": live,
        "receipt": receipt,
    }
    log_trade(trade)
    return trade


def format_trade_alert(trade, market_info):
    """Format a trade alert for Telegram."""
    slug = market_info.get("slug", "")
    label = slug_to_readable(slug)
    market_type = market_info.get("market_type", "?")
    score = market_info.get("composite_score", 0)
    confidence = market_info.get("confidence", "")
    outcome = trade["outcome"]
    price = trade["price"]
    size = trade["size"]
    cost = trade["cost"]
    live = trade["live"]

    mode = "LIVE" if live else "DRY RUN"
    lines = [
        f"⚾ *{mode}* — {label}",
        f"`{market_type}` → *{outcome}*",
        "",
        f"Score:\t*{score:.2f}* ({confidence})",
        f"Price:\t{price:.2f}",
        f"Size:\t{size} shares",
        f"Cost:\t${cost:.2f}",
    ]
    return "\n".join(lines)


def run_bot(live=False, dry_run=False):
    """Main bot loop."""
    if dry_run:
        live = False

    # Load signals
    signals = load_live_signals()
    active = signals.get("live_consensus", [])
    print(f"Loaded {len(active)} consensus signals")

    # Filter by threshold
    qualified = [m for m in active if m.get("composite_score", 0) >= COMPOSITE_THRESHOLD]
    print(f"Qualified (score >= {COMPOSITE_THRESHOLD}): {len(qualified)}")

    if not qualified:
        print("No qualifying markets. Exiting.")
        return

    # Initialize Polymarket client
    try:
        cfg = PolyConfig.from_env(str(BASE / ".env"))
        if live:
            cfg = PolyConfig(
                pk=cfg.pk,
                funder=cfg.funder,
                sig_type=cfg.sig_type,
                live=True,
                host=cfg.host,
                chain_id=cfg.chain_id,
                api_key=cfg.api_key,
                api_secret=cfg.api_secret,
                api_passphrase=cfg.api_passphrase,
            )
        client = make_client(cfg)
        print(f"Polymarket client: {cfg}")
    except Exception as e:
        print(f"ERROR: Failed to initialize Polymarket client: {e}")
        print("Check your POLY_PK, POLY_FUNDER in .env")
        sys.exit(1)

    # Run preflight checks
    if live:
        print("\nRunning preflight checks...")
        result = preflight(cfg, cap_usd=MAX_BET_USD)
        print_report(result)
        if not result.all_ok:
            print("Preflight failed. Fix issues before going live.")
            sys.exit(1)

    # Load token map
    token_map = load_token_map()
    bet_size = compute_bet_size(BANKROLL, BET_FRACTION, MAX_BET_USD)
    print(f"\nBet size: ${bet_size:.2f} ({BET_FRACTION:.0%} of ${BANKROLL:,.0f} bankroll)")
    print(f"Mode: {'LIVE' if live else 'DRY RUN'}")
    print()

    trades = []
    for market in qualified:
        slug = market.get("slug", "")
        outcome = market.get("top_outcome", "")
        score = market.get("composite_score", 0)
        confidence = market.get("confidence", "")
        condition_id = market.get("condition_id", "")

        print(f"--- {slug_to_readable(slug)} ---")
        print(f"  Outcome: {outcome}, Score: {score:.2f}, Confidence: {confidence}")

        # Resolve token_id
        token_id = resolve_token_id(client, condition_id, outcome, token_map)
        if not token_id:
            print(f"  SKIP: Could not resolve token_id for {outcome}")
            continue

        # Fetch orderbook
        try:
            book = client.get_order_book(token_id)
            bd = extract_book_data(book)
            if not bd:
                print(f"  SKIP: No asks available")
                continue

            price = bd["best_ask"]
            tick_size = bd["tick_size"]
            min_order_size = bd["min_order_size"]
            ask_size = bd["ask_size"]

            # Size the order
            size = dollar_floor_size(price, int(ask_size), min_order_size)
            if size <= 0:
                print(f"  SKIP: Cannot size order (price={price}, ask={ask_size})")
                continue

            cost = price * size
            print(f"  Ask: {price:.2f}, Size: {size}, Cost: ${cost:.2f}")

            # Cap cost at bet_size
            if cost > bet_size:
                # Recalculate size to fit budget
                size = dollar_floor_size(price, int(bet_size / price), min_order_size)
                if size <= 0:
                    print(f"  SKIP: Cannot fit within budget")
                    continue
                cost = price * size
                print(f"  Capped: Size={size}, Cost=${cost:.2f}")

            # Place order
            trade = execute_trade(client, token_id, outcome, price, size, tick_size, live, market)
            trades.append(trade)

            # Send alert via telegram bot
            send_trade_alert(trade, market)
            print(f"  {'EXECUTED' if live else 'DRY RUN'}: {size} shares @ {price:.2f}")

        except Exception as e:
            print(f"  ERROR: {e}")

        time.sleep(0.5)  # Rate limit

    # Summary
    print(f"\n{'='*60}")
    print(f"Session complete: {len(trades)} trades")
    if trades:
        total_cost = sum(t["cost"] for t in trades)
        print(f"Total cost: ${total_cost:.2f}")
        if live:
            print("Orders placed on Polymarket!")
        else:
            print("DRY RUN — no real orders placed")

    save_token_map(token_map)


def main():
    parser = argparse.ArgumentParser(description="MLB Sentiment Bot")
    parser.add_argument("--live", action="store_true", help="Place real orders")
    parser.add_argument("--dry-run", action="store_true", help="Force dry run")
    args = parser.parse_args()

    run_bot(live=args.live, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
