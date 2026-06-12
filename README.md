# GPU Rental Price Tracker

Daily scraper for posted GPU rental rates (H100 / H200 / B200 / A100),
normalized to USD per GPU-hour, plus a spot-vs-term spread calculator.

## What this measures (and doesn't)

This tracks **posted on-demand/spot marketplace rates** — the marginal,
thin-tail price of compute. Most capacity trades on 1–5 year take-or-pay
contracts, which are NOT observable here. Use `spread.py` to compare spot
against term rates you enter manually (from SemiAnalysis, broker quotes,
or neocloud earnings disclosures).

Spread interpretation:
- **Spot > 1y term (backwardation)** → demand-constrained market
- **Spot < 1y term (contango)** → supply outrunning spot demand

## Quick start (local)

```bash
pip install -r requirements.txt
python scraper.py          # appends to data/gpu_prices.csv
python spread.py H100      # computes spread vs manual term rates
```

## Automated daily runs (GitHub Actions)

1. Create a new GitHub repo and push this folder.
2. The workflow in `.github/workflows/daily.yml` runs at 9:30am ET daily
   and commits new rows to `data/gpu_prices.csv`.
3. Optional secrets (Settings → Secrets and variables → Actions):
   - `LAMBDA_API_KEY` — free Lambda Labs account → API key
   - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — for p5/p6 spot prices

No secrets are required for the Vast.ai and RunPod modules.

## Providers

| Provider | Auth | Endpoint | Notes |
|---|---|---|---|
| Vast.ai | none | `console.vast.ai/api/v0/bundles` | Marketplace median; community hardware — cheapest tier |
| RunPod | none* | `api.runpod.io/graphql` | Secure vs community tiers logged separately |
| Lambda | API key | `cloud.lambdalabs.com/api/v1/instance-types` | Posted on-demand, datacenter grade |
| AWS | creds | EC2 `describe_spot_price_history` | True spot auction prices, p5/p5e/p6-b200 |

*RunPod's public pricing query has historically worked without a key; if it
starts returning auth errors, add `RUNPOD_API_KEY` as a secret.

## Caveats

1. **Endpoints can change.** These are undocumented-or-lightly-documented
   public APIs. If a module starts failing, check the provider's current
   API docs — the scraper logs failures per-provider and continues.
2. **Tiers are not comparable.** Vast.ai/RunPod community listings are
   consumer-grade hardware and trade well below dedicated datacenter
   capacity. The `tier` column exists so you never average across them.
   `spread.py` excludes community tiers automatically.
3. **Posted ≠ transacted.** Hyperscaler list prices especially are 2–3x
   what large customers pay. This series is best used for *direction and
   spread*, not absolute level.
4. **Seed term rate is a placeholder.** `data/manual_term_rates.csv`
   contains one approximate entry — replace with verified figures before
   computing spreads you'd act on.

## Reference series to track alongside

- Silicon Data indices on Bloomberg: `SDH100RT Index`, `SDA100RT Index`,
  `SDB200RT Index` (daily, methodology-normalized)
- SemiAnalysis GPU rental price index (term structure: on-demand to 5y)
