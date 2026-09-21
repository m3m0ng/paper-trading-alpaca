"""Pure, cost-aware backtest engine for the exact V1 rules.

The engine in this module is deterministic and network-free: it consumes
already-fetched adjusted daily bars. `main()` is the only code path that
talks to Alpaca, and it runs only when the user invokes the CLI
(`python -m bot.backtest`).

No look-ahead: every rebalance uses closes with dates strictly before the
trade date, and fills happen at the trade date's close plus modeled slippage.
Slippage is a MODELED cost, not an actual Alpaca or live-market fee.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import sqrt
from statistics import stdev
from zoneinfo import ZoneInfo

from bot.strategy import (
    BASE_WEIGHTS,
    DEFENSIVE,
    INTERNATIONAL_EQUITY,
    SYMBOLS,
    US_EQUITY,
    apply_volatility_cap,
    evaluate_signal,
    target_weights,
)

EASTERN = ZoneInfo("America/New_York")

VARIANTS = ("buy_and_hold", "monthly_trend", "monthly_trend_vol_cap")
DEFAULT_SLIPPAGE_BPS = (0.0, 5.0, 10.0, 25.0)

# evaluate_signal needs 253 completed bars; the 63-day volatility lookback is
# implied by that. Index 253 is the first day a full signal can be formed.
WARMUP_BARS = 253
# SGOV weight above the permanent 10% reserve means a defensive sleeve is active.
BASE_SGOV_WEIGHT = BASE_WEIGHTS[DEFENSIVE]
DISCLAIMERS = (
    "Slippage and trading costs are MODELED assumptions, not actual Alpaca or live-market fees.",
    "Bar data uses Alpaca's IEX feed, which is a subset of full-market consolidated data.",
    "Backtest results are historical simulations of a hypothesis; they are not investment advice or a return promise.",
    "Signals use only completed daily bars with dates strictly before the trade date (no look-ahead).",
)


@dataclass(frozen=True)
class BacktestConfig:
    variant: str
    slippage_bps: float
    initial_equity: float = 10_000.0
    target_annual_volatility: float = 0.10
    min_trade_notional: float = 1.0


class _State:
    """Cash plus fractional share units; equity is marked to close each day."""

    def __init__(self, initial_equity: float) -> None:
        self.cash = initial_equity
        self.units = {symbol: 0.0 for symbol in SYMBOLS}

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(self.units[symbol] * prices[symbol] for symbol in SYMBOLS)

    def weights(self, prices: dict[str, float]) -> dict[str, float]:
        equity = self.equity(prices)
        if equity <= 0:
            return {symbol: 0.0 for symbol in SYMBOLS}
        return {symbol: self.units[symbol] * prices[symbol] / equity for symbol in SYMBOLS}


def prepare_aligned_closes(
    bars_by_symbol: dict[str, list[dict]], as_of: date | None = None
) -> tuple[list[date], dict[str, list[float]]]:
    """Align symbols onto their common trading dates, keeping only completed bars.

    A bar dated on or after `as_of` is dropped, so a same-day (still open)
    candle can never enter the signal inputs.
    """
    maps: dict[str, dict[date, float]] = {}
    for symbol in SYMBOLS:
        rows: dict[date, float] = {}
        for bar in bars_by_symbol.get(symbol, []):
            day = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(EASTERN).date()
            if as_of is not None and day >= as_of:
                continue
            price = float(bar["c"])
            if price <= 0:
                raise ValueError(f"{symbol} has a non-positive close on {day}")
            rows[day] = price
        maps[symbol] = rows

    common: set[date] | None = None
    for symbol in SYMBOLS:
        days = set(maps[symbol])
        common = days if common is None else common & days
    dates = sorted(common or set())
    if len(dates) < WARMUP_BARS + 2:
        raise ValueError(f"Need at least {WARMUP_BARS + 2} aligned completed bars per symbol; got {len(dates)}")
    closes = {symbol: [maps[symbol][day] for day in dates] for symbol in SYMBOLS}
    return dates, closes


def run_backtest(
    bars_by_symbol: dict[str, list[dict]],
    *,
    variant: str,
    slippage_bps: float,
    initial_equity: float = 10_000.0,
    target_annual_volatility: float = 0.10,
    min_trade_notional: float = 1.0,
    as_of: date | None = None,
) -> dict:
    """Run one variant once. Pure: no network, no clock, no filesystem."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}")
    if slippage_bps < 0:
        raise ValueError("slippage_bps cannot be negative")
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    config = BacktestConfig(
        variant=variant,
        slippage_bps=slippage_bps,
        initial_equity=initial_equity,
        target_annual_volatility=target_annual_volatility,
        min_trade_notional=min_trade_notional,
    )

    dates, closes = prepare_aligned_closes(bars_by_symbol, as_of)
    dates = list(dates)
    state = _State(initial_equity)
    # Warmup bars generate signals but are not portfolio performance. All variants
    # begin with the same first investable trade date for a fair comparison.
    evaluation_dates: list[date] = []
    equity_curve: list[float] = []
    sgov_weights: list[float] = []
    trades: list[dict] = []
    total_traded_notional = 0.0
    total_modeled_cost = 0.0
    started = False  # buy-and-hold trades exactly once; trend variants start once, then monthly
    min_cash_balance = float("inf")

    for index, day in enumerate(dates):
        prices = {symbol: closes[symbol][index] for symbol in SYMBOLS}
        due = index >= WARMUP_BARS and (
            not started
            or (variant != "buy_and_hold" and day.month != dates[index - 1].month)
        )
        if due:
            started = True
            # closes[:index] ends at dates[index - 1]: strictly completed data.
            if variant == "buy_and_hold":
                targets = dict(BASE_WEIGHTS)
                signal_through: date | None = None
            else:
                signal_through_date = dates[index - 1]
                signals = {
                    symbol: evaluate_signal(symbol, closes[symbol][:index])
                    for symbol in (US_EQUITY, INTERNATIONAL_EQUITY)
                }
                targets = target_weights(signals)
                if variant == "monthly_trend_vol_cap":
                    targets, _, _ = apply_volatility_cap(
                        targets,
                        {
                            US_EQUITY: closes[US_EQUITY][:index],
                            INTERNATIONAL_EQUITY: closes[INTERNATIONAL_EQUITY][:index],
                        },
                        config.target_annual_volatility,
                    )
                signal_through = signal_through_date

            equity_now = state.equity(prices)
            # All deltas come from the same pre-trade snapshot.
            deltas = {
                symbol: equity_now * targets[symbol] - state.units[symbol] * prices[symbol]
                for symbol in SYMBOLS
            }

            def execute(symbol: str, delta: float, signal_through: date | None) -> None:
                nonlocal total_traded_notional, total_modeled_cost
                # The modeled fee is always a cost: buys pay delta+fee, sells
                # receive |delta|-fee.
                fee = abs(delta) * config.slippage_bps / 10_000
                state.cash -= delta + fee
                state.units[symbol] += delta / prices[symbol]
                total_traded_notional += abs(delta)
                total_modeled_cost += abs(fee)
                trades.append(
                    {
                        "date": day.isoformat(),
                        "symbol": symbol,
                        "side": "buy" if delta > 0 else "sell",
                        "notional": round(abs(delta), 2),
                        "price": round(prices[symbol], 6),
                        "modeled_slippage_cost": round(abs(fee), 4),
                        "signal_through_date": signal_through.isoformat() if signal_through else None,
                    }
                )

            # Sells first: their (modeled) proceeds are available to buys at the
            # same close fill.
            for symbol in SYMBOLS:
                if deltas[symbol] <= -config.min_trade_notional:
                    execute(symbol, deltas[symbol], signal_through)

            # Buys scale down to cash on hand (buy + modeled cost must fit in
            # the remaining balance), so slippage can never push cash negative.
            budget = max(0.0, state.cash)
            slip = config.slippage_bps / 10_000
            for symbol in SYMBOLS:
                delta = deltas[symbol]
                if delta < config.min_trade_notional:
                    continue
                delta = min(delta, budget / (1 + slip))
                if delta < config.min_trade_notional:
                    continue
                execute(symbol, delta, signal_through)
                budget -= delta * (1 + slip)

        if started:
            equity = state.equity(prices)
            evaluation_dates.append(day)
            equity_curve.append(equity)
            min_cash_balance = min(min_cash_balance, state.cash)
            sgov_weights.append(state.weights(prices)[DEFENSIVE])

    metrics = _metrics(
        evaluation_dates,
        equity_curve,
        initial_equity,
        total_traded_notional,
        total_modeled_cost,
        sgov_weights,
        len(trades),
        min_cash_balance,
    )
    return {
        "variant": variant,
        "slippage_bps": slippage_bps,
        "metrics": metrics,
        "trades": trades,
    }


def _metrics(
    dates: list[date],
    equity_curve: list[float],
    initial_equity: float,
    total_traded_notional: float,
    total_modeled_cost: float,
    sgov_weights: list[float],
    trade_count: int,
    min_cash_balance: float,
) -> dict:
    if not equity_curve:
        raise ValueError("no equity observations")
    daily_returns = [equity_curve[i] / equity_curve[i - 1] - 1 for i in range(1, len(equity_curve))]
    days = max((dates[-1] - dates[0]).days, 1)

    end_equity = equity_curve[-1]
    cagr = (end_equity / initial_equity) ** (365.25 / days) - 1 if end_equity > 0 else -1.0
    volatility = stdev(daily_returns) * sqrt(252) if len(daily_returns) > 1 else 0.0

    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, 1 - value / peak)

    # Calendar-year returns compound the per-day growth that falls in each year.
    # Day 0's growth is the move from initial capital to the first marked equity.
    annual: dict[int, float] = {}
    for i, day in enumerate(dates):
        growth = equity_curve[i] / equity_curve[i - 1] if i > 0 else equity_curve[0] / initial_equity
        annual[day.year] = annual.get(day.year, 1.0) * growth
    annual_returns = {str(year): value - 1 for year, value in annual.items()}
    worst_year = min(annual_returns, key=lambda year: annual_returns[year]) if annual_returns else None

    years = days / 365.25
    mean_equity = sum(equity_curve) / len(equity_curve)
    annualized_turnover = (total_traded_notional / 2 / years / mean_equity) if years > 0 and mean_equity > 0 else 0.0

    defensive_days = sum(1 for weight in sgov_weights if weight > BASE_SGOV_WEIGHT + 1e-9)
    return {
        "start_date": dates[0].isoformat(),
        "end_date": dates[-1].isoformat(),
        "initial_equity": round(initial_equity, 2),
        "end_equity": round(end_equity, 2),
        "cagr_percent": round(cagr * 100, 4),
        "annualized_volatility_percent": round(volatility * 100, 4),
        "max_drawdown_percent": round(max_drawdown * 100, 4),
        "worst_calendar_year": {
            "year": worst_year,
            "return_percent": round(annual_returns[worst_year] * 100, 4),
        }
        if worst_year
        else None,
        "annual_returns_percent": {year: round(value * 100, 4) for year, value in annual_returns.items()},
        "total_traded_notional": round(total_traded_notional, 2),
        # One-way convention: buys+sells summed, halved, per year, as a share of mean equity.
        "annualized_turnover_ratio": round(annualized_turnover, 4),
        "modeled_slippage_cost": round(total_modeled_cost, 2),
        "modeled_cost_percent_of_initial_equity": round(total_modeled_cost / initial_equity * 100, 4),
        "min_cash_balance": round(min_cash_balance, 4),
        "time_in_sgov_percent": round(defensive_days / len(sgov_weights) * 100, 4) if sgov_weights else 0.0,
        "mean_sgov_weight_percent": round(sum(sgov_weights) / len(sgov_weights) * 100, 4) if sgov_weights else 0.0,
        "trade_count": trade_count,
    }


def _parse_slippage(raw: str) -> list[float]:
    values = sorted({float(part.strip()) for part in raw.split(",") if part.strip()})
    if not values or any(value < 0 for value in values):
        raise SystemExit("--slippage-bps must be non-negative, comma-separated values")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Cost-aware V1 backtest: buy-and-hold vs monthly trend-only vs "
            "monthly trend + volatility cap. Fetches adjusted daily Alpaca bars, "
            "writes a JSON report under reports/, submits nothing."
        )
    )
    parser.add_argument("--years", type=float, default=10.0, help="History length in years (default 10).")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--slippage-bps", type=str, default="0,5,10,25", help="One-way slippage scenarios in bps.")
    parser.add_argument(
        "--target-annual-volatility", type=float, default=None,
        help="Overrides BOT_TARGET_ANNUAL_VOLATILITY from .env for this run.",
    )
    parser.add_argument("--end", type=str, default=None, help="Last backtest date YYYY-MM-DD (default: yesterday ET).")
    args = parser.parse_args()

    # Imported lazily so importing bot.backtest never requires credentials.
    from bot.alpaca import AlpacaClient
    from bot.config import Settings

    settings = Settings.load()
    now = datetime.now(tz=EASTERN)
    end = date.fromisoformat(args.end) if args.end else now.date() - timedelta(days=1)
    # +450 calendar days of margin guarantees >=253 aligned warmup bars after
    # weekends/holidays are dropped by the intersection.
    start = end - timedelta(days=int(args.years * 365.25) + 450)
    slippage_values = _parse_slippage(args.slippage_bps)
    target_volatility = (
        args.target_annual_volatility if args.target_annual_volatility is not None else settings.target_annual_volatility
    )
    if not 0 < target_volatility <= 1:
        raise SystemExit("target annual volatility must be between 0 and 1")

    print(f"Fetching adjusted daily bars for {', '.join(SYMBOLS)} {start}..{end} (IEX feed)...")
    client = AlpacaClient(settings)
    bars = client.daily_bars(list(SYMBOLS), start, end)
    missing = [symbol for symbol in SYMBOLS if not bars.get(symbol)]
    if missing:
        raise SystemExit(f"No bars returned for: {', '.join(missing)}. Check the plan window and data access.")

    results: dict[str, dict[str, dict]] = {}
    for variant in VARIANTS:
        results[variant] = {}
        for bps in slippage_values:
            print(f"  running {variant} @ {bps:g} bps one-way slippage...")
            results[variant][f"{bps:g}"] = run_backtest(
                bars,
                variant=variant,
                slippage_bps=bps,
                initial_equity=args.initial_equity,
                target_annual_volatility=target_volatility,
            )

    comparison = [
        {
            "variant": variant,
            "slippage_bps": bps,
            "end_equity": results[variant][bps]["metrics"]["end_equity"],
            "cagr_percent": results[variant][bps]["metrics"]["cagr_percent"],
            "max_drawdown_percent": results[variant][bps]["metrics"]["max_drawdown_percent"],
            "annualized_turnover_ratio": results[variant][bps]["metrics"]["annualized_turnover_ratio"],
            "time_in_sgov_percent": results[variant][bps]["metrics"]["time_in_sgov_percent"],
        }
        for variant in VARIANTS
        for bps in results[variant]
    ]
    report = {
        "generated_at_et": now.isoformat(),
        "data": {
            "source": "Alpaca market data",
            "feed": "iex",
            "adjustment": "all (split+dividend adjusted)",
            "timeframe": "1Day",
            "symbols": list(SYMBOLS),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "completed_bars_only": True,
        },
        "config": {
            "initial_equity": args.initial_equity,
            "target_annual_volatility": target_volatility,
            "slippage_bps_scenarios": slippage_values,
            "base_weights": dict(BASE_WEIGHTS),
        },
        "disclaimers": list(DISCLAIMERS),
        "comparison": comparison,
        "results": results,
    }

    reports_dir = _reports_dir()
    path = reports_dir / f"backtest-{now.strftime('%Y%m%dT%H%M%S%z')}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Backtest complete. Report: {path}")
    header = f"{'variant':<24} {'bps':>5} {'end equity':>12} {'CAGR%':>8} {'maxDD%':>8} {'turnover':>9}"
    print(header)
    for row in comparison:
        print(_format_comparison_row(row))


def _format_comparison_row(row: dict) -> str:
    # Report JSON keys are strings, but the terminal table needs numeric formatting.
    return (
        f"{row['variant']:<24} {float(row['slippage_bps']):>5g} {row['end_equity']:>12.2f} "
        f"{row['cagr_percent']:>8.2f} {row['max_drawdown_percent']:>8.2f} {row['annualized_turnover_ratio']:>9.3f}"
    )


def _reports_dir():
    from pathlib import Path

    directory = Path("reports")
    directory.mkdir(exist_ok=True)
    return directory


if __name__ == "__main__":
    main()
