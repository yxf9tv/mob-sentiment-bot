"""Backtest composite score weights against historical predictions.

Loads backtest_results.json (353 resolved markets) and sweeps weight
combinations to find optimal parameters for the composite score.

Usage:
    python3 scripts/backtest_composite.py
"""

import json, math, itertools
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BACKTEST_PATH = DATA_DIR / "backtest_results.json"
TRACKED_PATH = DATA_DIR / "tracked_games.json"

LOG_MAX_VOL = math.log(1 + 20000)

TRADER_TIERS = [(5, 1.0), (3, 0.7), (0, 0.3)]

EDGE_SCORES = {
    "spread": 0.64,
    "moneyline": 0.56,
    "total": 0.43,
    "nrfi": 0.80,
    "futures": 0.25,
    "other": 0.50,
}


def vol_score(v):
    if v <= 0:
        return 0.0
    return min(math.log(1 + v) / LOG_MAX_VOL, 1.0)


def trad_score(t):
    for mn, sc in TRADER_TIERS:
        if t >= mn:
            return sc
    return 0.3


def liq_score(di):
    if di is None:
        return 0.0
    return max(di - 0.5, 0.0) * 2


def edge_score(mt):
    return EDGE_SCORES.get(mt, 0.50)


def load_data():
    with open(BACKTEST_PATH) as f:
        bt = json.load(f)

    # Build depth_imbalance lookup from tracked games
    di_map = {}
    if TRACKED_PATH.exists():
        with open(TRACKED_PATH) as f:
            tracked = json.load(f)
        for g in tracked["games"]:
            for m in g["markets"].values():
                cid = m.get("condition_id", "")
                if cid and m.get("depth_imbalance") is not None:
                    di_map[cid] = m["depth_imbalance"]

    predictions = bt["predictions"]
    resolved = [p for p in predictions if p.get("correct") is not None]
    return resolved, di_map


def compute_composite(p, di_map, w):
    cid = p.get("condition_id", "")
    depth_imb = di_map.get(cid)
    return (
        w[0] * vol_score(p.get("total_weighted_volume", 0))
        + w[1] * trad_score(p.get("unique_traders", 0))
        + w[2] * liq_score(depth_imb)
        + w[3] * edge_score(p.get("market_type", "other"))
    )


def evaluate(resolved, di_map, w, threshold, rank_formula):
    """Evaluate a weight/threshold/formula combination."""
    scored = []
    for p in resolved:
        cs = compute_composite(p, di_map, w)
        scored.append({**p, "composite": cs})

    # Apply ranking formula for ordering (not filtering)
    if rank_formula == "composite":
        scored.sort(key=lambda x: -x["composite"])
    elif rank_formula == "traders":
        scored.sort(key=lambda x: -(x["composite"] * math.sqrt(max(x.get("unique_traders", 0), 1))))
    elif rank_formula == "events":
        scored.sort(key=lambda x: -(x["composite"] * max(x.get("total_trade_events", 1), 1) ** 0.3))

    # Filter by threshold
    filtered = [s for s in scored if s["composite"] >= threshold]

    if not filtered:
        return {
            "accuracy": 0,
            "coverage": 0,
            "correct": 0,
            "total": 0,
            "simulated_pnl": 0,
            "simulated_roi": 0,
        }

    correct = sum(1 for s in filtered if s["correct"])
    total = len(filtered)
    coverage = total / len(resolved) if resolved else 0
    pnl = correct - (total - correct)
    roi = pnl / total * 100 if total else 0

    return {
        "accuracy": round(correct / total, 4),
        "coverage": round(coverage, 4),
        "correct": correct,
        "total": total,
        "simulated_pnl": pnl,
        "simulated_roi": round(roi, 2),
    }


def main():
    resolved, di_map = load_data()
    print(f"Loaded {len(resolved)} resolved markets")
    print(f"Markets with depth_imbalance: {len(di_map)}")
    print()

    # Weight sweep: volume, traders, liquidity, edge (must sum to 1.0)
    vol_range = [0.20, 0.30, 0.40, 0.50, 0.60]
    trad_range = [0.10, 0.15, 0.20, 0.25, 0.30]
    liq_range = [0.10, 0.15, 0.20, 0.25, 0.30]
    edge_range = [0.10, 0.15, 0.20]

    rank_formulas = ["composite", "traders", "events"]
    thresholds = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]

    results = []
    combos_tested = 0

    for v, tr, l, e in itertools.product(vol_range, trad_range, liq_range, edge_range):
        total_w = v + tr + l + e
        if abs(total_w - 1.0) > 0.01:
            continue
        w = (v, tr, l, e)

        for rf in rank_formulas:
            for th in thresholds:
                metrics = evaluate(resolved, di_map, w, th, rf)
                if metrics["total"] == 0:
                    continue
                results.append({
                    "weights": {"volume": v, "traders": tr, "liquidity": l, "edge": e},
                    "rank_formula": rf,
                    "threshold": th,
                    **metrics,
                })
                combos_tested += 1

    print(f"Tested {combos_tested} combinations\n")

    # Sort by accuracy (primary) then coverage (secondary)
    results.sort(key=lambda r: (-r["accuracy"], -r["coverage"]))

    # Top 20 by accuracy with coverage >= 10%
    print("=" * 90)
    print("TOP 20 BY ACCURACY (coverage >= 10%)")
    print("=" * 90)
    print(f"{'Weights (V/T/L/E)':25s} {'Formula':10s} {'Thresh':6s} {'Acc':>6s} {'Cov':>6s} {'N':>4s} {'PnL':>5s} {'ROI':>7s}")
    print("-" * 90)

    top_filtered = [r for r in results if r["coverage"] >= 0.10][:20]
    for r in top_filtered:
        w = r["weights"]
        wf = f"{w['volume']:.0%}/{w['traders']:.0%}/{w['liquidity']:.0%}/{w['edge']:.0%}"
        print(f"{wf:25s} {r['rank_formula']:10s} {r['threshold']:6.2f} {r['accuracy']:6.1%} {r['coverage']:6.1%} {r['total']:4d} ${r['simulated_pnl']:4d} {r['simulated_roi']:6.1f}%")

    # Top 20 by ROI with accuracy >= 60%
    print()
    print("=" * 90)
    print("TOP 20 BY ROI (accuracy >= 60%, coverage >= 5%)")
    print("=" * 90)
    print(f"{'Weights (V/T/L/E)':25s} {'Formula':10s} {'Thresh':6s} {'Acc':>6s} {'Cov':>6s} {'N':>4s} {'PnL':>5s} {'ROI':>7s}")
    print("-" * 90)

    roi_filtered = [r for r in results if r["accuracy"] >= 0.60 and r["coverage"] >= 0.05][:20]
    roi_filtered.sort(key=lambda r: -r["simulated_roi"])
    for r in roi_filtered:
        w = r["weights"]
        wf = f"{w['volume']:.0%}/{w['traders']:.0%}/{w['liquidity']:.0%}/{w['edge']:.0%}"
        print(f"{wf:25s} {r['rank_formula']:10s} {r['threshold']:6.2f} {r['accuracy']:6.1%} {r['coverage']:6.1%} {r['total']:4d} ${r['simulated_pnl']:4d} {r['simulated_roi']:6.1f}%")

    # Best balanced (accuracy * coverage)
    print()
    print("=" * 90)
    print("TOP 20 BY BALANCED SCORE (accuracy * coverage)")
    print("=" * 90)
    print(f"{'Weights (V/T/L/E)':25s} {'Formula':10s} {'Thresh':6s} {'Acc':>6s} {'Cov':>6s} {'N':>4s} {'PnL':>5s} {'ROI':>7s} {'Bal':>6s}")
    print("-" * 95)

    for r in results:
        r["balanced"] = r["accuracy"] * r["coverage"]

    bal_sorted = sorted(results, key=lambda r: -r["balanced"])[:20]
    for r in bal_sorted:
        w = r["weights"]
        wf = f"{w['volume']:.0%}/{w['traders']:.0%}/{w['liquidity']:.0%}/{w['edge']:.0%}"
        print(f"{wf:25s} {r['rank_formula']:10s} {r['threshold']:6.2f} {r['accuracy']:6.1%} {r['coverage']:6.1%} {r['total']:4d} ${r['simulated_pnl']:4d} {r['simulated_roi']:6.1f}% {r['balanced']:6.4f}")

    # Save full results
    out_path = DATA_DIR / "composite_backtest_results.json"
    with open(out_path, "w") as f:
        json.dump({"combos_tested": combos_tested, "results": results[:500]}, f, indent=2)
    print(f"\nFull results saved to {out_path}")

    # Also compare against current system
    print()
    print("=" * 90)
    print("COMPARISON: CURRENT THRESHOLD SYSTEM vs BEST COMPOSITE")
    print("=" * 90)

    # Current system: volume thresholds by market type
    current_correct = 0
    current_total = 0
    vol_thresholds = {
        "moneyline": 1071.0,
        "spread": 107.0,
        "total": 0.0,
    }
    for p in resolved:
        mt = p.get("market_type", "")
        vol = p.get("total_weighted_volume", 0)
        threshold = vol_thresholds.get(mt)
        if threshold is not None and vol >= threshold:
            current_total += 1
            if p["correct"]:
                current_correct += 1

    if current_total:
        current_acc = current_correct / current_total
        current_pnl = current_correct - (current_total - current_correct)
        current_roi = current_pnl / current_total * 100
        print(f"Current system:  {current_acc:.1%} accuracy, {current_total} markets, PnL=${current_pnl}, ROI={current_roi:.1f}%")

    # Best composite (by balanced score)
    if bal_sorted:
        best = bal_sorted[0]
        print(f"Best composite:  {best['accuracy']:.1%} accuracy, {best['total']} markets, PnL=${best['simulated_pnl']}, ROI={best['simulated_roi']:.1f}%")
        print(f"  Weights: volume={best['weights']['volume']:.0%}, traders={best['weights']['traders']:.0%}, liquidity={best['weights']['liquidity']:.0%}, edge={best['weights']['edge']:.0%}")
        print(f"  Formula: {best['rank_formula']}, threshold: {best['threshold']}")


if __name__ == "__main__":
    main()
