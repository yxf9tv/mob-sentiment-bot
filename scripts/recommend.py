"""Generate betting recommendations from live consensus using composite score.

Ranks markets by composite score and classifies by confidence level.
Run after poll_live.py to get fresh picks.
"""

import json, time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LIVE_PATH = DATA_DIR / "live_signals.json"
OUTPUT_PATH = DATA_DIR / "bet_recommendations.json"

# Minimum composite scores for recommendation tiers (backtested)
MIN_SCORE_BET = 0.40
MIN_SCORE_HIGH = 0.50


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
        score = m.get("composite_score", 0) or 0
        if score < MIN_SCORE_BET:
            continue

        outcome = m.get("top_outcome", "?")
        confidence = m.get("confidence", "LOW")
        market_type = m.get("market_type", "")

        # Total markets are fades (backtest shows 42.7% accuracy)
        if market_type == "total":
            outcomes = m.get("outcomes", {})
            other = [k for k in outcomes.keys() if k != outcome]
            rec = {
                "slug": m.get("slug", ""),
                "event_slug": m.get("event_slug", ""),
                "game_date": m.get("game_date", ""),
                "market_type": market_type,
                "action": "FADE",
                "predicted_outcome": other[0] if other else "opposite",
                "composite_score": score,
                "confidence": confidence,
                "volume": m.get("total_weighted_volume", 0),
                "traders": m.get("unique_traders", 0),
            }
            fades.append(rec)
        else:
            rec = {
                "slug": m.get("slug", ""),
                "event_slug": m.get("event_slug", ""),
                "game_date": m.get("game_date", ""),
                "market_type": market_type,
                "action": "BET",
                "predicted_outcome": outcome,
                "composite_score": score,
                "confidence": confidence,
                "volume": m.get("total_weighted_volume", 0),
                "traders": m.get("unique_traders", 0),
            }
            bets.append(rec)

    bets.sort(key=lambda r: -r["composite_score"])
    fades.sort(key=lambda r: -r["composite_score"])

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
