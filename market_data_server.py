"""
Market data MCP server for the Portfolio Optimization Desk.

Three layers, deliberately separated:

  1. fetch_prices / fetch_history  -- pure functions, no MCP imports.
     Testable with plain pytest, runnable from a REPL.
  2. @server.tool wrappers         -- declare schemas, hand off to layer 1.
  3. run()                         -- transport.

Design rules learned the hard way this week:
  * Batch at the MCP boundary. One tool call returning N prices costs one
    model round-trip; N tool calls cost N. Upstream fetches stay serial so
    that a single bad ticker produces a per-symbol error instead of
    failing the batch.
  * Keep schemas boring. A list of strings and an optional string. No
    `const`, no nullable unions -- those are exactly what broke tool
    calling against Gemini's OpenAI compatibility layer.
  * Partial success over total failure. One bad ticker returns an error
    entry for that ticker; the rest still come back. An agent can work
    with that. An exception kills the whole run.

Run:
    python market_data_server.py            # http://localhost:8000/mcp
    python market_data_server.py --stdio    # for MCP Inspector

Register in TrueForge: Settings -> Connectors -> add by URL
    http://localhost:8000/mcp
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

import yfinance as yf
from mcp.server.mcpserver import MCPServer
import math
import json
from pathlib import Path

VALID_PERIODS = ("1mo", "3mo", "6mo", "1y", "2y", "5y")
MAX_TICKERS = 25
PLANS_FILE = Path("saved_plans.json")

# --------------------------------------------------------------------------
# Layer 1: pure functions. No MCP here on purpose -- these are what the
# tests import, and what you can poke at in a REPL when something looks off.
# --------------------------------------------------------------------------

def _normalise(tickers: list[str]) -> list[str]:
    """Uppercase, strip, drop blanks, de-duplicate, preserve order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in tickers:
        symbol = str(raw).strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def fetch_prices(tickers: list[str]) -> dict[str, Any]:
    """
    Latest price for each ticker.

    Returns {"as_of", "prices", "errors"}. A ticker that fails lands in
    `errors` and is simply absent from `prices` -- callers get everything
    that did work.
    """
    symbols = _normalise(tickers)
    if not symbols:
        return {"as_of": _now(), "prices": {}, "errors": {"_": "no tickers supplied"}}
    if len(symbols) > MAX_TICKERS:
        return {
            "as_of": _now(),
            "prices": {},
            "errors": {"_": f"too many tickers ({len(symbols)}), max {MAX_TICKERS}"},
        }

    prices: dict[str, float] = {}
    errors: dict[str, str] = {}

    for symbol in symbols:
        try:
            info = yf.Ticker(symbol).fast_info
            price = info.get("lastPrice") or info.get("last_price")

            if price is None:
                # fast_info can come back empty for some instruments;
                # fall back to the last close from a short history pull.
                frame = yf.Ticker(symbol).history(period="5d")
                if frame.empty:
                    errors[symbol] = "no data returned (delisted or bad symbol?)"
                    continue
                price = float(frame["Close"].iloc[-1])

            price = float(price)
            if not math.isfinite(price) or price <= 0:
                errors[symbol] = f"non-positive price: {price}"
                continue
            prices[symbol] = round(price, 4)

        except Exception as exc:  # noqa: BLE001 -- upstream throws many types
            errors[symbol] = f"{type(exc).__name__}: {exc}"

    return {"as_of": _now(), "prices": prices, "errors": errors}


def fetch_history(tickers: list[str], period: str = "1y") -> dict[str, Any]:
    """
    Daily closing prices over `period`, aligned across tickers.

    Feeds the optimisation step: returns are computed in the sandbox from
    these closes, not here. This function fetches; it does not analyse.
    """
    symbols = _normalise(tickers)
    if not symbols:
        return {"as_of": _now(), "period": period, "closes": {}, "errors": {"_": "no tickers supplied"}}
    if period not in VALID_PERIODS:
        return {
            "as_of": _now(),
            "period": period,
            "closes": {},
            "errors": {"_": f"period must be one of {', '.join(VALID_PERIODS)}"},
        }

    if len(symbols) > MAX_TICKERS:
        return {
            "as_of": _now(),
            "period": period,
            "closes": {},
            "errors": {"_": f"too many tickers ({len(symbols)}), max {MAX_TICKERS}"},
        }

    closes: dict[str, dict[str, float]] = {}
    errors: dict[str, str] = {}

    for symbol in symbols:
        try:
            frame = yf.Ticker(symbol).history(period=period)
            if frame.empty:
                errors[symbol] = "no history returned"
                continue
            series = frame["Close"].dropna()
            closes[symbol] = {
                stamp.strftime("%Y-%m-%d"): round(float(value), 4)
                for stamp, value in series.items()
                if math.isfinite(float(value))
            }
        except Exception as exc:  # noqa: BLE001
            errors[symbol] = f"{type(exc).__name__}: {exc}"

    trading_days = {sym: len(vals) for sym, vals in closes.items()}
    return {
        "as_of": _now(),
        "period": period,
        "closes": closes,
        "trading_days": trading_days,
        "errors": errors,
    }

def save_plan(label: str, plan: dict[str, Any]) -> dict[str, Any]:
    try:
        existing = json.loads(PLANS_FILE.read_text()) if PLANS_FILE.exists() else []
    except json.JSONDecodeError:
        existing = []

    record = {"label": label, "saved_at": _now(), "plan": plan}
    existing.append(record)
    PLANS_FILE.write_text(json.dumps(existing, indent=2))
    return {"saved": True, "label": label, "saved_at": record["saved_at"],
            "total_plans": len(existing)}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Layer 2: the MCP surface.
#
# The docstrings below are NOT for you. They are injected into the model's
# context and are the entire basis on which it decides to call these tools.
# Say what the tool does AND when to reach for it.
# --------------------------------------------------------------------------

server = MCPServer(
    name="financemcp",
    instructions=(
        "Live public market data for portfolio analysis. Prices are delayed "
        "and for analysis only. This server is read-only: it cannot place, "
        "modify, or cancel any trade."
    ),
)


@server.tool()
def get_prices(tickers: list[str]) -> dict[str, Any]:
    """Get the latest market price for one or more tickers.

    Always use this tool for prices. Never state a price from memory --
    your training data is stale and prices change every second.

    Pass every ticker you need in a single call: get_prices(["VTI","BND"]),
    not one call per ticker.

    Returns prices keyed by ticker, plus an errors map for any that failed.
    Symbols that fail do not prevent the others from returning.
    """
    return fetch_prices(tickers)


@server.tool()
def get_history(tickers: list[str], period: str = "1y") -> dict[str, Any]:
    """Get daily closing prices over a period, for volatility and correlation work.

    Use when you need historical series -- computing returns, volatility,
    covariance, or a Sharpe ratio. For a single current price, use
    get_prices instead; this returns far more data than you need for that.

    period must be one of: 1mo, 3mo, 6mo, 1y, 2y, 5y. Defaults to 1y.

    Returns closes as {ticker: {"YYYY-MM-DD": price}}. Do not compute
    statistics yourself -- pass this data to the sandbox and calculate
    there.
    """
    return fetch_history(tickers, period)

@server.tool()
def save_rebalance_plan(label: str, plan: dict[str, Any]) -> dict[str, Any]:
    """Save an approved rebalance plan to the user's records.

    Call this ONLY after the user has explicitly approved the specific
    trades. Pass the full drift.py output as `plan` so the saved record
    includes the prices and drift figures the decision was based on.

    This writes to the user's records and is not silently reversible.
    """
    return save_plan(label, plan)

# --------------------------------------------------------------------------
# Layer 3: transport.
# --------------------------------------------------------------------------

# def main() -> None:
#     parser = argparse.ArgumentParser(description="Market data MCP server")
#     parser.add_argument("--stdio", action="store_true", help="run over stdio (MCP Inspector)")
#     parser.add_argument("--port", type=int, default=8000)
#     args = parser.parse_args()

#     if args.stdio:
#         server.run(transport="stdio")
#     else:
#         server.settings.port = args.port
#         print(f"market-data MCP server on http://localhost:{args.port}/mcp")
#         server.run(transport="streamable-http")

def main() -> None:
    parser = argparse.ArgumentParser(description="Market data MCP server")
    parser.add_argument("--stdio", action="store_true", help="run over stdio (MCP Inspector)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.stdio:
        server.run(transport="stdio")
    else:
        print(f"market-data MCP server on http://localhost:{args.port}/mcp")
        server.run(transport="streamable-http", port=args.port)

if __name__ == "__main__":
    main()