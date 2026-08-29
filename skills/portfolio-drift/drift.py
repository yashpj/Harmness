#!/usr/bin/env python3
"""
Portfolio drift calculator.

Contract:
    stdin  -> one JSON object (see INPUT SCHEMA below)
    stdout -> one JSON object (see OUTPUT SCHEMA below)
    exit 0 on success, exit 1 with {"ok": false, "error": ...} on bad input

Deliberately stdlib-only and offline. Prices are supplied by the caller
(the MCP market-data server), never fetched here. That keeps this module
deterministic, unit-testable, and safe to run in an untrusted sandbox.

INPUT SCHEMA
{
  "as_of":    "2026-08-26T14:00:00Z",     # optional, echoed back
  "cash":     2500.0,                      # optional, default 0
  "targets":  {"VTI": 0.6, "BND": 0.4},    # required, weights must sum to ~1
  "prices":   {"VTI": 289.12, "BND": 72.4},# required, > 0
  "holdings": {"VTI": {"shares": 40},      # shares OR market_value per symbol
               "BND": {"market_value": 5000.0}},
  "policy": {
    "abs_band_pp":         5.0,   # absolute band, percentage points
    "rel_band_pct":        25.0,  # relative band, % of target weight
    "allow_fractional":    true,  # false -> whole-share trade sizing
    "deploy_cash":         true,  # include cash in the rebalance base
    "min_trade_value":     50.0   # suppress dust trades
  }
}

OUTPUT SCHEMA
{
  "ok": true,
  "as_of": ...,
  "totals": {"invested": ..., "cash": ..., "base": ...},
  "policy": {...echoed, with defaults filled in...},
  "positions": [
    {"symbol": "VTI", "shares": 40.0, "price": 289.12, "market_value": 11564.8,
     "current_weight": 0.6981, "target_weight": 0.6, "drift_pp": 9.81,
     "drift_relative_pct": 16.35, "breached": true, "breach_reason": "abs_band",
     "action": "SELL", "delta_value": -1625.3, "delta_shares": -5.62,
     "post_trade_weight": 0.6}
  ],
  "summary": {"total_drift_pp": ..., "max_drift_pp": ..., "turnover_pct": ...,
              "rebalance_required": true, "breached_symbols": ["VTI"]},
  "warnings": []
}

The 5/25 rule (Larry Swedroe): rebalance when a holding drifts more than
5 percentage points absolute OR more than 25% of its own target weight,
whichever binds first. The relative band is what catches small sleeves --
a 4% target that drifts to 6% is a 50% relative move but only 2pp absolute.
"""

import json
import sys

WEIGHT_SUM_TOLERANCE = 1e-4

DEFAULT_POLICY = {
    "abs_band_pp": 5.0,
    "rel_band_pct": 25.0,
    "allow_fractional": True,
    "deploy_cash": True,
    "min_trade_value": 0.0,
}


class InputError(ValueError):
    """Raised on malformed input so the agent gets a clean message, not a traceback."""


def _validate(payload):
    if not isinstance(payload, dict):
        raise InputError("payload must be a JSON object")

    targets = payload.get("targets")
    prices = payload.get("prices")
    if not isinstance(targets, dict) or not targets:
        raise InputError("'targets' must be a non-empty object of symbol -> weight")
    if not isinstance(prices, dict):
        raise InputError("'prices' must be an object of symbol -> price")

    weight_sum = 0.0
    for symbol, weight in targets.items():
        if not isinstance(weight, (int, float)) or isinstance(weight, bool):
            raise InputError(f"target weight for {symbol} is not a number")
        if weight < 0:
            raise InputError(f"target weight for {symbol} is negative")
        weight_sum += float(weight)
    if abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise InputError(f"target weights sum to {weight_sum:.6f}, expected 1.0")

    missing = sorted(set(targets) - set(prices))
    if missing:
        raise InputError(f"no price supplied for: {', '.join(missing)}")

    for symbol, price in prices.items():
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            raise InputError(f"price for {symbol} is not a number")
        if price <= 0:
            raise InputError(f"price for {symbol} must be > 0, got {price}")

    cash = payload.get("cash", 0.0)
    if not isinstance(cash, (int, float)) or isinstance(cash, bool):
        raise InputError("'cash' must be a number")
    if cash < 0:
        raise InputError("'cash' must not be negative")


def _resolve_policy(raw):
    policy = dict(DEFAULT_POLICY)
    if raw is None:
        return policy
    if not isinstance(raw, dict):
        raise InputError("'policy' must be an object")
    unknown = sorted(set(raw) - set(DEFAULT_POLICY))
    if unknown:
        raise InputError(f"unknown policy keys: {', '.join(unknown)}")
    policy.update(raw)
    return policy


def _market_value(entry, price, symbol):
    """A holding is given as share count or as a market value, not both."""
    if entry is None:
        return 0.0, 0.0
    if not isinstance(entry, dict):
        raise InputError(f"holding for {symbol} must be an object")

    has_shares = "shares" in entry
    has_value = "market_value" in entry
    if has_shares and has_value:
        raise InputError(f"holding for {symbol}: give 'shares' or 'market_value', not both")
    if not has_shares and not has_value:
        raise InputError(f"holding for {symbol}: needs 'shares' or 'market_value'")

    if has_shares:
        shares = float(entry["shares"])
        if shares < 0:
            raise InputError(f"holding for {symbol}: shares must not be negative")
        return shares, shares * price

    value = float(entry["market_value"])
    if value < 0:
        raise InputError(f"holding for {symbol}: market_value must not be negative")
    return value / price, value


def calculate_drift(payload):
    _validate(payload)
    policy = _resolve_policy(payload.get("policy"))

    targets = payload["targets"]
    prices = payload["prices"]
    holdings = payload.get("holdings") or {}
    cash = float(payload.get("cash", 0.0))
    warnings = []

    untracked = sorted(set(holdings) - set(targets))
    if untracked:
        warnings.append(
            "held but not in target allocation, excluded from drift: " + ", ".join(untracked)
        )

    # --- current state -------------------------------------------------
    rows = []
    invested = 0.0
    for symbol in targets:
        price = float(prices[symbol])
        shares, value = _market_value(holdings.get(symbol), price, symbol)
        invested += value
        rows.append({"symbol": symbol, "shares": shares, "price": price, "market_value": value})

    base = invested + cash if policy["deploy_cash"] else invested

    if base <= 0:
        raise InputError("portfolio base value is zero; nothing to measure drift against")
    if not policy["deploy_cash"] and cash > 0:
        warnings.append(f"cash of {cash:.2f} held out of the rebalance base by policy")

    # --- drift and bands -----------------------------------------------
    positions = []
    total_abs_drift_pp = 0.0
    breached_symbols = []

    for row in rows:
        symbol = row["symbol"]
        target_weight = float(targets[symbol])
        current_weight = row["market_value"] / base
        drift_pp = (current_weight - target_weight) * 100.0

        if target_weight > 0:
            drift_relative_pct = (current_weight - target_weight) / target_weight * 100.0
        else:
            # Zero-target sleeve: any holding at all is a full breach.
            drift_relative_pct = float("inf") if current_weight > 0 else 0.0

        abs_breach = abs(drift_pp) > policy["abs_band_pp"]
        rel_breach = abs(drift_relative_pct) > policy["rel_band_pct"]
        breached = abs_breach or rel_breach
        if breached:
            breached_symbols.append(symbol)

        if abs_breach and rel_breach:
            reason = "abs_band+rel_band"
        elif abs_breach:
            reason = "abs_band"
        elif rel_breach:
            reason = "rel_band"
        else:
            reason = None

        total_abs_drift_pp += abs(drift_pp)

        # --- trade sizing back to target -------------------------------
        target_value = target_weight * base
        delta_value = target_value - row["market_value"]
        delta_shares = delta_value / row["price"]

        if not policy["allow_fractional"]:
            # Round toward zero so a rebalance never overshoots into an
            # unfunded buy or an oversold position.
            delta_shares = float(int(delta_shares))
            delta_value = delta_shares * row["price"]

        if abs(delta_value) < policy["min_trade_value"]:
            delta_value = 0.0
            delta_shares = 0.0

        if delta_value > 0:
            action = "BUY"
        elif delta_value < 0:
            action = "SELL"
        else:
            action = "HOLD"

        positions.append({
            "symbol": symbol,
            "shares": round(row["shares"], 6),
            "price": round(row["price"], 4),
            "market_value": round(row["market_value"], 2),
            "current_weight": round(current_weight, 6),
            "target_weight": round(target_weight, 6),
            "drift_pp": round(drift_pp, 4),
            "drift_relative_pct": (
                round(drift_relative_pct, 4) if drift_relative_pct != float("inf") else None
            ),
            "breached": breached,
            "breach_reason": reason,
            "action": action,
            "delta_value": round(delta_value, 2),
            "delta_shares": round(delta_shares, 6),
            "post_trade_weight": round((row["market_value"] + delta_value) / base, 6),
        })

    # Turnover: half the sum of absolute deviations, i.e. the share of the
    # portfolio that changes hands in a full rebalance.
    turnover_pct = total_abs_drift_pp / 2.0
    max_drift_pp = max((abs(p["drift_pp"]) for p in positions), default=0.0)

    net_trade_value = sum(p["delta_value"] for p in positions)
    if policy["deploy_cash"] and net_trade_value > cash + 0.01:
        warnings.append(
            f"proposed buys exceed available cash by {net_trade_value - cash:.2f}; "
            "sells must settle first"
        )

    return {
        "ok": True,
        "as_of": payload.get("as_of"),
        "totals": {
            "invested": round(invested, 2),
            "cash": round(cash, 2),
            "base": round(base, 2),
        },
        "policy": policy,
        "positions": positions,
        "summary": {
            "total_drift_pp": round(total_abs_drift_pp, 4),
            "max_drift_pp": round(max_drift_pp, 4),
            "turnover_pct": round(turnover_pct, 4),
            "rebalance_required": bool(breached_symbols),
            "breached_symbols": breached_symbols,
        },
        "warnings": warnings,
    }


def main():
    raw = sys.stdin.read()
    try:
        if not raw.strip():
            raise InputError("no input received on stdin")
        result = calculate_drift(json.loads(raw))
    except InputError as exc:
        json.dump({"ok": False, "error": str(exc), "error_type": "input"}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "error": f"invalid JSON: {exc}", "error_type": "input"}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())