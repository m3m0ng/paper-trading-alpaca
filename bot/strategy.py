from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import stdev
from typing import Iterable

US_EQUITY = "VTI"
INTERNATIONAL_EQUITY = "VXUS"
DEFENSIVE = "SGOV"
SYMBOLS = (US_EQUITY, INTERNATIONAL_EQUITY, DEFENSIVE)

# V1 base allocation (agreed after review): at most 90% risk assets split
# VTI/VXUS, plus a permanent 10% SGOV reserve that is never deployed to equities.
BASE_WEIGHTS = {US_EQUITY: 0.54, INTERNATIONAL_EQUITY: 0.36, DEFENSIVE: 0.10}


@dataclass(frozen=True)
class Signal:
    symbol: str
    latest_close: float
    sma_200: float
    return_12_1_month: float
    invested: bool


def evaluate_signal(symbol: str, closes: Iterable[float]) -> Signal:
    """Use only completed daily closes; 252 and 21 trading days approximate 12–1 momentum."""
    prices = list(closes)
    if len(prices) < 253:
        raise ValueError(f"{symbol} needs at least 253 completed daily bars; got {len(prices)}")
    if any(price <= 0 for price in prices):
        raise ValueError(f"{symbol} contains a non-positive close")

    latest_close = prices[-1]
    sma_200 = sum(prices[-200:]) / 200
    return_12_1_month = prices[-21] / prices[-253] - 1
    invested = latest_close > sma_200 and return_12_1_month > 0
    return Signal(symbol, latest_close, sma_200, return_12_1_month, invested)


def target_weights(signals: dict[str, Signal]) -> dict[str, float]:
    for symbol in (US_EQUITY, INTERNATIONAL_EQUITY):
        if symbol not in signals:
            raise ValueError(f"Missing signal for {symbol}")

    # A sleeve whose trend fails moves its full fixed weight into SGOV; the
    # 10% reserve stays in SGOV either way.
    targets = dict(BASE_WEIGHTS)
    for symbol in (US_EQUITY, INTERNATIONAL_EQUITY):
        if not signals[symbol].invested:
            targets[symbol] = 0.0
            targets[DEFENSIVE] += BASE_WEIGHTS[symbol]
    return targets


def annualized_portfolio_volatility(
    closes_by_symbol: dict[str, list[float]], equity_weights: dict[str, float], lookback: int = 63
) -> float:
    """Calculate trailing realized volatility of the currently targeted equity mix."""
    active = {symbol: weight for symbol, weight in equity_weights.items() if weight > 0}
    if not active:
        return 0.0
    required_prices = lookback + 1
    for symbol in active:
        if len(closes_by_symbol[symbol]) < required_prices:
            raise ValueError(f"{symbol} needs {required_prices} closes for volatility")

    returns: list[float] = []
    for offset in range(-lookback, 0):
        returns.append(
            sum(
                weight * (closes_by_symbol[symbol][offset] / closes_by_symbol[symbol][offset - 1] - 1)
                for symbol, weight in active.items()
            )
        )
    return stdev(returns) * sqrt(252)


def apply_volatility_cap(
    weights: dict[str, float], closes_by_symbol: dict[str, list[float]], target_volatility: float
) -> tuple[dict[str, float], float, float]:
    """Reduce risk in volatile markets without leverage; residual weight moves to SGOV."""
    if not 0 < target_volatility <= 1:
        raise ValueError("target_volatility must be between 0 and 1")

    risky_weights = {US_EQUITY: weights[US_EQUITY], INTERNATIONAL_EQUITY: weights[INTERNATIONAL_EQUITY]}
    realized_volatility = annualized_portfolio_volatility(closes_by_symbol, risky_weights)
    multiplier = min(1.0, target_volatility / realized_volatility) if realized_volatility else 1.0
    capped = dict(weights)
    risky_total_before = sum(risky_weights.values())
    capped[US_EQUITY] *= multiplier
    capped[INTERNATIONAL_EQUITY] *= multiplier
    capped[DEFENSIVE] += risky_total_before * (1 - multiplier)
    return capped, realized_volatility, multiplier
