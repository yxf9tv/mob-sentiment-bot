"""
Backtest pre-game trader sentiment vs full-data sentiment.

Re-fetches historical trade data for all known wallets, computes sentiment
using only trades before game start (GAME_START_HOUR_UTC on game date),
compares accuracy against existing full-data backtest.

Helps answer: does the signal just before game start differ from the
signal using ALL trades (which may include post-game data leakage)?

Output -> data/backtest_pregame_results.json
Cache  -> data/cache_pregame/ (separate from live cache)
"""

import json, time, os, sys, re, datetime
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PREGAME_CACHE_DIR = DATA_DIR / "cache_pregame"
OUTPUT_PATH = DATA_DIR / "backtest_pregame_results.json"
SENTIMENT_PATH = DATA_DIR / "sentiment_scores.json"
TRADER_PATH = DATA_DIR / "top_mlb_traders.json"

API_URL = "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"
AGENT_TRADES = 556
AGENT_MARKETS = 574

# Game start heuristic: 19:00 ET = 23:00 UTC (typical MLB night game)
GAME_START_HOUR_UTC = 23
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})$")
SLUG_RE = re.compile(
    r"^mlb-(?P<team1>[a-z]+)-(?P<team2>[a-z]+)-(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:-(?P<type>total|spread|nrfi))?"
    r"(?:-(?P<side>home|away))?"
    r"(?:-(?P<line>[^-]+))?"
    r"(?:-(?P<line2>[^-]+))?"
    r"$"
)


def load_env_key():
    load_dotenv()
    key = os.getenv("intelligence_api_key")
    if not key:
        print("ERROR: intelligence_api_key not found in .env", file=sys.stderr)
        sys.exit(1)
    return key


def api_call(agent_id, params, pagination=None, api_key=None, retries=3):
    if api_key is None:
        api_key = load_env_key()
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
            resp = requests.post(API_URL, json=body, headers=headers, timeout=60)
            if resp.status_code == 429:
                wait = (2 ** attempt) * 5
                print(f"  [rate-limited, retry {wait}s]")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = (2 ** attempt) * 3
                print(f"  [error: {e}, retry {wait}s]")
                time.sleep(wait)
            else:
                raise


def paginated_call(agent_id, params, api_key, limit=200, max_pages=5):
    all_results = []
    offset = 0
    for page in range(max_pages):
        resp = api_call(agent_id, params, {"limit": limit, "offset": offset}, api_key)
        results = resp.get("data", {}).get("results", [])
        if not results:
            break
        all_results.extend(results)
        if not resp.get("pagination", {}).get("has_more", False):
            break
        offset += limit
    return all_results


def fetch_historical_trades(wallet, start_unix, api_key):
    """Fetch all MLB trades for a wallet from start_unix to now."""
    params = {
        "proxy_wallet": wallet,
        "condition_id": "ALL",
        "start_time": str(start_unix),
    }
    return paginated_call(AGENT_TRADES, params, api_key, limit=200, max_pages=5)


def parse_slug(slug):
    """Extract structured info from an MLB slug."""
    if slug.startswith("will-") or slug.startswith("1-mlb-") or slug.startswith("2-mlb-") or slug.startswith("3-mlb-"):
        return {"event_slug": "futures-props", "market_type": "futures", "teams": None, "date": None}
    m = SLUG_RE.match(slug)
    if not m:
        return {"event_slug": "other", "market_type": "other", "teams": None, "date": None}
    g = m.groupdict()
    event_slug = f"mlb-{g['team1']}-{g['team2']}-{g['date']}"
    market_type = g.get("type") or "moneyline"
    return {
        "event_slug": event_slug,
        "market_type": market_type,
        "teams": (g["team1"], g["team2"]),
        "date": g["date"],
        "side": g.get("side"),
        "line": g.get("line"),
    }


def load_traders():
    """Load traders with weights (same logic as compute_sentiment.py)."""
    with open(TRADER_PATH) as f:
        data = json.load(f)
    all_traders = data["top_tier"] + data["watchlist"]

    # Filter out MM/HFT
    def is_mm(t):
        flags = set(t.get("behavioral_flags") or [])
        return "timing_anomaly" in flags or "sybil_risk" in flags

    clean = [t for t in all_traders if not is_mm(t)]
    print(f"  Clean traders: {len(clean)}/{len(all_traders)} (excluded {len(all_traders)-len(clean)} MM/HFT)")

    max_pnl = max((t["baseball_pnl_15d"] for t in clean if t["baseball_pnl_15d"] > 0), default=1)
    for t in clean:
        pnl = max(t["baseball_pnl_15d"], 0)
        wr = t["win_rate"] or 0.5
        sharpe = max(t["sharpe_ratio"] or 0, 0)
        human = (t["human_likeness_score"] or 50) / 100.0
        pnl_w = pnl / max_pnl
        wr_w = max(wr - 0.5, 0) * 2
        sharpe_w = min(sharpe / 2.0, 1.0)
        weight = 0.5 * pnl_w + 0.2 * wr_w + 0.15 * sharpe_w + 0.15 * human
        t["_weight"] = round(weight, 4)
    return {t["wallet"]: t for t in clean}


def get_game_date_from_slug(slug):
    m = DATE_RE.search(slug)
    return m.group(1) if m else None


def get_game_start_utc(game_date_str):
    """Return UTC timestamp for game start (GAME_START_HOUR_UTC on game_date)."""
    dt = datetime.datetime.strptime(game_date_str, "%Y-%m-%d")
    dt = dt.replace(hour=GAME_START_HOUR_UTC, minute=0, second=0, tzinfo=datetime.timezone.utc)
    return dt.isoformat()


def compute_pre_game_sentiment(cached_trades, trader_idx, game_start_iso):
    """Compute sentiment using only trades before game_start_iso."""
    pre_trades = [t for t in cached_trades if t.get("timestamp", "") < game_start_iso]
    full_trades = cached_trades
    return compute_sentiment_from_trades(pre_trades, trader_idx), compute_sentiment_from_trades(full_trades, trader_idx)


def compute_sentiment_from_trades(trades, trader_idx):
    """Compute sentiment for a list of trades (same logic as compute_sentiment.py)."""
    if not trades:
        return None

    outcomes = defaultdict(lambda: {"weighted_volume": 0.0, "trader_count": 0, "trader_set": set(), "trades": []})

    for tr in trades:
        t = trader_idx.get(tr.get("wallet", ""))
        if not t:
            continue
        weight = t["_weight"]
        notional = (tr.get("size", 0) or 0) * (tr.get("price", 0) or 0)
        signal = notional * weight
        if tr.get("side") == "SELL":
            signal = -signal
        o = outcomes[tr.get("outcome", "?")]
        o["weighted_volume"] += signal
        o["trader_count"] += 1
        o["trader_set"].add(tr["wallet"])
        o["trades"].append(tr)

    if not outcomes:
        return None

    total_weighted = sum(o["weighted_volume"] for o in outcomes.values())
    if total_weighted == 0:
        return None

    sorted_outcomes = sorted(outcomes.items(), key=lambda x: -abs(x[1]["weighted_volume"]))
    top_outcome, top_data = sorted_outcomes[0]
    top_fraction = top_data["weighted_volume"] / total_weighted if total_weighted else 0

    if len(sorted_outcomes) >= 2:
        second_fraction = abs(sorted_outcomes[1][1]["weighted_volume"]) / total_weighted
        conviction = abs(top_fraction - second_fraction) / max(top_fraction, second_fraction) if max(top_fraction, second_fraction) > 0 else 0
    else:
        conviction = 1.0

    all_traders = set()
    for o in outcomes.values():
        all_traders.update(o["trader_set"])

    timestamps = [tr.get("timestamp", "") for tr in trades if tr.get("timestamp")]
    timestamps.sort()
    first_date = timestamps[0][:10] if timestamps else ""
    last_date = timestamps[-1][:10] if timestamps else ""

    return {
        "top_outcome": top_outcome,
        "top_weighted_fraction": round(abs(top_fraction), 4),
        "conviction": round(abs(conviction), 4),
        "total_weighted_volume": round(total_weighted, 2),
        "unique_traders": len(all_traders),
        "total_trade_events": len(trades),
        "first_trade_date": first_date,
        "last_trade_date": last_date,
        "outcomes": {
            oc: {
                "weighted_volume": round(od["weighted_volume"], 2),
                "trader_count": len(od["trader_set"]),
                "trade_count": len(od["trades"]),
            }
            for oc, od in sorted_outcomes
        },
    }


def query_resolved_outcome(condition_id, api_key):
    body = {
        "agent_id": AGENT_MARKETS,
        "params": {"condition_id": condition_id, "closed": "True"},
        "formatter_config": {"format_type": "raw"},
    }
    try:
        resp = requests.post(
            API_URL, json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        results = (resp.json().get("data") or {}).get("results") or []
        if results:
            return results[0].get("winning_outcome")
    except Exception:
        pass
    return None


def main():
    api_key = load_env_key()
    print("\n=== PRE-GAME BACKTEST ===\n")

    # 1. Load sentiment data
    print("Loading sentiment data...")
    with open(SENTIMENT_PATH) as f:
        sentiment = json.load(f)
    markets = sentiment["by_market"]
    summary = sentiment["summary"]
    data_start = summary["data_start_date"]  # 2026-04-03
    data_end = summary["data_end_date"]      # 2026-06-29
    print(f"  {len(markets)} markets, {summary['data_start_date']} to {summary['data_end_date']}")

    # 2. Load traders
    print("Loading traders...")
    trader_idx = load_traders()
    print(f"  {len(trader_idx)} traders loaded")

    # 3. Fetch historical trades for all traders
    print("\nFetching historical trades...")
    start_dt = datetime.datetime.strptime(data_start, "%Y-%m-%d")
    start_unix = int(start_dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    
    PREGAME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wallets_fetched = 0
    total_mlb_trades = 0
    
    for i, (wallet, tinfo) in enumerate(trader_idx.items()):
        cache_path = PREGAME_CACHE_DIR / f"mtrades_{wallet}.json"
        if cache_path.exists():
            with open(cache_path) as f:
                cached = json.load(f)
            n = len(cached.get("mlb_trade_details", []))
            total_mlb_trades += n
            if i < 3 or (i+1) % 20 == 0:
                print(f"  [{i+1}/{len(trader_idx)}] {wallet[:6]}...{wallet[-4:]} (cached, {n} trades)")
            continue
        
        try:
            all_trades = fetch_historical_trades(wallet, start_unix, api_key)
            mlb_trades = [t for t in all_trades if "mlb" in t.get("slug", "").lower()]
            with open(cache_path, "w") as f:
                json.dump({
                    "wallet": wallet,
                    "total_trades_90d": len(all_trades),
                    "mlb_trades_count": len(mlb_trades),
                    "mlb_trade_details": mlb_trades,
                    "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }, f, default=str)
            total_mlb_trades += len(mlb_trades)
            wallets_fetched += 1
            print(f"  [{i+1}/{len(trader_idx)}] {wallet[:6]}...{wallet[-4:]} -> {len(mlb_trades)} MLB / {len(all_trades)} total")
            time.sleep(0.3)
        except Exception as e:
            print(f"  [{i+1}/{len(trader_idx)}] {wallet[:6]}...{wallet[-4:]} ERROR: {e}")
    
    print(f"\n  Wallets fetched: {wallets_fetched}, Total MLB trades cached: {total_mlb_trades}")

    # 4. Load all pre-game cached trades into by-cid index
    print("\nLoading cached historical trades...")
    trades_by_cid = defaultdict(list)
    for fname in os.listdir(PREGAME_CACHE_DIR):
        if not fname.startswith("mtrades_"):
            continue
        with open(PREGAME_CACHE_DIR / fname) as f:
            cached = json.load(f)
        wallet = cached["wallet"]
        for d in cached.get("mlb_trade_details", []):
            cid = d.get("condition_id", "")
            if cid:
                d["wallet"] = wallet
                trades_by_cid[cid].append(d)
    print(f"  {sum(len(v) for v in trades_by_cid.values())} trades across {len(trades_by_cid)} markets")

    # 5. For each market, compute pre-game vs full sentiment
    print("\nComputing pre-game vs full sentiment...")
    results = []
    overall_outcome_cache = {}  # reuse existing outcome cache

    for i, m in enumerate(markets):
        cid = m.get("condition_id", "")
        slug = m.get("slug", "")
        game_date = get_game_date_from_slug(slug) or m.get("first_trade_date", "")
        market_type = m.get("market_type", "other")

        trades = trades_by_cid.get(cid, [])
        if not trades:
            continue

        # Get game start time
        game_start_iso = get_game_start_utc(game_date) if game_date else ""

        # Pre-game sentiment
        pre_trades = [t for t in trades if game_start_iso and t.get("timestamp", "") < game_start_iso]
        pre_sent = compute_sentiment_from_trades(pre_trades, trader_idx)

        # Full sentiment (all trades)
        full_sent = compute_sentiment_from_trades(trades, trader_idx)

        if not pre_sent and not full_sent:
            continue

        # Resolve actual outcome
        winning = overall_outcome_cache.get(cid)
        if winning is None:
            winning = query_resolved_outcome(cid, api_key)
            if winning:
                overall_outcome_cache[cid] = winning
                time.sleep(0.3)

        pre_correct = None
        full_correct = None
        if winning:
            if pre_sent:
                pre_correct = (pre_sent["top_outcome"] == winning)
            if full_sent:
                full_correct = (full_sent["top_outcome"] == winning)

        result = {
            "condition_id": cid,
            "slug": slug,
            "market_type": market_type,
            "game_date": game_date,
            "game_start_utc": game_start_iso,
            "total_trades": len(trades),
            "pre_game_trades": len(pre_trades),
            "actual_outcome": winning,
            "pre_game": pre_sent,
            "full_data": full_sent,
            "pre_game_correct": pre_correct,
            "full_data_correct": full_correct,
        }
        results.append(result)

        if (i+1) % 50 == 0:
            print(f"  [{i+1}/{len(markets)}] processed, {len(results)} results so far")

    # 6. Compute comparison stats
    print("\n=== COMPARISON RESULTS ===\n")

    resolved_pre = [r for r in results if r["pre_game_correct"] is not None]
    resolved_full = [r for r in results if r["full_data_correct"] is not None]

    pre_correct = sum(1 for r in resolved_pre if r["pre_game_correct"])
    full_correct = sum(1 for r in resolved_full if r["full_data_correct"])

    print(f"Markets with pre-game data: {len(resolved_pre)}/{len(results)}")
    print(f"Markets with full data:     {len(resolved_full)}/{len(results)}")
    print(f"")
    print(f"Pre-game accuracy:  {pre_correct}/{len(resolved_pre)} = {pre_correct/len(resolved_pre):.1%}" if resolved_pre else "Pre-game: N/A")
    print(f"Full-data accuracy: {full_correct}/{len(resolved_full)} = {full_correct/len(resolved_full):.1%}" if resolved_full else "Full-data: N/A")

    # Agreement analysis
    both_resolved = [r for r in results if r["pre_game_correct"] is not None and r["full_data_correct"] is not None]
    same_prediction = sum(1 for r in both_resolved if r.get("pre_game") and r.get("full_data") and r["pre_game"]["top_outcome"] == r["full_data"]["top_outcome"])
    same_outcome = sum(1 for r in both_resolved if r["pre_game_correct"] == r["full_data_correct"])

    print(f"\nBoth resolved:     {len(both_resolved)}")
    print(f"Same prediction:   {same_prediction}/{len(both_resolved)} = {same_prediction/len(both_resolved):.1%}")
    print(f"Same outcome:      {same_outcome}/{len(both_resolved)} = {same_outcome/len(both_resolved):.1%}")

    # By market type
    print("\nBy market type:")
    types = set(r["market_type"] for r in results)
    for mt in sorted(types):
        subset = [r for r in results if r["market_type"] == mt]
        pre_res = [r for r in subset if r["pre_game_correct"] is not None]
        full_res = [r for r in subset if r["full_data_correct"] is not None]
        pre_acc = sum(1 for r in pre_res if r["pre_game_correct"]) / len(pre_res) if pre_res else 0
        full_acc = sum(1 for r in full_res if r["full_data_correct"]) / len(full_res) if full_res else 0
        changed = sum(1 for r in subset if r.get("pre_game") and r.get("full_data") and r["pre_game"]["top_outcome"] != r["full_data"]["top_outcome"])
        print(f"  {mt:15s}: pre={pre_acc:.1%} ({len(pre_res)}) full={full_acc:.1%} ({len(full_res)}) changed={changed}")

    # 7. Save output
    output = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "data_range": {"start": data_start, "end": data_end},
        "game_start_heuristic_utc": f"{GAME_START_HOUR_UTC}:00",
        "summary": {
            "total_markets": len(markets),
            "results": len(results),
            "resolved_pre_game": len(resolved_pre),
            "resolved_full_data": len(resolved_full),
            "pre_game_accuracy": round(pre_correct / len(resolved_pre), 4) if resolved_pre else 0,
            "full_data_accuracy": round(full_correct / len(resolved_full), 4) if resolved_full else 0,
            "pre_game_correct": pre_correct,
            "full_data_correct": full_correct,
            "same_prediction_pct": round(same_prediction / len(both_resolved) * 100, 1) if both_resolved else 0,
            "change_count": sum(1 for r in results if r.get("pre_game") and r.get("full_data") and r["pre_game"]["top_outcome"] != r["full_data"]["top_outcome"]),
        },
        "by_market_type": {},
        "results": results,
    }

    # Fill by_market_type
    for mt in sorted(types):
        subset = [r for r in results if r["market_type"] == mt]
        pre_res = [r for r in subset if r["pre_game_correct"] is not None]
        full_res = [r for r in subset if r["full_data_correct"] is not None]
        output["by_market_type"][mt] = {
            "total": len(subset),
            "pre_game": {
                "resolved": len(pre_res),
                "correct": sum(1 for r in pre_res if r["pre_game_correct"]),
                "accuracy": round(sum(1 for r in pre_res if r["pre_game_correct"]) / len(pre_res), 4) if pre_res else 0,
            },
            "full_data": {
                "resolved": len(full_res),
                "correct": sum(1 for r in full_res if r["full_data_correct"]),
                "accuracy": round(sum(1 for r in full_res if r["full_data_correct"]) / len(full_res), 4) if full_res else 0,
            },
            "predictions_changed": sum(1 for r in subset if r.get("pre_game") and r.get("full_data") and r["pre_game"]["top_outcome"] != r["full_data"]["top_outcome"]),
        }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
