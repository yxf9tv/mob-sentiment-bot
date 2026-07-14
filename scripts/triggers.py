"""
Track when markets first cross the composite score threshold.

Each poll cycle, checks live markets against the composite threshold (0.40)
and logs the first moment a qualifying market crosses. Replaces the old
volume-only threshold system.

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

# Composite threshold: 62.0% accuracy, 46.2% coverage (backtested)
COMPOSITE_THRESHOLD = 0.40


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
    """Check all active markets against composite threshold and log first-time crosses."""
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

            composite_score = m.get("composite_score", 0) or 0
            confidence = m.get("confidence", "LOW")
            volume = m.get("total_weighted_volume", 0) or 0
            top_outcome = m.get("top_outcome", "")
            market_type = m.get("market_type", "")

            # Ensure state entry
            entry = state.get(cid)
            if entry is None:
                entry = {
                    "condition_id": cid,
                    "slug": m.get("slug", ""),
                    "market_type": market_type,
                    "game_date": m.get("game_date", ""),
                    "first_seen": now_iso,
                    "crossed": False,
                    "last_composite": 0,
                    "last_outcome": top_outcome,
                }
                state[cid] = entry

            already = entry.get("crossed", False)
            current = composite_score >= COMPOSITE_THRESHOLD
            if current and not already:
                event = {
                    "event_type": "composite_crossed",
                    "condition_id": cid,
                    "slug": m.get("slug", ""),
                    "market_type": market_type,
                    "composite_score": round(composite_score, 4),
                    "confidence": confidence,
                    "threshold": COMPOSITE_THRESHOLD,
                    "crossed_at": now_iso,
                    "volume": round(volume, 2),
                    "top_outcome": top_outcome,
                    "unique_traders": m.get("unique_traders", 0),
                    "depth_imbalance": m.get("orderbook", {}).get("depth_imbalance", None),
                    "total_trade_events": m.get("total_trade_events", 0),
                    "game_date": m.get("game_date", ""),
                    "confidence_spike": m.get("confidence_spike", False),
                }
                log_event(event)
                new_events.append(event)
                entry["crossed"] = True
                entry["crossed_at"] = now_iso
                updated_any = True

            entry["last_composite"] = round(composite_score, 4)
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

    print(f"\nTrigger events: {len(events)} total")
    for e in events[-10:]:
        ts = e.get("crossed_at", "")[:19]
        score = e.get("composite_score", 0)
        conf = e.get("confidence", "?")
        slug = e.get("slug", "?")
        print(f"  {ts}  {slug}  score={score:.3f}  {conf}")


if __name__ == "__main__":
    summarize_events()
