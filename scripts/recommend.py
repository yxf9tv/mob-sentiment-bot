"""Generate betting recommendations from live consensus using backtest edge model.

Applies +EV rules derived from backtest_results.json to live market data,
producing a ranked betting slip. Run after poll_live.py to get fresh picks.
"""

import json, time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LIVE_PATH = DATA_DIR / "live_signals.json"
BACKTEST_PATH = DATA_DIR / "backtest_results.json"
OUTPUT_PATH = DATA_DIR / "bet_recommendations.json"

EDGE_RULES = [
    {
        "name": "ML High Volume",
        "action": "BET",
        "market_type": "moneyline",
        "min_volume": 1071,
        "min_traders": 1,
        "accuracy": 0.744,
        "roi": 48.8,
        "confidence": "high",
    },
    {
        "name": "Spread Mid Volume",
        "action": "BET",
        "market_type": "spread",
        "min_volume": 107,
        "min_traders": 1,
        "accuracy": 0.731,
        "roi": 46.2,
        "confidence": "high",
    },
    {
        "name": "ML Mid Volume",
        "action": "BET",
        "market_type": "moneyline",
        "min_volume": 107,
        "min_traders": 1,
        "accuracy": 0.640,
        "roi": 27.9,
        "confidence": "medium",
    },
    {
        "name": "Total (Fade)",
        "action": "FADE",
        "market_type": "total",
        "min_volume": 0,
        "min_traders": 1,
        "accuracy": 0.573,
        "roi": 14.6,
        "confidence": "medium",
    },
]


def classify_market(m):
    mt = m.get("market_type", "")
    vol = m.get("total_weighted_volume", 0)
    traders = m.get("unique_traders", 0)
    for rule in EDGE_RULES:
        if rule["market_type"] == mt and vol >= rule["min_volume"] and traders >= rule["min_traders"]:
            return rule
    return None


def main():
    if not LIVE_PATH.exists():
        print("No live data found")
        return

    with open(LIVE_PATH) as f:
        live = json.load(f)

    markets = live.get("live_consensus_all", [])
    markets_with_data = [m for m in markets if m.get("has_trader_data")]

    bets = []
    fades = []

    for m in markets_with_data:
        rule = classify_market(m)
        if rule is None:
            continue

        outcome = m.get("top_outcome", "?")
        rec = {
            "slug": m.get("slug", ""),
            "event_slug": m.get("event_slug", ""),
            "game_date": m.get("game_date", ""),
            "market_type": m.get("market_type", ""),
            "action": rule["action"],
            "strategy": rule["name"],
            "predicted_outcome": outcome,
            "conviction": m.get("conviction", 0),
            "volume": m.get("total_weighted_volume", 0),
            "traders": m.get("unique_traders", 0),
            "expected_accuracy": rule["accuracy"],
            "expected_roi": rule["roi"],
            "confidence": rule["confidence"],
        }

        if rule["action"] == "FADE":
            outcomes = m.get("outcomes", {})
            other = [k for k in outcomes.keys() if k != outcome]
            rec["fade_outcome"] = other[0] if other else "opposite"

        if rule["action"] == "BET":
            bets.append(rec)
        else:
            fades.append(rec)

    bets.sort(key=lambda r: -r["expected_roi"])
    fades.sort(key=lambda r: -r["expected_roi"])

    output = {
        "generated_at": live.get("generated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        "today": live.get("today", ""),
        "total_analyzed": len(markets_with_data),
        "bets": bets,
        "fades": fades,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Bet recommendations: {len(bets)} bets, {len(fades)} fades (from {len(markets_with_data)} markets)")


if __name__ == "__main__":
    main()
