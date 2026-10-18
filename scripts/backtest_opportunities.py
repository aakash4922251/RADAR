"""Score the Opportunity Finder against what actually happened.

    python scripts/backtest_opportunities.py 2024-01-01 2025-01-01
    python scripts/backtest_opportunities.py 2025-01-01 --keyword housekeeping

For each as-of date, predictions are built from awards before that date only
and checked against later awards. See core.dataset_import.backtest_aoc_signals.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core.dataset_import import backtest_aoc_signals, find_historical_aoc_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("as_of", nargs="+", help="one or more YYYY-MM-DD dates to pretend it is")
    parser.add_argument("--horizon-days", type=int, default=182)
    parser.add_argument("--keyword", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--date-column", choices=["aoc_date", "closing_date"], default="aoc_date")
    args = parser.parse_args()

    root = find_historical_aoc_root(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "historical"))
    if root is None:
        print("No historical AOC data found under data/historical.", file=sys.stderr)
        return 1
    pd.set_option("display.width", 220)
    for as_of in args.as_of:
        result = backtest_aoc_signals(root, as_of=as_of, horizon_days=args.horizon_days, keyword=args.keyword,
                                      state=args.state, date_column=args.date_column)
        print(f"\n=== as of {result['as_of']} · checked against awards up to {result['data_end']}")
        print(result["summary"].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
