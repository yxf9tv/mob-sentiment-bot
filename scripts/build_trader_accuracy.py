"""Build per-trader per-market-type accuracy database.

Fetches 30 days of trade history from the API for all known traders,
cross-references with resolved market outcomes, and computes accuracy
stats for each trader on each market type.

Output: data/trader_accuracy.json

Usage:
    python3 scripts/build_trader_accuracy.py
"""

import json, os, sys, re, time
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "cache"
OUTCOME_CACHE = CACHE_DIR / "outcome_cache.json"
OUT_PATH = DATA_DIR / "trader_accuracy.json"
TRADE_CACHE_DIR = CACHE_DIR / "trades_long"

MIN_TRADES = 5
LOOKBACK_DAYS = 30

API_URL = "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"
AGENT_TRADES = 556

SLUG_RE = re.compile(
    r"^mlb-(?P<team1>[a-z]+)-(?P<team2>[a-z]+)-(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:-(?P<type>total|spread|nrfi))?"
    r"(?:-(?P<side>home|away))?"
    r"(?:-(?P<line>[^-]+))?"
    r"(?:-(?P<line2>[^-]+))?"
    r"$"
)

MLB_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"mlb|baseball|world\s*series|"
    r"yankees|red\s*sox|dodgers|astros|braves|"
    r"brewers|cardinals|phillies|padres|giants|"
    r"blue\s*jays|orioles|rays|mariners|twins|"
    r"guardians|tigers|royals|athletics|rangers|"
    r"angels|white\s*sox|cubs|reds|pirates|"
    r"rockies|diamondbacks|marlins|nationals|"
    r"ohtani|judge|acuna"
    r")\b",
    re.IGNORECASE,
)


def load_env_key():
    load_dotenv()
    key = os.getenv("intelligence_api_key")
    if not key:
        print("ERROR: intelligence_api_key not found in .env", file=sys.stderr)
        sys.exit(1)
    print(f"API key loaded: {key[:10]}...")
    return key


def api_call(agent_id, params, pagination=None, api_key=None, retries=3):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "agent_id": agent_id,
        "params": params,
        "formatter_config": {"format_type": "raw"},
    }
    if pagination:
        body["pagination"] = pagination
    for attempt in range(retries):
        try:
            print(f"    [debug] agent_id={agent_id}, params={json.dumps(params)[:100]}...")
            resp = requests.post(API_URL, json=body, headers=headers, timeout=60)
            print(f"    [debug] status={resp.status_code}")
            if resp.status_code == 429:
                wait = (2 ** attempt) * 5
                print(f"  [rate-limited, retry {wait}s]")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"    [debug] error: {e}")
            if attempt < retries - 1:
                wait = (2 ** attempt) * 3
                print(f"  [error: {e}, retry {wait}s]")
                time.sleep(wait)
            else:
                raise


def parse_slug(slug):
    if slug.startswith("will-") or slug.startswith("1-mlb-") or slug.startswith("2-mlb-") or slug.startswith("3-mlb-"):
        return "futures"
    m = SLUG_RE.match(slug)
    if not m:
        return "other"
    return m.group("type") or "moneyline"


def load_outcome_cache():
    if OUTCOME_CACHE.exists():
        with open(OUTCOME_CACHE) as f:
            return json.load(f)
    return {}


def load_trader_list():
    path = DATA_DIR / "top_mlb_traders.json"
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    traders = data.get("top_tier", []) + data.get("watchlist", [])

    # Filter out market makers
    clean = []
    for t in traders:
        flags = set(t.get("behavioral_flags") or [])
        if "timing_anomaly" not in flags and "sybil_risk" not in flags:
            clean.append(t)

    clean.sort(key=lambda t: -(t.get("baseball_pnl_15d") or 0))
    print(f"Loaded {len(clean)} clean traders")
    return clean


def fetch_trades_30d(trader_wallets, api_key):
    """Fetch 30 days of trades for all traders. Caches results."""
    TRADE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    thirty_days_ago = int(time.time()) - (LOOKBACK_DAYS * 86400)

    all_trades = {}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    for i, wallet in enumerate(trader_wallets):
        cache_path = TRADE_CACHE_DIR / f"trades_{wallet}.json"
        if cache_path.exists():
            with open(cache_path) as f:
                cached = json.load(f)
            if cached.get("fetched_at", 0) > thirty_days_ago:
                all_trades[wallet] = cached.get("trades", [])
                continue

        print(f"  [{i+1}/{len(trader_wallets)}] {wallet[:10]}...", end=" ", flush=True)
        body = {
            "agent_id": AGENT_TRADES,
            "params": {
                "proxy_wallet": wallet,
                "condition_id": "ALL",
                "start_time": str(thirty_days_ago),
            },
            "pagination": {"limit": 200, "offset": 0},
            "formatter_config": {"format_type": "raw"},
        }
        try:
            resp = requests.post(API_URL, json=body, headers=headers, timeout=60)
            if resp.status_code == 429:
                print(f"rate-limited, waiting 10s...")
                time.sleep(10)
                resp = requests.post(API_URL, json=body, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            trades = data.get("data", {}).get("results", [])
            all_trades[wallet] = trades

            with open(cache_path, "w") as f:
                json.dump({
                    "wallet": wallet,
                    "trades": trades,
                    "fetched_at": int(time.time()),
                }, f, default=str)
            print(f"{len(trades)} trades")
        except Exception as e:
            print(f"error: {e}")
            all_trades[wallet] = []
        
        time.sleep(0.5)  # Rate limit protection

    return all_trades


def determine_trade_correct(trade, outcome_cache):
    """Determine if a trade was correct based on side and outcome resolution."""
    cid = trade.get("condition_id", "")
    outcome = trade.get("outcome", "")
    side = trade.get("side", "")
    slug = trade.get("slug", "")

    if not cid or cid not in outcome_cache:
        return None

    winning = outcome_cache[cid].get("winning_outcome", "")
    if not winning:
        return None

    if side == "BUY":
        return outcome == winning
    elif side == "SELL":
        return outcome != winning

    return None


def build_accuracy():
    outcome_cache = load_outcome_cache()
    print(f"Loaded {len(outcome_cache)} resolved markets from outcome cache")

    traders = load_trader_list()
    trader_wallets = [t["wallet"] for t in traders]

    api_key = load_env_key()
    print(f"\nFetching {LOOKBACK_DAYS}-day trade history...")
    all_trades = fetch_trades_30d(trader_wallets, api_key)

    # Collect all trades per trader per market type
    trader_stats = defaultdict(lambda: defaultdict(lambda: {"trades": 0, "wins": 0, "notionals": []}))

    total_mlb = 0
    total_resolved = 0
    for wallet, trades in all_trades.items():
        for trade in trades:
            slug = trade.get("slug", "")
            if not slug:
                continue
            if not (MLB_KEYWORDS_RE.search(slug) or slug.startswith("mlb-")):
                continue

            market_type = parse_slug(slug)
            if market_type in ("other", "futures"):
                continue

            total_mlb += 1
            correct = determine_trade_correct(trade, outcome_cache)
            notional = (trade.get("size", 0) or 0) * (trade.get("price", 0) or 0)

            stats = trader_stats[wallet][market_type]
            if correct is not None:
                total_resolved += 1
                stats["trades"] += 1
                if correct is True:
                    stats["wins"] += 1
            stats["notionals"].append(notional)

    print(f"\nMLB trades found: {total_mlb}")
    print(f"Trades with resolved outcomes: {total_resolved}")

    # Build output
    result = {}
    traders_with_data = 0
    traders_with_enough = 0

    for wallet, market_types in trader_stats.items():
        result[wallet] = {}
        has_enough = False

        for mt, stats in market_types.items():
            trades = stats["trades"]
            wins = stats["wins"]
            notionals = stats["notionals"]
            avg_notional = sum(notionals) / len(notionals) if notionals else 0

            entry = {
                "trades": trades,
                "wins": wins,
                "accuracy": round(wins / trades, 4) if trades > 0 else 0,
                "avg_notional": round(avg_notional, 2),
            }
            if trades >= MIN_TRADES:
                entry["reliable"] = True
                has_enough = True
            else:
                entry["reliable"] = False

            result[wallet][mt] = entry

        traders_with_data += 1
        if has_enough:
            traders_with_enough += 1

    # Summary
    print(f"\nTraders with trade data: {traders_with_data}")
    print(f"Traders with {MIN_TRADES}+ trades on at least one market type: {traders_with_enough}")

    # Show accuracy breakdown
    print(f"\n=== ACCURACY BY MARKET TYPE ({MIN_TRADES}+ trades only) ===")
    for mt in ["moneyline", "spread", "total"]:
        entries = []
        for wallet, mts in result.items():
            if mt in mts and mts[mt].get("reliable"):
                entries.append(mts[mt])
        if entries:
            avg_acc = sum(e["accuracy"] for e in entries) / len(entries)
            print(f"  {mt}: {len(entries)} traders, avg accuracy: {avg_acc:.1%}")
            for e in sorted(entries, key=lambda x: -x["accuracy"])[:5]:
                print(f"    {e['accuracy']*100:.0f}% ({e['wins']}/{e['trades']})")

    # Save
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nSaved to {OUT_PATH}")
    return result


if __name__ == "__main__":
    build_accuracy()
