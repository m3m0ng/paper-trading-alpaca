"""Read-only end-of-day reconciliation and reporting.

`python -m bot.reconcile` fetches the paper account, positions, open orders,
today's closed orders, and the broker P/L history, then writes a JSON audit
under `logs/`. It NEVER submits an order: there is no order-submission code
path in this module.

Every P/L figure is explicitly labeled as Alpaca PAPER (simulated fills), and
every fill-cost figure is explicitly labeled as MODELED by this bot (fill
price vs IEX daily close) — not an actual Alpaca or live-market fee.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.strategy import SYMBOLS

EASTERN = ZoneInfo("America/New_York")

CONSISTENCY_TOLERANCE_USD = 0.01

PAPER_PL_LABEL = (
    "Alpaca PAPER account P/L: simulated fills; excludes real fees, slippage, "
    "market impact, dividends timing, and taxes. Not a live-trading result."
)
MODELED_COST_LABEL = (
    "MODELED costs computed by this bot: |fill price - IEX daily close| x filled qty. "
    "NOT actual Alpaca or live-market fees. filled_avg_price collapses partial fills "
    "into one average, so per-fill slippage is an approximation."
)


def _filled_date_et(order: dict) -> date | None:
    filled_at = order.get("filled_at")
    if not filled_at:
        return None
    return datetime.fromisoformat(filled_at.replace("Z", "+00:00")).astimezone(EASTERN).date()


def model_order_costs(
    orders: list[dict], reference_closes: dict[tuple[str, str], float]
) -> tuple[list[dict], float]:
    """MODELED slippage per order: positive means the fill was worse than the daily close."""
    modeled: list[dict] = []
    total_cost = 0.0
    for order in orders:
        qty = float(order.get("filled_qty") or 0)
        avg_price = order.get("filled_avg_price")
        if qty <= 0 or avg_price is None:
            continue
        symbol = order.get("symbol", "")
        side = order.get("side", "")
        fill_day = _filled_date_et(order)
        reference = reference_closes.get((symbol, fill_day.isoformat())) if fill_day else None
        entry: dict = {
            "order_id": order.get("id"),
            "client_order_id": order.get("client_order_id"),
            "symbol": symbol,
            "side": side,
            "filled_qty": qty,
            "filled_avg_price": float(avg_price),
            "fill_date_et": fill_day.isoformat() if fill_day else None,
        }
        if reference is not None and side in {"buy", "sell"}:
            # Buy: paying above close is unfavorable. Sell: receiving below close is unfavorable.
            slippage_per_share = (float(avg_price) - reference) if side == "buy" else (reference - float(avg_price))
            cost = abs(slippage_per_share) * qty
            total_cost += cost
            entry.update(
                {
                    "reference_close": reference,
                    "reference_source": "IEX daily bar close (adjusted), same trading day",
                    "modeled_slippage_per_share": round(slippage_per_share, 6),
                    "modeled_cost": round(cost, 4),
                    "sign_convention": "positive slippage = unfavorable to the account",
                }
            )
        else:
            entry.update(
                {
                    "reference_close": reference,
                    "modeled_slippage_per_share": None,
                    "modeled_cost": None,
                    "note": "No same-day IEX close available; no cost modeled.",
                }
            )
        modeled.append(entry)
    return modeled, total_cost


def build_reconciliation(
    *,
    now_et: datetime,
    account: dict,
    positions: list[dict],
    open_orders: list[dict],
    closed_orders: list[dict],
    portfolio_history: dict,
    reference_closes: dict[tuple[str, str], float],
    market_open: bool,
) -> dict:
    """Assemble the audit dict. Pure: no network, no clock, no filesystem."""
    equity = float(account["equity"])
    cash = float(account["cash"])
    last_equity = float(account["last_equity"])

    position_rows = []
    position_value = 0.0
    long_only_ok = True
    untracked_symbols = []
    for position in positions:
        symbol = position.get("symbol", "")
        qty = float(position.get("qty") or 0)
        market_value = float(position.get("market_value") or 0)
        position_value += market_value
        if qty < 0:
            long_only_ok = False
        if symbol not in SYMBOLS:
            # Untracked holdings can never be traded by this bot; they must halt
            # any future execution run (AGENTS.md tracked-ownership rule).
            untracked_symbols.append(symbol)
        position_rows.append(
            {"symbol": symbol, "qty": qty, "side": position.get("side"), "market_value": market_value}
        )

    cash_position_gap = abs(equity - (cash + position_value))

    history_points = [point for point in (portfolio_history.get("profit_loss") or []) if point is not None]
    broker_daily_pl = history_points[-1] if history_points else None

    modeled_costs, total_modeled_cost = model_order_costs(closed_orders, reference_closes)

    return {
        "mode": "reconciliation",
        "read_only": True,
        "submitted_orders": [],
        "timestamp_et": now_et.isoformat(),
        "market_state": {
            "open": market_open,
            "note": (
                "Market was open: today's bar and P/L may be incomplete; rerun after close."
                if market_open
                else "Market closed per Alpaca clock."
            ),
        },
        "account": {
            "status": account.get("status"),
            "equity": equity,
            "cash": cash,
            "last_equity": last_equity,
            "trading_blocked": account.get("trading_blocked"),
            "account_blocked": account.get("account_blocked"),
        },
        "broker_reported": {
            "label": PAPER_PL_LABEL,
            "daily_profit_loss_from_history": broker_daily_pl,
            "equity_minus_last_equity": round(equity - last_equity, 2),
            "source": "Alpaca /v2/account and /v2/account/portfolio/history (paper environment)",
        },
        "positions": position_rows,
        "consistency": {
            "position_value_sum": round(position_value, 2),
            "cash": cash,
            "cash_plus_positions": round(cash + position_value, 2),
            "equity": equity,
            "absolute_gap_usd": round(cash_position_gap, 2),
            "tolerance_usd": CONSISTENCY_TOLERANCE_USD,
            "is_consistent": cash_position_gap <= CONSISTENCY_TOLERANCE_USD,
            "long_only_ok": long_only_ok,
            "untracked_symbols_outside_strategy": untracked_symbols,
            "note": "Any inconsistency, short position, or untracked symbol must halt future order execution.",
        },
        "open_orders": [{"id": order.get("id"), "symbol": order.get("symbol"), "side": order.get("side")} for order in open_orders],
        "modeled_fill_costs": {
            "label": MODELED_COST_LABEL,
            "orders": modeled_costs,
            "total_modeled_cost": round(total_modeled_cost, 4),
        },
    }


def _reference_closes(client, orders: list[dict], day: date) -> dict[tuple[str, str], float]:
    """Same-day IEX daily closes for every filled symbol, used as the cost reference."""
    symbols = sorted({order.get("symbol") for order in orders if order.get("filled_qty") and order.get("symbol")})
    if not symbols:
        return {}
    bars = client.daily_bars(symbols, day, day)
    closes: dict[tuple[str, str], float] = {}
    for symbol, rows in bars.items():
        for bar in rows:
            day = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(EASTERN).date()
            closes[(symbol, day.isoformat())] = float(bar["c"])
    return closes


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only end-of-day reconciliation: writes a JSON audit with broker-reported "
            "paper P/L and MODELED fill costs. Submits no order."
        )
    )
    parser.parse_args()

    from bot.alpaca import AlpacaClient
    from bot.config import Settings

    settings = Settings.load()
    client = AlpacaClient(settings)

    clock = client.clock()
    now_et = datetime.fromisoformat(clock["timestamp"].replace("Z", "+00:00")).astimezone(EASTERN)
    today = now_et.date()

    account = client.account()
    positions = client.positions()
    open_orders = client.open_orders()
    closed = client.closed_orders(today)
    history = client.portfolio_history(period="1D", timeframe="5Min")
    reference_closes = _reference_closes(client, closed, today)

    report = build_reconciliation(
        now_et=now_et,
        account=account,
        positions=positions,
        open_orders=open_orders,
        closed_orders=closed,
        portfolio_history=history,
        reference_closes=reference_closes,
        market_open=bool(clock.get("is_open")),
    )

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"reconcile-{now_et.strftime('%Y%m%dT%H%M%S%z')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    consistency = report["consistency"]
    print(f"RECONCILIATION complete (read-only, 0 orders submitted). Audit: {path}")
    print(
        f"  cash+positions={consistency['cash_plus_positions']:.2f} vs equity={consistency['equity']:.2f} "
        f"consistent={consistency['is_consistent']}"
    )
    print(
        f"  paper daily P/L (broker): {report['broker_reported']['daily_profit_loss_from_history']} | "
        f"modeled fill costs: {report['modeled_fill_costs']['total_modeled_cost']}"
    )
    alerts = []
    if not consistency["is_consistent"]:
        alerts.append("cash/position/equity mismatch")
    if not consistency["long_only_ok"]:
        alerts.append("non-long position detected")
    if consistency["untracked_symbols_outside_strategy"]:
        alerts.append(f"untracked symbols: {consistency['untracked_symbols_outside_strategy']}")
    if report["open_orders"]:
        alerts.append(f"{len(report['open_orders'])} open order(s)")
    if alerts:
        print("  ALERTS (halt future execution until resolved): " + "; ".join(alerts))


if __name__ == "__main__":
    main()
