"""Composite score for MLB prediction markets.

Combines volume, trader count, liquidity depth, market type, and
trader-specific signals (accuracy by market type, confidence spikes)
into a single 0-1 score representing overall signal quality.
"""

import math
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# --- Backtested weights (optimal from 684 combinations) ---
W_VOLUME = 0.40
W_TRADERS = 0.30
W_LIQUIDITY = 0.10
W_EDGE = 0.20

# --- Normalization constants ---
LOG_MAX_VOL = math.log(1 + 20000)

# --- Trader score step function ---
TRADER_TIERS = [(5, 1.0), (3, 0.7), (0, 0.3)]

# --- Edge scores by market type (derived from backtest accuracy) ---
EDGE_SCORES = {
    "spread": 0.64,
    "moneyline": 0.56,
    "total": 0.43,
    "nrfi": 0.80,
    "futures": 0.25,
    "other": 0.50,
}

# --- Confidence thresholds (backtested) ---
CONFIDENCE_HIGH = 0.50
CONFIDENCE_MEDIUM = 0.40

# --- Trader accuracy thresholds ---
MIN_TRADES_FOR_ACCURACY = 5
HIGH_ACCURACY_THRESHOLD = 0.60
LOW_ACCURACY_THRESHOLD = 0.45

# --- Confidence spike thresholds ---
SPIKE_MULTIPLIER = 1.5
HIGH_SPIKE_MULTIPLIER = 2.0


def compute_volume_score(volume):
    """Log-normalized volume score. Negative volume (net selling) → 0."""
    if volume <= 0:
        return 0.0
    return min(math.log(1 + volume) / LOG_MAX_VOL, 1.0)


def compute_traders_score(traders):
    """Step function: 0-2 → 0.3, 3-4 → 0.7, 5+ → 1.0."""
    for min_count, score in TRADER_TIERS:
        if traders >= min_count:
            return score
    return 0.3


def compute_liquidity_score(depth_imbalance):
    """Symmetric penalty: rewards alignment, ignores opposition.

    depth_imbalance ranges [0, 1]:
      0.5 = neutral (score 0.0)
      1.0 = full alignment (score 1.0)
      0.0 = full opposition (score 0.0)
    """
    if depth_imbalance is None:
        return 0.0
    return max(depth_imbalance - 0.5, 0.0) * 2


def compute_edge_score(market_type):
    """Static score based on market type historical accuracy."""
    return EDGE_SCORES.get(market_type, 0.50)


def compute_composite(market, weights=None):
    """Compute composite score for a market dict.

    Args:
        market: dict with keys: total_weighted_volume, unique_traders,
                orderbook.depth_imbalance, market_type
        weights: optional dict overriding W_VOLUME, W_TRADERS, W_LIQUIDITY, W_EDGE

    Returns:
        float in [0, 1]
    """
    w = weights or {
        "volume": W_VOLUME,
        "traders": W_TRADERS,
        "liquidity": W_LIQUIDITY,
        "edge": W_EDGE,
    }

    volume = market.get("total_weighted_volume", 0) or 0
    traders = market.get("unique_traders", 0) or 0
    depth_imb = None
    ob = market.get("orderbook")
    if ob:
        depth_imb = ob.get("depth_imbalance")
    market_type = market.get("market_type", "other")

    vol_score = compute_volume_score(volume)
    trad_score = compute_traders_score(traders)
    liq_score = compute_liquidity_score(depth_imb)
    edge_score = compute_edge_score(market_type)

    composite = (
        w["volume"] * vol_score
        + w["traders"] * trad_score
        + w["liquidity"] * liq_score
        + w["edge"] * edge_score
    )
    return round(min(composite, 1.0), 4)


def classify_confidence(score):
    """Return confidence label based on composite score."""
    if score >= CONFIDENCE_HIGH:
        return "HIGH"
    if score >= CONFIDENCE_MEDIUM:
        return "MEDIUM"
    return "LOW"


# --- Trader accuracy and confidence spike functions ---

_trader_accuracy_cache = None

def load_trader_accuracy():
    """Load trader accuracy database (cached in memory)."""
    global _trader_accuracy_cache
    if _trader_accuracy_cache is not None:
        return _trader_accuracy_cache
    path = DATA_DIR / "trader_accuracy.json"
    if path.exists():
        with open(path) as f:
            _trader_accuracy_cache = json.load(f)
    else:
        _trader_accuracy_cache = {}
    return _trader_accuracy_cache


def get_trader_accuracy_modifier(wallet, market_type, trader_accuracy=None):
    """Get accuracy-based weight modifier for a trader on a specific market type.

    Returns:
        float: 1.2 if high accuracy, 0.7 if low accuracy, 1.0 if unknown/insufficient data
    """
    if trader_accuracy is None:
        trader_accuracy = load_trader_accuracy()

    trader = trader_accuracy.get(wallet, {})
    mt_stats = trader.get(market_type, {})

    if not mt_stats.get("reliable", False):
        return 1.0

    accuracy = mt_stats.get("accuracy", 0)
    if accuracy >= HIGH_ACCURACY_THRESHOLD:
        return 1.2
    elif accuracy <= LOW_ACCURACY_THRESHOLD:
        return 0.7
    return 1.0


def get_confidence_spike_modifier(notional, avg_notional):
    """Get confidence spike weight modifier based on bet size vs average.

    Returns:
        float: 2.0 for high spike, 1.5 for normal spike, 1.0 for normal
    """
    if avg_notional <= 0:
        return 1.0

    ratio = notional / avg_notional
    if ratio >= HIGH_SPIKE_MULTIPLIER:
        return 2.0
    elif ratio >= SPIKE_MULTIPLIER:
        return 1.5
    return 1.0


def compute_trader_weight(trader, market_type, trader_accuracy=None):
    """Compute weight for a single trader incorporating accuracy and spike signals.

    Args:
        trader: dict with wallet, baseball_pnl_15d, win_rate, sharpe_ratio,
                human_likeness_score, _avg_notional, _confidence_spike
        market_type: string (moneyline, spread, total, etc.)
        trader_accuracy: optional pre-loaded accuracy database

    Returns:
        float: weighted score for this trader
    """
    pnl = max(trader.get("baseball_pnl_15d") or 0, 0)
    wr = trader.get("win_rate") or 0.5
    sharpe = max(trader.get("sharpe_ratio") or 0, 0)
    human = (trader.get("human_likeness_score") or 50) / 100.0

    # Base weight (same formula as compute_sentiment.py)
    max_pnl = 100000
    pnl_w = min(pnl / max_pnl, 1.0)
    wr_w = max(wr - 0.5, 0) * 2
    sharpe_w = min(sharpe / 2.0, 1.0)
    base_weight = 0.5 * pnl_w + 0.2 * wr_w + 0.15 * sharpe_w + 0.15 * human

    # Apply accuracy modifier
    wallet = trader.get("wallet", "")
    accuracy_mod = get_trader_accuracy_modifier(wallet, market_type, trader_accuracy)

    # Apply confidence spike modifier
    avg_notional = trader.get("_avg_notional", 0)
    notional = trader.get("_last_notional", 0)
    spike_mod = get_confidence_spike_modifier(notional, avg_notional) if notional > 0 else 1.0

    return base_weight * accuracy_mod * spike_mod
