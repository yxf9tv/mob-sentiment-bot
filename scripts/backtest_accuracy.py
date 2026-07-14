"""Backtest accuracy modifier and confidence spike detection.

Uses the 353 resolved predictions and 30-day trade history to measure
the impact of accuracy-based trader weighting and spike detection.

Usage:
    python3 scripts/backtest_accuracy.py
"""

import json, math, os, glob
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BACKTEST_PATH = DATA_DIR / "backtest_results.json"
TRADER_ACC_PATH = DATA_DIR / "trader_accuracy.json"
TRADE_CACHE_DIR = DATA_DIR / "cache" / "trades_long"
OUTCOME_CACHE = DATA_DIR / "cache" / "outcome_cache.json"

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


def edge_score(mt):
    return EDGE_SCORES.get(mt, 0.50)


def load_data():
    with open(BACKTEST_PATH) as f:
        bt = json.load(f)
    preds = bt["predictions"]
    resolved = [p for p in preds if p.get("correct") is not None]

    with open(TRADER_ACC_PATH) as f:
        trader_acc = json.load(f)

    return resolved, trader_acc


def build_trade_index():
    """Build condition_id -> trades mapping from 30-day cache."""
    cid_to_trades = defaultdict(list)
    if not TRADE_CACHE_DIR.exists():
        return cid_to_trades
    for f in glob.glob(str(TRADE_CACHE_DIR / "trades_*.json")):
        with open(f) as fh:
            cached = json.load(fh)
        wallet = cached.get("wallet", "")
        for trade in cached.get("trades", []):
            cid = trade.get("condition_id", "")
            if cid:
                cid_to_trades[cid].append({
                    "wallet": wallet,
                    "side": trade.get("side"),
                    "outcome": trade.get("outcome"),
                    "size": trade.get("size", 0) or 0,
                    "price": trade.get("price", 0) or 0,
                })
    return cid_to_trades


def get_accuracy_modifier(wallet, market_type, trader_acc):
    """Get accuracy modifier for a trader on a market type."""
    trader = trader_acc.get(wallet, {})
    mt_stats = trader.get(market_type, {})
    if not mt_stats.get("reliable", False):
        return 1.0
    accuracy = mt_stats.get("accuracy", 0)
    if accuracy >= 0.60:
        return 1.2
    elif accuracy <= 0.45:
        return 0.7
    return 1.0


def get_spike_modifier(trades, avg_notional, trader_acc, market_type):
    """Check if any ACCURATE trader has a confidence spike (>=1.5x avg).
    
    Only amplifies if the spike is from a trader with proven accuracy.
    """
    if avg_notional <= 0:
        return 1.0, False
    for t in trades:
        notional = t["size"] * t["price"]
        if notional >= avg_notional * 1.5:
            # Check if this trader is accurate on this market type
            acc_mod = get_accuracy_modifier(t["wallet"], market_type, trader_acc)
            if acc_mod >= 1.2:  # Only amplify for accurate traders (>=60% WR)
                return 2.0, True
    return 1.0, False


def compute_composite(p, w):
    """Compute base composite score."""
    return (
        w[0] * vol_score(p.get("total_weighted_volume", 0))
        + w[1] * trad_score(p.get("unique_traders", 0))
        + w[2] * 0.5  # No liquidity data in backtest
        + w[3] * edge_score(p.get("market_type", "other"))
    )


def compute_adjusted_composite(p, trades, trader_acc, w, apply_accuracy=True, apply_spike=True):
    """Compute composite with accuracy and spike modifiers."""
    base = compute_composite(p, w)

    if not trades:
        return base, 1.0, False

    # Compute average notional across all trades on this market
    notionals = [t["size"] * t["price"] for t in trades]
    avg_notional = sum(notionals) / len(notionals) if notionals else 0

    # Compute accuracy modifier (average across all traders)
    if apply_accuracy:
        wallets = set(t["wallet"] for t in trades)
        acc_mods = [get_accuracy_modifier(w2, p.get("market_type", "other"), trader_acc) for w2 in wallets]
        avg_acc_mod = sum(acc_mods) / len(acc_mods) if acc_mods else 1.0
    else:
        avg_acc_mod = 1.0

    # Compute spike modifier (only for accurate traders)
    if apply_spike:
        market_type = p.get("market_type", "other")
        spike_mod, has_spike = get_spike_modifier(trades, avg_notional, trader_acc, market_type)
    else:
        spike_mod, has_spike = 1.0, False

    adjusted = base * avg_acc_mod * spike_mod
    return adjusted, avg_acc_mod, has_spike


def evaluate(resolved, cid_to_trades, trader_acc, w, threshold, 
             apply_accuracy=True, apply_spike=True, fade_totals=False):
    """Evaluate a configuration."""
    scored = []
    for p in resolved:
        cid = p.get("condition_id", "")
        trades = cid_to_trades.get(cid, [])

        if apply_accuracy or apply_spike:
            adj_score, acc_mod, has_spike = compute_adjusted_composite(
                p, trades, trader_acc, w, apply_accuracy, apply_spike
            )
        else:
            adj_score = compute_composite(p, w)
            acc_mod = 1.0
            has_spike = False

        scored.append({
            **p,
            "composite": adj_score,
            "acc_mod": acc_mod,
            "has_spike": has_spike,
            "has_trade_data": len(trades) > 0,
        })

    # Filter by threshold
    filtered = [s for s in scored if s["composite"] >= threshold]

    # Apply fade to totals if enabled
    if fade_totals:
        for s in filtered:
            if s["market_type"] == "total":
                s["correct"] = not s["correct"]

    correct = sum(1 for s in filtered if s["correct"])
    total = len(filtered)
    coverage = total / len(resolved) if resolved else 0

    # ROI: assume -5% vig on each bet
    if total > 0:
        wins = correct
        losses = total - correct
        pnl = wins * 0.9 - losses  # Win $0.90 per $1 bet (after vig)
        roi = pnl / total * 100
    else:
        pnl = 0
        roi = 0

    return {
        "accuracy": round(correct / total, 4) if total else 0,
        "coverage": round(coverage, 4),
        "correct": correct,
        "total": total,
        "pnl": round(pnl, 2),
        "roi": round(roi, 2),
    }


def main():
    resolved, trader_acc = load_data()
    cid_to_trades = build_trade_index()

    print(f"Loaded {len(resolved)} resolved predictions")
    print(f"Loaded {len(trader_acc)} traders with accuracy data")
    print(f"Loaded trades for {len(cid_to_trades)} condition IDs")
    print(f"Predictions with trade data: {sum(1 for p in resolved if p.get('condition_id') in cid_to_trades)}")
    print()

    # Base composite weights (backtested optimal)
    W = (0.40, 0.30, 0.10, 0.20)

    # Test configurations
    configs = [
        ("Base composite", False, False, False),
        ("+ Accuracy modifier", True, False, False),
        ("+ Spike detection", False, True, False),
        ("+ Accuracy + Spike", True, True, False),
        ("+ Fade totals", False, False, True),
        ("+ All (accuracy + spike + fade)", True, True, True),
    ]

    thresholds = [0.40, 0.45, 0.50, 0.55, 0.60]

    print("=" * 100)
    print("ACCURACY MODIFIER & SPIKE DETECTION BACKTEST")
    print("=" * 100)
    print(f"{'Config':40s} {'Thresh':6s} {'Acc':>6s} {'Cov':>6s} {'N':>4s} {'PnL':>8s} {'ROI':>7s}")
    print("-" * 100)

    best_roi = -999
    best_config = None

    for name, apply_acc, apply_spike, fade_totals in configs:
        for th in thresholds:
            metrics = evaluate(resolved, cid_to_trades, trader_acc, W, th,
                             apply_acc, apply_spike, fade_totals)
            if metrics["total"] == 0:
                continue

            print(f"{name:40s} {th:6.2f} {metrics['accuracy']:6.1%} {metrics['coverage']:6.1%} {metrics['total']:4d} ${metrics['pnl']:7.2f} {metrics['roi']:6.1f}%")

            if metrics["roi"] > best_roi and metrics["total"] >= 10:
                best_roi = metrics["roi"]
                best_config = (name, th, metrics)

    print()
    print("=" * 100)
    print("BEST CONFIGURATION BY ROI (min 10 bets)")
    print("=" * 100)
    if best_config:
        name, th, metrics = best_config
        print(f"Config: {name}")
        print(f"Threshold: {th}")
        print(f"Accuracy: {metrics['accuracy']:.1%}")
        print(f"Coverage: {metrics['coverage']:.1%}")
        print(f"Bets: {metrics['total']}")
        print(f"PnL: ${metrics['pnl']:.2f}")
        print(f"ROI: {metrics['roi']:.1f}%")

    # Detailed analysis of accuracy modifier impact
    print()
    print("=" * 100)
    print("ACCURACY MODIFIER IMPACT (threshold=0.40)")
    print("=" * 100)

    base = evaluate(resolved, cid_to_trades, trader_acc, W, 0.40, False, False, False)
    with_acc = evaluate(resolved, cid_to_trades, trader_acc, W, 0.40, True, False, False)
    with_spike = evaluate(resolved, cid_to_trades, trader_acc, W, 0.40, False, True, False)
    with_both = evaluate(resolved, cid_to_trades, trader_acc, W, 0.40, True, True, False)
    with_fade = evaluate(resolved, cid_to_trades, trader_acc, W, 0.40, False, False, True)
    with_all = evaluate(resolved, cid_to_trades, trader_acc, W, 0.40, True, True, True)

    print(f"{'Config':40s} {'Acc':>6s} {'N':>4s} {'PnL':>8s} {'ROI':>7s}")
    print("-" * 70)
    print(f"{'Base':40s} {base['accuracy']:6.1%} {base['total']:4d} ${base['pnl']:7.2f} {base['roi']:6.1f}%")
    print(f"{'+ Accuracy':40s} {with_acc['accuracy']:6.1%} {with_acc['total']:4d} ${with_acc['pnl']:7.2f} {with_acc['roi']:6.1f}%")
    print(f"{'+ Spike':40s} {with_spike['accuracy']:6.1%} {with_spike['total']:4d} ${with_spike['pnl']:7.2f} {with_spike['roi']:6.1f}%")
    print(f"{'+ Accuracy + Spike':40s} {with_both['accuracy']:6.1%} {with_both['total']:4d} ${with_both['pnl']:7.2f} {with_both['roi']:6.1f}%")
    print(f"{'+ Fade totals':40s} {with_fade['accuracy']:6.1%} {with_fade['total']:4d} ${with_fade['pnl']:7.2f} {with_fade['roi']:6.1f}%")
    print(f"{'+ All':40s} {with_all['accuracy']:6.1%} {with_all['total']:4d} ${with_all['pnl']:7.2f} {with_all['roi']:6.1f}%")

    # Income projections
    print()
    print("=" * 100)
    print("INCOME PROJECTIONS")
    print("=" * 100)

    # Use the best config for projections
    if best_config:
        name, th, metrics = best_config
        acc = metrics["accuracy"]
        bets_per_day = metrics["total"] / 30  # 30-day backtest period
        avg_bet = 100  # Assume $100 per bet

        # Kelly criterion for optimal bet size
        if acc > 0.5:
            b = 0.9  # Decimal odds (after vig)
            kelly = (acc * b - (1 - acc)) / b
            kelly = max(0, min(kelly, 0.25))  # Cap at 25%
        else:
            kelly = 0

        weekly_bets = bets_per_day * 7
        monthly_bets = bets_per_day * 30

        # Expected value per bet
        ev_per_bet = (acc * 0.9 - (1 - acc)) * avg_bet  # After -5% vig

        weekly_income = ev_per_bet * weekly_bets
        monthly_income = ev_per_bet * monthly_bets

        print(f"Based on: {name} (threshold={th})")
        print(f"Accuracy: {acc:.1%}")
        print(f"Avg bets/day: {bets_per_day:.1f}")
        print(f"Assumed bet size: ${avg_bet}")
        print(f"Kelly fraction: {kelly:.1%}")
        print()
        print(f"Expected value per bet: ${ev_per_bet:.2f}")
        print(f"Weekly income (7 days): ${weekly_income:.2f}")
        print(f"Monthly income (30 days): ${monthly_income:.2f}")
        print()

        # Sensitivity analysis
        print("SENSITIVITY: Income by bet size")
        print(f"{'Bet Size':>10s} {'Weekly':>10s} {'Monthly':>10s}")
        print("-" * 35)
        for bet_size in [25, 50, 100, 200, 500]:
            ev = (acc * 0.9 - (1 - acc)) * bet_size
            weekly = ev * weekly_bets
            monthly = ev * monthly_bets
            print(f"${bet_size:>9d} ${weekly:>9.2f} ${monthly:>9.2f}")

    # Save results
    out_path = DATA_DIR / "accuracy_backtest_results.json"
    with open(out_path, "w") as f:
        json.dump({
            "resolved_count": len(resolved),
            "trade_data_count": sum(1 for p in resolved if p.get("condition_id") in cid_to_trades),
            "best_config": {
                "name": best_config[0] if best_config else None,
                "threshold": best_config[1] if best_config else None,
                "metrics": best_config[2] if best_config else None,
            },
            "configs_tested": configs,
        }, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
