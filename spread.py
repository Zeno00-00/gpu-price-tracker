#!/usr/bin/env python3
"""
Spot-vs-Term Spread Analysis
============================
Computes the contango/backwardation signal: posted spot rates (from
scraper.py output) vs 1-year contract rates (entered manually from
SemiAnalysis, broker quotes, or neocloud disclosures in
data/manual_term_rates.csv).

Interpretation:
  spread = spot - term_1y
  spread > 0 (backwardation): demand is winning - capacity is scarce NOW
  spread < 0 (contango):      supply is winning - sellers discount spot
                              to fill idle capacity

Usage: python spread.py [GPU_MODEL]   (default: H100)
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SPOT_CSV = DATA_DIR / "gpu_prices.csv"
TERM_CSV = DATA_DIR / "manual_term_rates.csv"


def load_latest_spot(gpu):
    """Median across providers of the most recent date, excluding
    community-tier listings (not comparable to dedicated capacity)."""
    if not SPOT_CSV.exists():
        sys.exit("No spot data yet - run scraper.py first.")
    rows = list(csv.DictReader(open(SPOT_CSV)))
    rows = [r for r in rows if r["gpu_model"] == gpu
            and "community" not in r["tier"]]
    if not rows:
        sys.exit(f"No spot rows for {gpu}.")
    latest = max(r["date"] for r in rows)
    todays = [float(r["price_usd_per_gpu_hr"]) for r in rows
              if r["date"] == latest]
    todays.sort()
    mid = todays[len(todays) // 2] if len(todays) % 2 else \
        (todays[len(todays) // 2 - 1] + todays[len(todays) // 2]) / 2
    return latest, mid, len(todays)


def load_latest_term(gpu):
    if not TERM_CSV.exists():
        return None
    rows = [r for r in csv.DictReader(open(TERM_CSV))
            if r["gpu_model"] == gpu and r["tenor"] == "1y"]
    if not rows:
        return None
    return max(rows, key=lambda r: r["date"])


def main():
    gpu = sys.argv[1].upper() if len(sys.argv) > 1 else "H100"
    spot_date, spot, n = load_latest_spot(gpu)
    print(f"{gpu} spot (median of {n} provider series, {spot_date}): "
          f"${spot:.2f}/gpu-hr")

    term = load_latest_term(gpu)
    if not term:
        print(f"No 1y term rate for {gpu} in {TERM_CSV.name} - "
              f"add a row to compute the spread.")
        return

    t = float(term["rate_usd_per_gpu_hr"])
    spread = spot - t
    pct = spread / t * 100
    print(f"{gpu} 1y contract ({term['date']}, source: {term['source']}): "
          f"${t:.2f}/gpu-hr")
    print(f"Spread: {spread:+.2f} ({pct:+.1f}%)")
    if spread > 0:
        print("=> BACKWARDATION: spot above term. Demand-constrained market.")
    else:
        print("=> CONTANGO: spot below term. Supply outrunning spot demand; "
              "sellers discounting to fill capacity.")


if __name__ == "__main__":
    main()
