import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bot.reconcile import CONSISTENCY_TOLERANCE_USD, build_reconciliation, model_order_costs

EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2025, 6, 2, 16, 30, tzinfo=EASTERN)


def _account(equity="5000.00", cash="1000.00", last_equity="4950.00"):
    return {
        "status": "ACTIVE",
        "equity": equity,
        "cash": cash,
        "last_equity": last_equity,
        "trading_blocked": False,
        "account_blocked": False,
    }


def _positions(**overrides):
    rows = [{"symbol": "VTI", "qty": "10", "side": "long", "market_value": "4000.00"}]
    rows.extend(overrides.get("extra", []))
    return rows


def _filled_buy(price="101.00", qty="10", symbol="VTI"):
    return {
        "id": "order-1",
        "client_order_id": "bot-1",
        "symbol": symbol,
        "side": "buy",
        "status": "filled",
        "filled_qty": qty,
        "filled_avg_price": price,
        "filled_at": "2025-06-02T15:00:00Z",  # 11:00 ET, same trading day
    }


class ModelOrderCostsTests(unittest.TestCase):
    def test_buy_above_close_is_unfavorable_cost(self) -> None:
        references = {("VTI", "2025-06-02"): 100.0}
        modeled, total = model_order_costs([_filled_buy()], references)
        self.assertEqual(len(modeled), 1)
        self.assertAlmostEqual(modeled[0]["modeled_slippage_per_share"], 1.0)
        self.assertAlmostEqual(modeled[0]["modeled_cost"], 10.0)
        self.assertAlmostEqual(total, 10.0)
        self.assertEqual(modeled[0]["sign_convention"], "positive slippage = unfavorable to the account")

    def test_sell_below_close_is_unfavorable_cost(self) -> None:
        order = _filled_buy()
        order.update({"side": "sell", "filled_avg_price": "99.00"})
        modeled, total = model_order_costs([order], {("VTI", "2025-06-02"): 100.0})
        self.assertAlmostEqual(modeled[0]["modeled_slippage_per_share"], 1.0)
        self.assertAlmostEqual(total, 10.0)

    def test_missing_reference_yields_no_cost(self) -> None:
        modeled, total = model_order_costs([_filled_buy()], {})
        self.assertIsNone(modeled[0]["modeled_slippage_per_share"])
        self.assertIsNone(modeled[0]["modeled_cost"])
        self.assertEqual(total, 0.0)
        self.assertIn("No same-day IEX close", modeled[0]["note"])

    def test_unfilled_orders_are_skipped(self) -> None:
        modeled, total = model_order_costs([{"id": "x", "symbol": "VTI", "side": "buy", "filled_qty": "0"}], {})
        self.assertEqual(modeled, [])
        self.assertEqual(total, 0.0)


class BuildReconciliationTests(unittest.TestCase):
    def _build(self, *, account=None, positions=None, closed=None, history=None, references=None):
        return build_reconciliation(
            now_et=NOW,
            account=account or _account(),
            positions=positions if positions is not None else _positions(),
            open_orders=[],
            closed_orders=closed or [],
            portfolio_history=history or {"profit_loss": [None, None, 50.0]},
            reference_closes=references or {},
            market_open=False,
        )

    def test_consistent_account_and_labels(self) -> None:
        report = self._build()
        consistency = report["consistency"]
        self.assertTrue(consistency["is_consistent"])  # 1000 cash + 4000 VTI = 5000 equity
        self.assertTrue(consistency["long_only_ok"])
        self.assertEqual(consistency["untracked_symbols_outside_strategy"], [])
        self.assertEqual(report["submitted_orders"], [])
        self.assertTrue(report["read_only"])
        # Paper P/L and modeled costs are explicitly labeled, never presented as live results.
        self.assertIn("PAPER", report["broker_reported"]["label"])
        self.assertIn("MODELED", report["modeled_fill_costs"]["label"])
        self.assertIn("NOT actual", report["modeled_fill_costs"]["label"])

    def test_broker_daily_pl_from_history_and_cross_check(self) -> None:
        report = self._build()
        broker = report["broker_reported"]
        self.assertAlmostEqual(broker["daily_profit_loss_from_history"], 50.0)
        self.assertAlmostEqual(broker["equity_minus_last_equity"], 50.0)

    def test_cash_mismatch_flags_inconsistency(self) -> None:
        report = self._build(account=_account(cash="900.00"))
        consistency = report["consistency"]
        self.assertFalse(consistency["is_consistent"])
        self.assertGreater(consistency["absolute_gap_usd"], CONSISTENCY_TOLERANCE_USD)

    def test_short_position_flags_long_only_violation(self) -> None:
        short = {"symbol": "VTI", "qty": "-5", "side": "short", "market_value": "-2000.00"}
        report = self._build(positions=[short], account=_account(cash="7000.00"))
        self.assertFalse(report["consistency"]["long_only_ok"])

    def test_untracked_symbol_is_flagged_never_traded(self) -> None:
        extra = {"symbol": "AAPL", "qty": "1", "side": "long", "market_value": "200.00"}
        report = self._build(positions=_positions(extra=[extra]), account=_account(cash="800.00"))
        self.assertEqual(report["consistency"]["untracked_symbols_outside_strategy"], ["AAPL"])

    def test_modeled_costs_flow_into_report(self) -> None:
        references = {("VTI", "2025-06-02"): 100.0}
        report = self._build(closed=[_filled_buy()], references=references)
        self.assertAlmostEqual(report["modeled_fill_costs"]["total_modeled_cost"], 10.0)
        self.assertEqual(len(report["modeled_fill_costs"]["orders"]), 1)

    def test_market_open_note(self) -> None:
        report = build_reconciliation(
            now_et=NOW,
            account=_account(),
            positions=_positions(),
            open_orders=[],
            closed_orders=[],
            portfolio_history={"profit_loss": []},
            reference_closes={},
            market_open=True,
        )
        self.assertTrue(report["market_state"]["open"])
        self.assertIn("incomplete", report["market_state"]["note"])


if __name__ == "__main__":
    unittest.main()
