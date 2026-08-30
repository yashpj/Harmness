---
name: portfolio-drift
description: Measure a portfolio against its target allocation and size the trades that would restore it. Use whenever the user asks about drift, allocation, rebalancing, or whether their portfolio is off target. Requires current prices and a target allocation.
---

# Portfolio drift

Measures how far a portfolio has moved from its target allocation and sizes
the trades that would bring it back.

## Never compute this yourself

Do not calculate weights, drift, or trade sizes in your own reasoning, and do
not write your own version of this maths in a script. Run `drift.py`. It is
deterministic, tested, and applies the rebalancing policy correctly. Your job
is to gather inputs, run it, and explain the output.

## Running it

The script reads one JSON object on stdin and writes one on stdout.

```bash
echo '<input json>' | python3 drift.py
```

Get prices from the `financemcp` server's `get_prices` tool in the same
sandbox script. Never state a price from memory.

## Input

```json
{
  "cash": 3000,
  "targets":  {"VTI": 0.50, "BND": 0.30, "VXUS": 0.20},
  "prices":   {"VTI": 379.36, "BND": 72.31, "VXUS": 87.52},
  "holdings": {"VTI": {"shares": 40}, "BND": {"shares": 100},
               "VXUS": {"shares": 20}},
  "policy": {"abs_band_pp": 5.0, "rel_band_pct": 25.0,
             "deploy_cash": true, "allow_fractional": true,
             "min_trade_value": 50.0}
}
```

Targets are **decimals that sum to 1.0**, not percentages. 0.50, not 50.

A holding is given as `shares` or `market_value`, never both.

## The cash question — ask, do not assume

`deploy_cash` decides whether uninvested cash counts toward the allocation
base. It changes every number in the output, so it is the user's call, not
yours.

- `true` — cash is part of the portfolio and should be invested. Drift is
  measured against total value including cash.
- `false` — cash is set aside (emergency fund, upcoming expense) and sits
  outside the allocation.

If the user holds cash and has not said which they want, **ask before
running**. Do not pick a default silently. On a portfolio with 11% cash the
two settings can differ by 7 percentage points of drift on a single holding.

## The rebalancing policy

A holding breaches when it drifts more than `abs_band_pp` percentage points
from target, **or** more than `rel_band_pct` of its own target weight —
whichever binds first. This is the 5/25 rule.

The relative band exists to catch small sleeves. A 4% target that drifts to
6% is only 2 percentage points absolute but a 50% relative move, and a pure
absolute threshold misses it entirely.

## Reading the output

- `positions[].drift_pp` — percentage points from target. Signed.
- `positions[].breached` and `breach_reason` — which band tripped.
- `positions[].delta_shares` — shares to buy (positive) or sell (negative).
- `summary.turnover_pct` — share of the portfolio that changes hands.
- `summary.rebalance_required` — true if any holding breached.
- `warnings` — read these aloud to the user. They cover things like buys
  exceeding available cash.

On `{"ok": false, ...}` report the `error` message plainly and fix the input.
Do not work around a validation failure by computing the answer yourself.

## What this is not

Analysis, not advice. The output is a set of candidate trades for a person to
review. Do not tell the user what they should do with their money, and do not
describe the result as a recommendation.

This skill cannot place trades. Nothing here touches a brokerage.