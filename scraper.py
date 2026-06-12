#!/usr/bin/env python3
"""
GPU Rental Spot Price Tracker
=============================
Pulls posted on-demand / spot GPU rental rates from public provider APIs,
normalizes to USD per GPU-hour, and appends to a daily CSV.

Providers (no API key required):
  - Vast.ai      (marketplace search API)
  - RunPod       (public GraphQL pricing endpoint)

Providers (optional, key required):
  - Lambda Labs  (set LAMBDA_API_KEY env var; free account)
  - AWS spot     (set AWS creds; uses boto3, p5/p6 instance families)

Output schema (data/gpu_prices.csv):
  date, provider, gpu_model, tier, price_usd_per_gpu_hr, n_offers, notes

NOTE: These are POSTED marketplace rates, i.e. the spot/on-demand tail of
the market - not transacted contract prices. Use alongside term-rate data
(see data/manual_term_rates.csv) to compute the spot-vs-term spread.
"""

import csv
import json
import os
import statistics
import sys
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import requests

DATA_DIR = Path(__file__).parent / "data"
OUT_CSV = DATA_DIR / "gpu_prices.csv"
TIMEOUT = 30
HEADERS = {"User-Agent": "gpu-price-tracker/1.0 (personal research)"}

# GPU models we care about, with per-provider name patterns
TARGET_GPUS = ["H100", "H200", "B200", "A100"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def median_or_none(values):
    vals = [v for v in values if v is not None and v > 0]
    return round(statistics.median(vals), 4) if vals else None


def make_row(provider, gpu_model, tier, price, n_offers, notes=""):
    return {
        "date": date.today().isoformat(),
        "provider": provider,
        "gpu_model": gpu_model,
        "tier": tier,
        "price_usd_per_gpu_hr": price,
        "n_offers": n_offers,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Vast.ai - public marketplace search
# Docs: https://docs.vast.ai (search endpoint historically open, no auth)
# Prices returned are per-instance; divide by num_gpus for per-GPU rate.
# ---------------------------------------------------------------------------

def fetch_vastai():
    rows = []
    base = "https://console.vast.ai/api/v0/bundles/"
    for gpu in TARGET_GPUS:
        query = {
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "gpu_name": {"in": [f"{gpu} SXM", f"{gpu} PCIE", f"{gpu} NVL", gpu]},
            "order": [["dph_total", "asc"]],
            "type": "on-demand",
            "limit": 200,
        }
        url = base + "?q=" + urllib.parse.quote(json.dumps(query))
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            offers = r.json().get("offers", [])
            per_gpu = []
            for o in offers:
                n = o.get("num_gpus") or 1
                dph = o.get("dph_total")
                if dph and n:
                    per_gpu.append(dph / n)
            med = median_or_none(per_gpu)
            if med:
                rows.append(make_row("vast.ai", gpu, "marketplace_on_demand",
                                     med, len(per_gpu),
                                     "median across verified rentable offers"))
        except Exception as e:
            print(f"[vast.ai] {gpu}: FAILED - {e}", file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# RunPod - public GraphQL pricing (gpuTypes query, no auth required
# historically; if it starts requiring a key, set RUNPOD_API_KEY)
# ---------------------------------------------------------------------------

RUNPOD_QUERY = """
query GpuTypes {
  gpuTypes {
    id
    displayName
    memoryInGb
    securePrice
    communityPrice
  }
}
"""

def fetch_runpod():
    rows = []
    url = "https://api.runpod.io/graphql"
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if api_key:
        url += f"?api_key={api_key}"
    try:
        r = requests.post(url, json={"query": RUNPOD_QUERY},
                          headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        gpu_types = r.json()["data"]["gpuTypes"]
    except Exception as e:
        print(f"[runpod] FAILED - {e}", file=sys.stderr)
        return rows

    for gpu in TARGET_GPUS:
        matches = [g for g in gpu_types
                   if gpu in (g.get("displayName") or "").upper()
                   or gpu in (g.get("id") or "").upper()]
        secure = median_or_none([g.get("securePrice") for g in matches])
        community = median_or_none([g.get("communityPrice") for g in matches])
        if secure:
            rows.append(make_row("runpod", gpu, "secure_cloud", secure,
                                 len(matches), "datacenter-grade tier"))
        if community:
            rows.append(make_row("runpod", gpu, "community_cloud", community,
                                 len(matches), "community tier - not comparable to dedicated"))
    return rows


# ---------------------------------------------------------------------------
# Lambda Labs - requires free API key (LAMBDA_API_KEY)
# ---------------------------------------------------------------------------

def fetch_lambda():
    rows = []
    key = os.environ.get("LAMBDA_API_KEY")
    if not key:
        print("[lambda] skipped (no LAMBDA_API_KEY set)", file=sys.stderr)
        return rows
    try:
        r = requests.get("https://cloud.lambdalabs.com/api/v1/instance-types",
                         auth=(key, ""), headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()["data"]
    except Exception as e:
        print(f"[lambda] FAILED - {e}", file=sys.stderr)
        return rows

    for name, info in data.items():
        it = info.get("instance_type", info)
        desc = (it.get("description") or name).upper()
        for gpu in TARGET_GPUS:
            if gpu in desc:
                cents = it.get("price_cents_per_hour")
                # gpu count is embedded in name like 'gpu_8x_h100_sxm5'
                n = 1
                for tok in name.split("_"):
                    if tok.endswith("x") and tok[:-1].isdigit():
                        n = int(tok[:-1])
                if cents:
                    rows.append(make_row("lambda", gpu, "on_demand",
                                         round(cents / 100 / n, 4), 1,
                                         f"instance {name}, {n}x GPUs"))
    return rows


# ---------------------------------------------------------------------------
# AWS spot - optional, requires boto3 + AWS credentials
# p5.48xlarge = 8x H100 | p5e/p5en.48xlarge = 8x H200 | p6-b200.48xlarge = 8x B200
# ---------------------------------------------------------------------------

AWS_INSTANCE_MAP = {
    "p5.48xlarge": ("H100", 8),
    "p5e.48xlarge": ("H200", 8),
    "p5en.48xlarge": ("H200", 8),
    "p6-b200.48xlarge": ("B200", 8),
}

def fetch_aws_spot():
    rows = []
    try:
        import boto3
    except ImportError:
        print("[aws] skipped (boto3 not installed)", file=sys.stderr)
        return rows
    if not (os.environ.get("AWS_ACCESS_KEY_ID") or
            Path.home().joinpath(".aws/credentials").exists()):
        print("[aws] skipped (no AWS credentials)", file=sys.stderr)
        return rows
    try:
        ec2 = boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        resp = ec2.describe_spot_price_history(
            InstanceTypes=list(AWS_INSTANCE_MAP.keys()),
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(timezone.utc),
        )
        by_type = {}
        for p in resp.get("SpotPriceHistory", []):
            by_type.setdefault(p["InstanceType"], []).append(float(p["SpotPrice"]))
        for itype, prices in by_type.items():
            gpu, n = AWS_INSTANCE_MAP[itype]
            med = median_or_none([p / n for p in prices])
            if med:
                rows.append(make_row("aws_spot", gpu, "spot", med, len(prices),
                                     f"{itype} across AZs in region"))
    except Exception as e:
        print(f"[aws] FAILED - {e}", file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    DATA_DIR.mkdir(exist_ok=True)
    all_rows = []
    for fn in (fetch_vastai, fetch_runpod, fetch_lambda, fetch_aws_spot):
        all_rows.extend(fn())

    if not all_rows:
        print("No data collected - all providers failed.", file=sys.stderr)
        sys.exit(1)

    write_header = not OUT_CSV.exists()
    with open(OUT_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        if write_header:
            writer.writeheader()
        writer.writerows(all_rows)

    print(f"Wrote {len(all_rows)} rows to {OUT_CSV}")
    for row in all_rows:
        print(f"  {row['provider']:>10} | {row['gpu_model']:>5} | "
              f"{row['tier']:<22} | ${row['price_usd_per_gpu_hr']}/gpu-hr "
              f"(n={row['n_offers']})")


if __name__ == "__main__":
    main()
