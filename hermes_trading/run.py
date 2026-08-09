"""Entry point for Hermes Trading Worker."""

import argparse
import sys

from .loop import main as run_loop_main


def main():
    parser = argparse.ArgumentParser(
        description="Hermes Trading Worker - Self-improving trading agent"
    )
    parser.add_argument(
        "--asset",
        type=str,
        default=None,
        help="Trading pair (e.g., BTC/USDT). Overrides goal.yaml setting."
    )
    args = parser.parse_args()
    
    run_loop_main(args.asset)


if __name__ == "__main__":
    main()