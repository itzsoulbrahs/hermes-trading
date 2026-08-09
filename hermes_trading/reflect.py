"""Reflection module - evolves strategy based on trade outcomes."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .score import score, load_goal

STATE_DIR = Path(__file__).parent.parent / "state"
STRATEGY_PATH = STATE_DIR / "strategy.yaml"
TRADES_PATH = STATE_DIR / "trades.jsonl"
HYPOTHESES_PATH = STATE_DIR / "hypotheses.jsonl"
HISTORY_DIR = STATE_DIR / "history"
GOAL_PATH = STATE_DIR / "goal.yaml"


def load_strategy() -> dict:
    """Load current strategy."""
    with open(STRATEGY_PATH, "r") as f:
        return yaml.safe_load(f)


def save_strategy(strategy: dict):
    """Save strategy to file."""
    with open(STRATEGY_PATH, "w") as f:
        yaml.dump(strategy, f, default_flow_style=False)


def load_trades(limit: int = None) -> list[dict]:
    """Load trades from JSONL file."""
    if not TRADES_PATH.exists():
        return []
    
    trades = []
    with open(TRADES_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    
    if limit:
        trades = trades[-limit:]
    
    return trades


def append_hypothesis(hypothesis: dict):
    """Append hypothesis to JSONL file."""
    with open(HYPOTHESES_PATH, "a") as f:
        f.write(json.dumps(hypothesis) + "\n")


def save_history(strategy: dict):
    """Save current strategy to history before modification."""
    version = strategy.get("version", "00")
    history_path = HISTORY_DIR / f"v{version}.yaml"
    with open(history_path, "w") as f:
        yaml.dump(strategy, f, default_flow_style=False)


def apply_hypothesis(strategy: dict, hypothesis: dict) -> dict:
    """Apply a hypothesis (variable change) to the strategy."""
    variable = hypothesis["variable"]
    new_value = hypothesis["new_value"]
    
    # Navigate nested keys (e.g., "entry.threshold")
    keys = variable.split(".")
    obj = strategy
    for key in keys[:-1]:
        obj = obj.setdefault(key, {})
    
    old_value = obj.get(keys[-1])
    obj[keys[-1]] = new_value
    
    # Bump version
    current_version = int(strategy.get("version", "1"))
    strategy["version"] = str(current_version + 1).zfill(2)
    
    return strategy, old_value


async def reflect_fallback():
    """Deterministic fallback reflection - used before Hermes is installed.
    
    Rules:
    - If realised return < target → loosen entry.threshold by 2 (easier to enter)
    - If drawdown > max → tighten stop_loss_pct by 0.2 (tighter stops)
    - Always changes exactly ONE variable
    """
    goal = load_goal()
    strategy = load_strategy()
    trades = load_trades()
    
    if len(trades) < goal.get("reflection_every", 5):
        print(f"Not enough trades for reflection (need {goal['reflection_every']}, have {len(trades)})")
        return
    
    current_score = score(trades, goal)
    
    # Save current strategy to history
    save_history(strategy)
    
    # Determine which variable to change
    realised_return = sum(t.get("pnl", 0) for t in trades)
    target_return = goal.get("target_return_30d", 0.05)
    max_drawdown = goal.get("max_drawdown", 0.08)
    
    # Calculate max drawdown from trades
    equity_curve = []
    cumulative = 0
    for t in trades:
        cumulative += t.get("pnl", 0)
        equity_curve.append(cumulative)
    
    peak = 0
    actual_max_dd = 0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = (peak - val) / abs(peak) if peak != 0 else 0
        if dd > actual_max_dd:
            actual_max_dd = dd
    
    if actual_max_dd > max_drawdown:
        # Tighten stop loss
        variable = "stop_loss_pct"
        old_value = strategy.get("stop_loss_pct", 2.0)
        new_value = max(0.5, old_value - 0.2)
        strategy["stop_loss_pct"] = new_value
        
        hypothesis = {
            "timestamp": datetime.utcnow().isoformat(),
            "variable": variable,
            "old_value": old_value,
            "new_value": new_value,
            "reasoning": f"Drawdown {actual_max_dd:.2%} exceeded max {max_drawdown:.2%}. Tightening stop loss from {old_value}% to {new_value}%.",
            "score_before": current_score,
            "mode": "fallback",
        }
    elif realised_return < target_return:
        # Loosen entry threshold
        variable = "entry.threshold"
        old_value = strategy.get("entry", {}).get("threshold", 30)
        new_value = max(10, old_value - 2)
        strategy["entry"]["threshold"] = new_value
        
        hypothesis = {
            "timestamp": datetime.utcnow().isoformat(),
            "variable": variable,
            "old_value": old_value,
            "new_value": new_value,
            "reasoning": f"Return {realised_return:.2%} below target {target_return:.2%}. Loosening entry threshold from {old_value} to {new_value} to allow more trades.",
            "score_before": current_score,
            "mode": "fallback",
        }
    else:
        # Meeting goals - make a small optimization
        variable = "entry.threshold"
        old_value = strategy.get("entry", {}).get("threshold", 30)
        new_value = min(50, old_value + 1)  # Slightly tighten
        strategy["entry"]["threshold"] = new_value
        
        hypothesis = {
            "timestamp": datetime.utcnow().isoformat(),
            "variable": variable,
            "old_value": old_value,
            "new_value": new_value,
            "reasoning": f"Goals met. Slightly tightening entry threshold from {old_value} to {new_value} to improve quality.",
            "score_before": current_score,
            "mode": "fallback",
        }
    
    # Save updated strategy
    save_strategy(strategy)
    
    # Record hypothesis
    append_hypothesis(hypothesis)
    
    print(f"Reflection complete: v{strategy['version']}")
    print(f"  Changed: {variable} from {old_value} to {new_value}")
    print(f"  Reasoning: {hypothesis['reasoning']}")
    print(f"  Score: {current_score}")


async def reflect_hermes():
    """Production reflection mode - calls Hermes as subprocess.
    
    Reads last 25 trades and current strategy, formats prompt,
    calls hermes, parses hypothesis, applies it.
    """
    goal = load_goal()
    strategy = load_strategy()
    trades = load_trades(limit=25)
    
    if len(trades) < goal.get("reflection_every", 5):
        print(f"Not enough trades for reflection (need {goal['reflection_every']}, have {len(trades)})")
        return
    
    current_score = score(trades, goal)
    
    # Save current strategy to history
    save_history(strategy)
    
    # Format prompt for Hermes
    trades_json = json.dumps(trades, indent=2)
    strategy_yaml = yaml.dump(strategy, default_flow_style=False)
    
    prompt = f"""You are analyzing a trading strategy's performance. Here are the last {len(trades)} trades and current strategy:

TRADES (JSON):
{trades_json}

CURRENT STRATEGY (YAML):
{strategy_yaml}

GOAL:
- Target return: {goal.get('target_return_30d', 0.05)}
- Max drawdown: {goal.get('max_drawdown', 0.08)}
- Min Sharpe: {goal.get('min_sharpe', 1.2)}

Current score: {current_score}

Generate exactly ONE hypothesis to improve the strategy. Respond in this exact JSON format:
{{
  "variable": "path.to.variable",
  "new_value": new_numeric_value,
  "reasoning": "explanation"
}}

Rules:
- Change exactly ONE variable
- Only change numeric values
- Valid variables: entry.threshold, stop_loss_pct, position_size_r
"""
    
    # Call hermes subprocess
    try:
        result = subprocess.run(
            ["hermes", "--prompt", prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
        
        # Parse response - look for JSON in output
        output = result.stdout.strip()
        
        # Try to extract JSON from output
        import re
        json_match = re.search(r'\{[^}]*\}', output, re.DOTALL)
        if json_match:
            hypothesis_data = json.loads(json_match.group())
        else:
            raise ValueError(f"Could not parse Hermes output: {output}")
        
    except Exception as e:
        print(f"Hermes reflection failed: {e}")
        print("Falling back to deterministic reflection")
        await reflect_fallback()
        return
    
    # Apply hypothesis
    variable = hypothesis_data["variable"]
    new_value = hypothesis_data["new_value"]
    old_value = None
    
    keys = variable.split(".")
    obj = strategy
    for key in keys[:-1]:
        obj = obj.setdefault(key, {})
    old_value = obj.get(keys[-1])
    obj[keys[-1]] = new_value
    
    # Bump version
    current_version = int(strategy.get("version", "1"))
    strategy["version"] = str(current_version + 1).zfill(2)
    
    hypothesis = {
        "timestamp": datetime.utcnow().isoformat(),
        "variable": variable,
        "old_value": old_value,
        "new_value": new_value,
        "reasoning": hypothesis_data.get("reasoning", ""),
        "score_before": current_score,
        "mode": "hermes",
    }
    
    # Save updated strategy
    save_strategy(strategy)
    
    # Record hypothesis
    append_hypothesis(hypothesis)
    
    print(f"Hermes reflection complete: v{strategy['version']}")
    print(f"  Changed: {variable} from {old_value} to {new_value}")
    print(f"  Reasoning: {hypothesis['reasoning']}")


def main():
    parser = argparse.ArgumentParser(description="Strategy reflection module")
    parser.add_argument("--fallback", action="store_true", help="Use deterministic fallback reflection")
    parser.add_argument("--hermes", action="store_true", help="Use Hermes for reflection")
    args = parser.parse_args()
    
    if args.hermes:
        asyncio.run(reflect_hermes())
    elif args.fallback:
        asyncio.run(reflect_fallback())
    else:
        print("Specify --fallback or --hermes")
        sys.exit(1)


if __name__ == "__main__":
    main()