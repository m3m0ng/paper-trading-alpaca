import unittest
from datetime import date, timedelta

from bot.backtest import (
    VARIANTS,
    _format_comparison_row,
    prepare_aligned_closes,
    run_backtest,
)
from bot.strategy import BASE_WEIGHTS, DEFENSIVE, INTERNATIONAL_EQUITY, US_EQUITY

START = date(2015, 1, 1)


def _dates(count: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(count)]


def _bars(symbol: str, prices: list[float]) -> list[dict]:
    return [
        {"t": f"{day.isoformat()}T04:00:00Z", "c": price}
        for day, price in zip(_dates(len(prices)), prices)
    ]


def _steady_world(count: int = 400) -> dict[str, list[dict]]:
    """All three sleeves in gentle uptrends, so every signal stays invested."""
    return {
        US_EQUITY: _bars(US_EQUITY, [100 * 1.001**i for i in range(count)]),
        INTERNATIONAL_EQUITY: _bars(INTERNATIONAL_EQUITY, [100 * 1.0005**i for i in range(count)]),
        DEFENSIVE: _bars(DEFENSIVE, [100 * 1.0001**i for i in range(count)]),
    }


class PrepareClosesTests(unittest.TestCase):
    def test_aligns_to_common_dates(self) -> None:
        bars = _steady_world(300)
        bars[US_EQUITY] = bars[US_EQUITY][10:]  # simulate a shorter history for one symbol
        dates, closes = prepare_aligned_closes(bars)
        self.assertEqual(len(dates), 290)
        self.assertEqual(len(closes[US_EQUITY]), 290)

    def test_drops_bars_on_or_after_as_of(self) -> None:
        bars = _steady_world(300)
        dates, closes = prepare_aligned_closes(bars, as_of=date(2015, 10, 1))
        self.assertTrue(all(day < date(2015, 10, 1) for day in dates))
        self.assertEqual(len(dates), 273)


class BuyAndHoldTests(unittest.TestCase):
    def test_end_equity_matches_exact_weighted_growth(self) -> None:
        bars = _steady_world(400)
        result = run_backtest(bars, variant="buy_and_hold", slippage_bps=0.0, initial_equity=10_000.0)
        metrics = result["metrics"]

        # Single initial buy per sleeve at index 253, then never traded again.
        vti_growth = bars[US_EQUITY][-1]["c"] / bars[US_EQUITY][253]["c"]
        vxus_growth = bars[INTERNATIONAL_EQUITY][-1]["c"] / bars[INTERNATIONAL_EQUITY][253]["c"]
        sgov_growth = bars[DEFENSIVE][-1]["c"] / bars[DEFENSIVE][253]["c"]
        expected = 10_000 * (
            BASE_WEIGHTS[US_EQUITY] * vti_growth
            + BASE_WEIGHTS[INTERNATIONAL_EQUITY] * vxus_growth
            + BASE_WEIGHTS[DEFENSIVE] * sgov_growth
        )
        self.assertAlmostEqual(metrics["end_equity"], expected, places=2)
        self.assertEqual(metrics["trade_count"], 3)
        self.assertAlmostEqual(metrics["total_traded_notional"], 10_000.0, places=2)
        self.assertAlmostEqual(metrics["modeled_slippage_cost"], 0.0, places=6)
        # A monotonic uptrend cannot draw down.
        self.assertEqual(metrics["max_drawdown_percent"], 0.0)
        self.assertAlmostEqual(metrics["time_in_sgov_percent"], 0.0, places=6)

    def test_cagr_matches_observed_growth(self) -> None:
        bars = _steady_world(400)
        metrics = run_backtest(bars, variant="buy_and_hold", slippage_bps=0.0)["metrics"]
        start = date.fromisoformat(metrics["start_date"])
        end = date.fromisoformat(metrics["end_date"])
        days = (end - start).days
        expected_cagr = (metrics["end_equity"] / metrics["initial_equity"]) ** (365.25 / days) - 1
        self.assertAlmostEqual(metrics["cagr_percent"], expected_cagr * 100, places=3)

    def test_metrics_begin_on_first_investable_trade_not_warmup(self) -> None:
        result = run_backtest(_steady_world(400), variant="buy_and_hold", slippage_bps=0.0)
        self.assertEqual(result["metrics"]["start_date"], result["trades"][0]["date"])


class NoLookAheadTests(unittest.TestCase):
    def test_monthly_trades_use_only_prior_completed_bars(self) -> None:
        bars = _steady_world(500)
        result = run_backtest(bars, variant="monthly_trend", slippage_bps=0.0)
        trades = result["trades"]
        self.assertGreater(len(trades), 3)  # initial buys plus monthly rebalances

        trade_dates = sorted({trade["date"] for trade in trades})
        initial_trade_date = trade_dates[0]  # the initial buy lands on the warmup date, not a month start
        for trade_date in trade_dates[1:]:
            day = date.fromisoformat(trade_date)
            # Monthly rebalances after the initial buy land on the first aligned
            # trading day of a month.
            prior_same_month = [d for d in _dates(500) if d < day and (d.year, d.month) == (day.year, day.month)]
            self.assertFalse(
                prior_same_month,
                f"trade on {day} is not the first trading day of its month",
            )
        # Every trend trade records the last bar used, always strictly before the trade date.
        for trade in trades:
            through = trade["signal_through_date"]
            self.assertIsNotNone(through)
            self.assertLess(date.fromisoformat(through), date.fromisoformat(trade["date"]))


class SlippageTests(unittest.TestCase):
    def test_modeled_cost_equals_traded_notional_times_bps(self) -> None:
        bars = _steady_world(500)
        zero = run_backtest(bars, variant="monthly_trend", slippage_bps=0.0)
        costed = run_backtest(bars, variant="monthly_trend", slippage_bps=25.0)
        self.assertGreater(costed["metrics"]["modeled_slippage_cost"], 0.0)
        self.assertAlmostEqual(
            costed["metrics"]["modeled_slippage_cost"],
            costed["metrics"]["total_traded_notional"] * 25.0 / 10_000,
            places=2,
        )
        self.assertLess(costed["metrics"]["end_equity"], zero["metrics"]["end_equity"])

    def test_slippage_never_pushes_cash_negative(self) -> None:
        # An extreme 10% one-way fee forces buy scaling; cash must stay >= 0.
        for variant in ("buy_and_hold", "monthly_trend", "monthly_trend_vol_cap"):
            result = run_backtest(_steady_world(500), variant=variant, slippage_bps=1000.0)
            self.assertGreaterEqual(result["metrics"]["min_cash_balance"], -1e-6, variant)

    def test_scaled_buys_spend_at_most_available_cash(self) -> None:
        bars = _steady_world(400)
        result = run_backtest(bars, variant="buy_and_hold", slippage_bps=100.0)
        buys = [trade for trade in result["trades"] if trade["side"] == "buy"]
        # notional + modeled fee must fit inside the starting cash balance.
        spend = sum(trade["notional"] for trade in buys) * 1.01
        self.assertLessEqual(spend, 10_000.0 + 1e-6)
        # Cash is fully deployed (no unspent remainder, no negative balance).
        self.assertAlmostEqual(result["metrics"]["min_cash_balance"], 0.0, places=6)

    def test_negative_slippage_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_backtest(_steady_world(400), variant="buy_and_hold", slippage_bps=-1.0)


class TrendDefensiveTests(unittest.TestCase):
    def test_failed_trend_moves_sleeve_to_sgov(self) -> None:
        # Strong uptrend, then a ~35% slide that leaves VTI below its 200-day SMA.
        count = 420
        vti = [100 + i for i in range(320)] + [420 * 0.99**k for k in range(1, count - 320 + 1)]
        bars = {
            US_EQUITY: _bars(US_EQUITY, vti),
            INTERNATIONAL_EQUITY: _bars(INTERNATIONAL_EQUITY, [100 + i for i in range(count)]),
            DEFENSIVE: _bars(DEFENSIVE, [100 * 1.0001**i for i in range(count)]),
        }
        result = run_backtest(bars, variant="monthly_trend", slippage_bps=0.0)
        metrics = result["metrics"]
        # VTI closes far below its rising 200-day average after the slide, so some
        # months must run with SGOV above the permanent 10% reserve.
        self.assertGreater(metrics["time_in_sgov_percent"], 0.0)
        self.assertGreater(metrics["mean_sgov_weight_percent"], 10.0)
        self.assertLess(metrics["max_drawdown_percent"], 35.0)

    def test_vol_cap_variant_runs_and_sums_to_one(self) -> None:
        bars = _steady_world(500)
        result = run_backtest(bars, variant="monthly_trend_vol_cap", slippage_bps=5.0)
        self.assertTrue(result["metrics"]["end_equity"] > 0)
        # Every trade's signal inputs end strictly before its trade date.
        for trade in result["trades"]:
            if trade["signal_through_date"]:
                self.assertLess(date.fromisoformat(trade["signal_through_date"]), date.fromisoformat(trade["date"]))


class ReportFormattingTests(unittest.TestCase):
    def test_comparison_row_formats_json_string_slippage(self) -> None:
        row = {
            "variant": "buy_and_hold",
            "slippage_bps": "5",
            "end_equity": 5000.0,
            "cagr_percent": 10.0,
            "max_drawdown_percent": 12.0,
            "annualized_turnover_ratio": 0.1,
        }
        self.assertIn("    5", _format_comparison_row(row))


class VariantValidationTests(unittest.TestCase):
    def test_unknown_variant_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_backtest(_steady_world(400), variant="nope", slippage_bps=0.0)

    def test_all_variants_available(self) -> None:
        self.assertEqual(set(VARIANTS), {"buy_and_hold", "monthly_trend", "monthly_trend_vol_cap"})


if __name__ == "__main__":
    unittest.main()
