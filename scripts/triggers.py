"""
Track when markets first cross edge volume thresholds during their lifetime.

Each poll cycle, checks live markets against known thresholds and logs the
first moment a qualifying market crosses. Accumulates data for later
backtesting: "if we fired at this moment, what accuracy would we get?"

Output:
  data/trigger_state.json   — per-market crossing state (mutable, overwritten)
  data/trigger_events.jsonl — timestamped events (append-only)
"""

import json, datetime
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "trigger_state.json"
EVENTS_PATH = DATA_DIR / "trigger_events.jsonl"

# Threshold definitions: (label, market_type, min_volume, description)
THRESHOLDS = [
    ("ml_high_vol",   "moneyline", 1071.0, "ML + vol >= $1,071 -> 74.4%"),
    ("spread_mid_vol", "spread",   107.0,  "Spread + vol >= $107 -> 73.1%"),
    ("ml_mid_vol",    "moneyline", 107.0,  "ML + vol >= $107 -> 64.0%"),
    ("total_cross",   "total",     0.0,    "Total market (fade) -> 57.3%"),
]


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log_event(event):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_PATH, "a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def process_live_data(active_games, polled_at):
    """Check all active markets against thresholds and log first-time crosses."""
    state = load_state()
    now_iso = polled_at if isinstance(polled_at, str) else polled_at.isoformat()
    new_events = []
    updated_any = False

    for event_slug, game in active_games.items():
        for m in game.get("all_markets", []):
            if not m.get("has_trader_data"):
                continue
            cid = m.get("condition_id", "")
            if not cid:
                continue
            market_type = m.get("market_type", "")
            volume = m.get("total_weighted_volume", 0) or 0
            top_outcome = m.get("top_outcome", "")
            conviction = m.get("conviction", 0)

            # Ensure state entry
            entry = state.get(cid)
            if entry is None:
                entry = {
                    "condition_id": cid,
                    "slug": m.get("slug", ""),
                    "market_type": market_type,
                    "game_date": m.get("game_date", ""),
                    "first_seen": now_iso,
                    "thresholds_crossed": {},
                    "last_volume": 0,
                    "last_outcome": top_outcome,
                }
                state[cid] = entry

            for label, mt, min_vol, desc in THRESHOLDS:
                if market_type != mt:
                    continue
                already = entry["thresholds_crossed"].get(label, {}).get("crossed", False)
                current = volume >= min_vol
                if current and not already:
                    event = {
                        "event_type": "threshold_crossed",
                        "condition_id": cid,
                        "slug": m.get("slug", ""),
                        "market_type": market_type,
                        "threshold": label,
                        "description": desc,
                        "crossed_at": now_iso,
                        "volume": round(volume, 2),
                        "min_volume": min_vol,
                        "top_outcome": top_outcome,
                        "conviction": conviction,
                        "unique_traders": m.get("unique_traders", 0),
                        "depth_imbalance": m.get("orderbook", {}).get("depth_imbalance", None),
                        "total_trade_events": m.get("total_trade_events", 0),
                        "game_date": m.get("game_date", ""),
                        "min_volume": min_vol,
                    }
                    log_event(event)
                    new_events.append(event)
                    entry["thresholds_crossed"][label] = {
                        "crossed": True,
                        "crossed_at": now_iso,
                        "volume": round(volume, 2),
                    }
                    updated_any = True

            entry["last_volume"] = volume
            entry["last_outcome"] = top_outcome
            entry["last_poll"] = now_iso

    if updated_any:
        save_state(state)

    return new_events


def summarize_events():
    """Print a summary of all logged trigger events."""
    if not EVENTS_PATH.exists():
        print("No trigger events logged yet.")
        return
    events = []
    with open(EVENTS_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    by_threshold = defaultdict(list)
    for e in events:
        by_threshold[e["threshold"]].append(e)

    print(f"\nTrigger events: {len(events)} total")
    for label, items in sorted(by_threshold.items()):
        print(f"  {label}: {len(items)} events")
        for e in items[:5]:
            ts = e["crossed_at"][:19]
            vol = e["volume"]
            slug = e["slug"]
            print(f"    {ts}  {slug}  vol=${vol:,.0f}")
        if len(items) > 5:
            print(f"    ... and {len(items)-5} more")


if __name__ == "__main__":
    summarize_events()
