"""Scoring module - evaluates trades against goal.yaml."""

import yaml
from pathlib import Path
from typing import Any


def load_goal(goal_path: str = None) -> dict:
    """Load goal configuration from YAML."""
    if goal_path is None:
        goal_path = Path(__file__).parent.parent / "state" / "goal.yaml"
    
    with open(goal_path, "r") as f:
        return yaml.safe_load(f)


def score(trades: list[dict], goal: dict = None) -> float:
    """Score a list of trades against the goal configuration.
    
    Returns a score in [-1, +1] where:
    - +1 = significantly exceeding all goals
    - 0 = meeting goals exactly
    - -1 = significantly below goals (beyond failure thresholds)
    
    Composite of:
    1. Realised return vs target
    2. Max drawdown vs max allowed
    3. Sharpe ratio vs minimum
    """
    if goal is None:
        goal = load_goal()
    
    if not trades:
        return 0.0
    
    # Calculate realised return
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    target_return = goal.get("target_return_30d", 0.05)
    
    # Return score component (-1 to +1)
    if target_return > 0:
        return_ratio = total_pnl / target_return
        return_score = max(-1, min(1, return_ratio))
    else:
        return_score = 0
    
    # Calculate max drawdown
    equity_curve = []
    cumulative = 0
    for t in trades:
        cumulative += t.get("pnl", 0)
        equity_curve.append(cumulative)
    
    peak = 0
    max_dd = 0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / abs(peak) if peak != 0 else 0
        if dd > max_dd:
            max_dd = dd
    
    max_drawdown_allowed = goal.get("max_drawdown", 0.08)
    
    # Drawdown score component (-1 to +1)
    if max_drawdown_allowed > 0:
        if max_dd <= max_drawdown_allowed * 0.5:
            dd_score = 1.0
        elif max_dd <= max_drawdown_allowed:
            dd_score = 1.0 - (max_dd / max_drawdown_allowed)
        else:
            dd_score = max(-1, 1.0 - 2 * (max_dd / max_drawdown_allowed))
    else:
        dd_score = 0
    
    # Calculate Sharpe ratio (simplified)
    if len(trades) >= 2:
        pnls = [t.get("pnl", 0) for t in trades]
        import statistics
        avg_pnl = statistics.mean(pnls)
        std_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 1.0
        sharpe = (avg_pnl / std_pnl) * (252 ** 0.5) if std_pnl > 0 else 0
    else:
        sharpe = 0
    
    min_sharpe = goal.get("min_sharpe", 1.2)
    
    # Sharpe score component (-1 to +1)
    if min_sharpe > 0:
        sharpe_ratio = sharpe / min_sharpe
        sharpe_score = max(-1, min(1, sharpe_ratio))
    else:
        sharpe_score = 0
    
    # Weighted composite score
    weights = [0.4, 0.35, 0.25]  # return, drawdown, sharpe
    composite = (
        weights[0] * return_score +
        weights[1] * dd_score +
        weights[2] * sharpe_score
    )
    
    return round(max(-1, min(1, composite)), 4)


def score_trade(trade: dict, goal: dict = None) -> float:
    """Score a single trade."""
    return score([trade], goal)