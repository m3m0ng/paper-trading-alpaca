import unittest

from bot.strategy import (
    DEFENSIVE,
    INTERNATIONAL_EQUITY,
    US_EQUITY,
    Signal,
    apply_volatility_cap,
    evaluate_signal,
    target_weights,
)


class StrategyTests(unittest.TestCase):
    def test_positive_trend_invests(self) -> None:
        # Rising prices meet both the 200-day trend and 12–1 month momentum rules.
        signal = evaluate_signal(US_EQUITY, [100 + day for day in range(260)])
        self.assertTrue(signal.invested)

    def test_negative_momentum_stays_defensive(self) -> None:
        # The last month rises, but the 12–1 month return is negative.
        signal = evaluate_signal(US_EQUITY, [300 - day for day in range(239)] + [62 + day for day in range(21)])
        self.assertFalse(signal.invested)

    def test_base_allocation_is_54_36_with_permanent_sgov_reserve(self) -> None:
        # Both sleeves in uptrends: 90% risk assets plus the untouched 10% reserve.
        signals = {
            US_EQUITY: Signal(US_EQUITY, 100, 90, 0.1, True),
            INTERNATIONAL_EQUITY: Signal(INTERNATIONAL_EQUITY, 100, 90, 0.1, True),
        }
        weights = target_weights(signals)
        self.assertAlmostEqual(weights[US_EQUITY], 0.54)
        self.assertAlmostEqual(weights[INTERNATIONAL_EQUITY], 0.36)
        self.assertAlmostEqual(weights[DEFENSIVE], 0.10)

    def test_trend_targets_move_failed_sleeve_to_defensive_etf(self) -> None:
        signals = {
            US_EQUITY: Signal(US_EQUITY, 100, 90, 0.1, True),
            INTERNATIONAL_EQUITY: Signal(INTERNATIONAL_EQUITY, 100, 110, -0.1, False),
        }
        weights = target_weights(signals)
        self.assertAlmostEqual(weights[US_EQUITY], 0.54)
        self.assertAlmostEqual(weights[INTERNATIONAL_EQUITY], 0.0)
        # The failed 36% international sleeve joins the 10% permanent reserve.
        self.assertAlmostEqual(weights[DEFENSIVE], 0.46)

    def test_volatility_cap_reduces_equities_without_leverage(self) -> None:
        # Alternating 2% moves produce annualized volatility above a 10% target.
        vti = [100.0]
        vxus = [100.0]
        for day in range(70):
            vti.append(vti[-1] * (1.02 if day % 2 else 0.98))
            vxus.append(vxus[-1] * (1.02 if day % 2 else 0.98))

        weights, volatility, multiplier = apply_volatility_cap(
            {US_EQUITY: 0.54, INTERNATIONAL_EQUITY: 0.36, DEFENSIVE: 0.10},
            {US_EQUITY: vti, INTERNATIONAL_EQUITY: vxus},
            0.10,
        )
        self.assertGreater(volatility, 0.10)
        self.assertLess(multiplier, 1)
        self.assertAlmostEqual(sum(weights.values()), 1)
        # Planned equity exposure is cut below the 90% base and the residual
        # lands in SGOV on top of the permanent reserve.
        self.assertLess(weights[US_EQUITY] + weights[INTERNATIONAL_EQUITY], 0.90)
        self.assertGreater(weights[DEFENSIVE], 0.10)


if __name__ == "__main__":
    unittest.main()
