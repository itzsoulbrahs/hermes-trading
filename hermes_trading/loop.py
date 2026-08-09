"""Main trading loop - 24/7 reliability worker."""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .adapters import price as price_adapter
from .adapters import onchain as onchain_adapter
from .adapters import news as news_adapter
from .adapters import macro as macro_adapter
from .score import score

STATE_DIR = Path(__file__).parent.parent / "state"
# Durable data dir: survives redeploys when a Railway volume is mounted there.
# Strategy/goal stay in the image (git is their source of truth); only
# accumulated outcomes need to persist across deploys.
DATA_DIR = Path(os.environ.get("HERMES_DATA_DIR", STATE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STRATEGY_PATH = STATE_DIR / "strategy.yaml"
GOAL_PATH = STATE_DIR / "goal.yaml"
TRADES_PATH = DATA_DIR / "trades.jsonl"
HEARTBEAT_PATH = DATA_DIR / "heartbeat.json"

MAX_CONSECUTIVE_FAILURES = 5
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2  # seconds


class CircuitBreakerError(Exception):
    """Raised when too many consecutive failures occur."""
    pass


def load_strategy() -> dict:
    """Load current strategy."""
    with open(STRATEGY_PATH, "r") as f:
        return yaml.safe_load(f)


def load_goal() -> dict:
    """Load goal configuration."""
    with open(GOAL_PATH, "r") as f:
        return yaml.safe_load(f)


def write_heartbeat(status: str = "ok", last_action: str = ""):
    """Write heartbeat file."""
    heartbeat = {
        "timestamp": datetime.utcnow().isoformat(),
        "status": status,
        "last_action": last_action,
        "pid": os.getpid(),
    }
    with open(HEARTBEAT_PATH, "w") as f:
        json.dump(heartbeat, f, indent=2)


def log_trade(trade: dict):
    """Append trade to JSONL file and emit it to stdout.

    The stdout line is the observability channel: the container filesystem is
    not reachable from outside, so `railway logs` is how a closed trade becomes
    visible. Prefix is grep-able and the payload is the exact JSONL record.
    """
    with open(TRADES_PATH, "a") as f:
        f.write(json.dumps(trade) + "\n")
    print(f"TRADE_CLOSED {json.dumps(trade)}", flush=True)


async def retry_fetch(fetch_func, *args, **kwargs):
    """Fetch with exponential backoff retry."""
    last_error = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return await fetch_func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY ** attempt)
    raise last_error


def evaluate_strategy(market_data: dict, strategy: dict) -> dict:
    """Evaluate whether to enter a trade based on strategy rules.
    
    Returns signal dict with 'action' (buy/sell/hold) and metadata.
    """
    entry = strategy.get("entry", {})
    indicator = entry.get("indicator", "rsi")
    threshold = entry.get("threshold", 30)
    direction = entry.get("direction", "long")
    
    current_value = market_data.get("data", {}).get(indicator)
    
    if current_value is None:
        return {"action": "hold", "reason": f"No {indicator} data available"}
    
    if direction == "long":
        if current_value <= threshold:
            return {
                "action": "buy",
                "reason": f"{indicator} ({current_value:.1f}) <= threshold ({threshold})",
                "indicator": indicator,
                "value": current_value,
            }
    else:  # short
        if current_value >= threshold:
            return {
                "action": "sell",
                "reason": f"{indicator} ({current_value:.1f}) >= threshold ({threshold})",
                "indicator": indicator,
                "value": current_value,
            }
    
    return {"action": "hold", "reason": f"{indicator} ({current_value:.1f}) not crossing threshold ({threshold})"}


def evaluate_exit(market_data: dict, strategy: dict, position: dict) -> dict:
    """Decide whether an open position should be closed.

    Entry-only strategies deadlock: with direction=long, evaluate_strategy()
    can only ever return buy/hold, so a position's sole exit was stop_loss and
    every closed trade was a guaranteed loser. This supplies the missing leg.

    Both conditions are read from strategy.yaml's `exit` block so reflection
    cycles can tune them like any other variable. Either may be omitted (null)
    to disable that condition; stop_loss remains handled by the caller.

    Returns {"action": "close"|"hold", "reason": str, "exit_reason": str}.
    """
    exit_cfg = strategy.get("exit") or {}
    entry_price = position["entry_price"]
    current_price = market_data.get("data", {}).get("close")

    if current_price is None or not entry_price:
        return {"action": "hold", "reason": "no price data"}

    # Take-profit: gain since entry, in the direction of the position.
    take_profit_pct = exit_cfg.get("take_profit_pct")
    if take_profit_pct is not None:
        if position.get("direction", "long") == "long":
            gain_pct = (current_price - entry_price) / entry_price * 100
        else:
            gain_pct = (entry_price - current_price) / entry_price * 100
        if gain_pct >= take_profit_pct:
            return {
                "action": "close",
                "exit_reason": "take_profit",
                "reason": f"gain {gain_pct:.2f}% >= take_profit_pct ({take_profit_pct}%)",
            }

    # Mean-reversion exit: RSI recovered into overbought territory.
    rsi_exit = exit_cfg.get("rsi_exit")
    rsi = market_data.get("data", {}).get("rsi")
    if rsi_exit is not None and rsi is not None:
        if position.get("direction", "long") == "long" and rsi >= rsi_exit:
            return {
                "action": "close",
                "exit_reason": "rsi_exit",
                "reason": f"rsi ({rsi:.1f}) >= rsi_exit ({rsi_exit})",
            }

    return {"action": "hold", "reason": "no exit condition met"}


async def run_loop(asset: str = "BTC/USDT"):
    """Main trading loop."""
    consecutive_failures = 0
    iteration = 0
    active_position = None  # Track open positions for paper trading
    
    print(f"Starting Hermes Trading Worker for {asset}")
    print(f"Mode: {os.environ.get('HERMES_TRADING_MODE', 'paper')}")
    
    while True:
        iteration += 1
        try:
            # Write heartbeat
            write_heartbeat("working", f"iteration {iteration}")
            
            # Fetch all data with retries
            try:
                price_data = await retry_fetch(price_adapter.fetch, asset)
                onchain_data = await retry_fetch(onchain_adapter.fetch, asset.split("/")[0])
                news_data = await retry_fetch(news_adapter.fetch, asset.split("/")[0].lower())
                macro_data = await retry_fetch(macro_adapter.fetch)
                
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                print(f"Data fetch error (attempt {consecutive_failures}): {e}")
                
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise CircuitBreakerError(f"Too many consecutive failures: {consecutive_failures}")
                
                write_heartbeat("error", str(e))
                await asyncio.sleep(60)
                continue
            
            # Validate schema versions
            for data, name in [
                (price_data, "price"),
                (onchain_data, "onchain"),
                (news_data, "news"),
                (macro_data, "macro"),
            ]:
                if data.get("schema_version") != "1.0":
                    raise ValueError(f"{name} adapter schema mismatch: {data.get('schema_version')}")
            
            # Load current strategy
            strategy = load_strategy()
            
            # Evaluate entry signal
            signal = evaluate_strategy(price_data, strategy)
            
            print(f"[{datetime.utcnow().isoformat()}] Iteration {iteration}: "
                  f"BTC ${price_data['data']['close']:.2f}, "
                  f"RSI {price_data['data']['rsi']:.1f}, "
                  f"Signal: {signal['action']}")
            
            # Paper trade logic
            mode = os.environ.get("HERMES_TRADING_MODE", "paper")
            if mode == "paper":
                if active_position is None and signal["action"] == "buy":
                    # Open long position
                    position_size = strategy.get("position_size_r", 0.5)
                    entry_price = price_data["data"]["close"]
                    active_position = {
                        "entry_time": datetime.utcnow().isoformat(),
                        "entry_price": entry_price,
                        "direction": "long",
                        "size": position_size,
                        "stop_loss_pct": strategy.get("stop_loss_pct", 2.0),
                    }
                    print(f"  -> Paper LONG opened at ${entry_price:.2f}")

                elif active_position is not None:
                    # Exit is driven by the strategy's `exit` block, not by the
                    # entry signal: a long-only entry rule never emits "sell".
                    exit_decision = evaluate_exit(price_data, strategy, active_position)

                    if exit_decision["action"] == "close":
                        exit_price = price_data["data"]["close"]
                        entry_price = active_position["entry_price"]

                        if active_position["direction"] == "long":
                            pnl_pct = (exit_price - entry_price) / entry_price
                        else:
                            pnl_pct = (entry_price - exit_price) / entry_price

                        pnl = pnl_pct * active_position["size"]

                        trade_record = {
                            "timestamp": datetime.utcnow().isoformat(),
                            "asset": asset,
                            "direction": active_position["direction"],
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "pnl": round(pnl, 6),
                            "pnl_pct": round(pnl_pct, 6),
                            "size": active_position["size"],
                            "strategy_version": strategy.get("version", "01"),
                            "exit_reason": exit_decision["exit_reason"],
                        }

                        log_trade(trade_record)
                        print(f"  -> Paper position closed ({exit_decision['exit_reason']}): "
                              f"{exit_decision['reason']} | PnL = {pnl_pct:.4%} (${pnl:.4f})")

                        active_position = None
            
            # Check stop loss for active position
            if active_position is not None:
                current_price = price_data["data"]["close"]
                entry_price = active_position["entry_price"]
                stop_loss_pct = active_position["stop_loss_pct"] / 100
                
                if active_position["direction"] == "long":
                    if current_price <= entry_price * (1 - stop_loss_pct):
                        # Stop loss triggered
                        pnl = -stop_loss_pct * active_position["size"]
                        
                        trade_record = {
                            "timestamp": datetime.utcnow().isoformat(),
                            "asset": asset,
                            "direction": "long",
                            "entry_price": entry_price,
                            "exit_price": current_price,
                            "pnl": round(pnl, 6),
                            "pnl_pct": round(-stop_loss_pct, 6),
                            "size": active_position["size"],
                            "strategy_version": strategy.get("version", "01"),
                            "exit_reason": "stop_loss",
                        }
                        
                        log_trade(trade_record)
                        print(f"  -> Stop loss triggered at ${current_price:.2f}")
                        active_position = None
            
            # Score recent trades periodically
            if iteration % 10 == 0:
                trades = []
                if TRADES_PATH.exists():
                    with open(TRADES_PATH, "r") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                trades.append(json.loads(line))
                
                if trades:
                    current_score = score(trades, load_goal())
                    print(f"  Current score: {current_score:+.4f} ({len(trades)} trades)")
            
            write_heartbeat("ok", f"iteration {iteration}, signal: {signal['action']}")
            
        except CircuitBreakerError as e:
            print(f"CIRCUIT BREAKER: {e}")
            write_heartbeat("circuit_breaker", str(e))
            await asyncio.sleep(300)  # Wait 5 minutes before retrying
            consecutive_failures = 0
            
        except Exception as e:
            print(f"Unexpected error: {e}")
            write_heartbeat("error", str(e))
            
        # Wait 1 minute between iterations
        await asyncio.sleep(60)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Hermes Trading Worker Loop")
    parser.add_argument("--asset", type=str, default=None, help="Trading pair (overrides goal.yaml)")
    args = parser.parse_args()
    
    # Determine asset
    asset = args.asset
    if asset is None:
        goal = load_goal()
        asset = goal.get("asset", "BTC/USDT")
    
    try:
        asyncio.run(run_loop(asset))
    except KeyboardInterrupt:
        print("\nWorker stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()