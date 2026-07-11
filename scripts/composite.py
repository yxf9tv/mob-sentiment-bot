"""Composite score for MLB prediction markets.

Combines volume, trader count, liquidity depth, and market type into a
single 0-1 score representing overall signal quality. Weights are
placeholders — run backtest_composite.py to optimize.
"""

import math

# --- Backtested weights (optimal from 684 combinations) ---
# 62.0% accuracy, 46.2% coverage, 23.9% ROI on 353 resolved markets
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
CONFIDENCE_HIGH = 0.50   # 63.0% accuracy, 33.7% coverage
CONFIDENCE_MEDIUM = 0.40 # 62.0% accuracy, 46.2% coverage


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
