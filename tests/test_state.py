import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bot.state import (
    QTY_MATCH_TOLERANCE,
    TERMINAL_STATUSES,
    StateStore,
    client_order_id,
    normalize_broker_status,
)


class ClientOrderIdTests(unittest.TestCase):
    def test_deterministic_for_same_intent(self) -> None:
        first = client_order_id("2025-06", 1, "VTI", "buy")
        second = client_order_id("2025-06", 1, "VTI", "buy")
        self.assertEqual(first, second)

    def test_differs_by_attempt_symbol_and_side(self) -> None:
        base = client_order_id("2025-06", 1, "VTI", "buy")
        self.assertNotEqual(base, client_order_id("2025-06", 2, "VTI", "buy"))
        self.assertNotEqual(base, client_order_id("2025-06", 1, "VXUS", "buy"))
        self.assertNotEqual(base, client_order_id("2025-06", 1, "VTI", "sell"))

    def test_short_and_alpaca_safe(self) -> None:
        # Alpaca caps client order IDs at 48 characters; keep headroom.
        self.assertLessEqual(len(client_order_id("2025-06", 12, "VXUS", "sell")), 48)


class NormalizeStatusTests(unittest.TestCase):
    def test_terminal_statuses_map_to_themselves(self) -> None:
        for status in ("filled", "canceled", "expired", "rejected"):
            self.assertEqual(normalize_broker_status(status), status)
            self.assertIn(status, TERMINAL_STATUSES)

    def test_live_statuses_map_to_submitted(self) -> None:
        for status in ("new", "accepted", "pending_new", "held", "done_for_day"):
            self.assertEqual(normalize_broker_status(status), "submitted")

    def test_partially_filled_keeps_its_name(self) -> None:
        self.assertEqual(normalize_broker_status("partially_filled"), "partially_filled")

    def test_unknown_status_treated_as_still_live(self) -> None:
        # Never assume an unrecognized status is terminal.
        self.assertEqual(normalize_broker_status("some_new_broker_state"), "submitted")
        self.assertEqual(normalize_broker_status(None), "submitted")


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = StateStore(Path(self._tmp.name) / "state" / "bot-state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _seed_run(self, run_id: str = "rebalance-2025-06-a1", month_key: str = "2025-06") -> None:
        self.store.begin_run(
            run_id=run_id,
            kind="rebalance",
            month_key=month_key,
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )

    def test_intent_recorded_before_submit_is_persisted_and_idempotent(self) -> None:
        self._seed_run()
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0)
        # A restart re-records the identical intent: no-op, not a duplicate.
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0)
        intents = self.store.intents()
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].status, "pending")
        self.assertIsNone(intents[0].submitted_at)

    def test_conflicting_intent_for_same_client_order_id_raises(self) -> None:
        self._seed_run()
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0)
        with self.assertRaises(ValueError):
            self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=999.0)

    def test_apply_broker_order_is_idempotent_fill_delta(self) -> None:
        self._seed_run()
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0)
        self.store.mark_submitted(coid, "broker-1")

        broker_order = {"id": "broker-1", "status": "filled", "filled_qty": "10.5", "filled_avg_price": "250.10"}
        delta = self.store.apply_broker_order(coid, broker_order)
        self.assertAlmostEqual(delta, 10.5)
        # Replaying the same snapshot must be a no-op.
        replay = self.store.apply_broker_order(coid, broker_order)
        self.assertAlmostEqual(replay, 0.0)
        self.assertEqual(len(self.store.fills(coid)), 1)
        self.assertAlmostEqual(self.store.ownership()["VTI"], 10.5)
        intent = self.store.intent(coid)
        self.assertEqual(intent.status, "filled")
        self.assertAlmostEqual(intent.recorded_filled_qty, 10.5)

    def test_incremental_fill_snapshots_apply_only_the_delta(self) -> None:
        self._seed_run()
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0)
        self.store.mark_submitted(coid, "broker-1")

        self.store.apply_broker_order(coid, {"id": "broker-1", "status": "partially_filled", "filled_qty": "4", "filled_avg_price": "250"})
        self.store.apply_broker_order(coid, {"id": "broker-1", "status": "filled", "filled_qty": "9", "filled_avg_price": "251"})
        self.assertEqual(len(self.store.fills(coid)), 2)
        self.assertAlmostEqual(self.store.ownership()["VTI"], 9.0)

    def test_sell_fill_reduces_ownership(self) -> None:
        self._seed_run()
        coid = client_order_id("2025-06", 1, "VTI", "sell")
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="sell", qty=10.0, notional=None)
        self.store.apply_broker_order(coid, {"id": "b", "status": "filled", "filled_qty": "10", "filled_avg_price": "250"})
        self.assertAlmostEqual(self.store.ownership()["VTI"], -10.0)

    def test_unknown_status_is_queryable_and_not_terminal(self) -> None:
        self._seed_run()
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0)
        self.store.mark_submitted(coid, "broker-1")
        self.store.mark_status(coid, "unknown", note="ambiguous submit")
        unknown = self.store.intents(statuses={"unknown"})
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].client_order_id, coid)
        self.assertNotIn("unknown", TERMINAL_STATUSES)

    def test_abandoned_status_is_terminal(self) -> None:
        self._seed_run()
        coid = client_order_id("2025-05", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0)
        self.store.mark_status(coid, "abandoned")
        self.assertEqual(self.store.intent(coid).status, "abandoned")
        self.assertIn("abandoned", TERMINAL_STATUSES)

    def test_ownership_aggregates_multiple_symbols_and_sides(self) -> None:
        self.store.apply_fill("VTI", "buy", 10.0)
        self.store.apply_fill("VTI", "sell", 3.0)
        self.store.apply_fill("SGOV", "buy", 5.0)
        ownership = self.store.ownership()
        self.assertAlmostEqual(ownership["VTI"], 7.0, delta=QTY_MATCH_TOLERANCE)
        self.assertAlmostEqual(ownership["SGOV"], 5.0)

    def test_execution_lease_is_exclusive_and_fenced(self) -> None:
        first = self.store.acquire_execution_lease()
        self.assertIsNotNone(first)
        self.assertTrue(self.store.holds_execution_lease(first))

        # A second process opening the same SQLite state must fail closed.
        competing_store = StateStore(Path(self._tmp.name) / "state" / "bot-state.sqlite3")
        try:
            self.assertIsNone(competing_store.acquire_execution_lease())
            self.store.release_execution_lease(first)
            second = competing_store.acquire_execution_lease()
            self.assertIsNotNone(second)
            self.assertGreater(second.fence, first.fence)
            self.assertFalse(competing_store.holds_execution_lease(first))
            # A stale holder cannot release the newer holder's fence.
            self.store.release_execution_lease(first)
            self.assertTrue(competing_store.holds_execution_lease(second))
            competing_store.release_execution_lease(second)
        finally:
            competing_store.close()

    def test_run_persistence_and_lookup(self) -> None:
        self._seed_run()
        run = self.store.run_for_month("2025-06")
        self.assertEqual(run.run_id, "rebalance-2025-06-a1")
        self.assertEqual(run.status, "open")
        self.assertAlmostEqual(run.target_weights["VTI"], 0.54)
        self.assertEqual(self.store.count_runs_for_month("2025-06"), 1)
        self.assertIsNone(self.store.run_for_month("2025-07"))

        self.store.set_run_status("rebalance-2025-06-a1", "complete")
        self.assertEqual(self.store.run_for_month("2025-06").status, "complete")


class AtomicFillTransactionTests(unittest.TestCase):
    """Requirement: fill insert, ownership delta, cumulative filled qty, and
    order status update must be ONE SQLite transaction, and a regressive
    broker filled_qty must be rejected."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = StateStore(Path(self._tmp.name) / "state" / "bot-state.sqlite3")
        self.store.begin_run(
            run_id="rebalance-2025-06-a1",
            kind="rebalance",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        self.coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(
            client_order_id=self.coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0
        )
        self.store.mark_submitted(self.coid, "broker-1")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _snapshot(self, filled_qty: str, price: str = "250.10", status: str = "filled") -> dict:
        return {"id": "broker-1", "status": status, "filled_qty": filled_qty, "filled_avg_price": price}

    def test_mid_transaction_failure_rolls_back_fill_ownership_and_status(self) -> None:
        # Inject a failure at the ownership persistence boundary (inside the
        # transaction) via a SQLite trigger; the whole snapshot must roll back.
        connection = self.store._connection
        connection.execute(
            "CREATE TRIGGER injected_fail BEFORE INSERT ON ownership"
            " BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END"
        )
        with self.assertRaises(sqlite3.Error):
            self.store.apply_broker_order(self.coid, self._snapshot("10.5"))
        connection.execute("DROP TRIGGER injected_fail")

        # Nothing was applied: no fill row, no ownership, no status/qty change.
        self.assertEqual(self.store.fills(self.coid), [])
        self.assertEqual(self.store.ownership(), {})
        intent = self.store.intent(self.coid)
        self.assertEqual(intent.status, "submitted")
        self.assertAlmostEqual(intent.recorded_filled_qty, 0.0)

        # Reconcile the same broker fill snapshot now: applied exactly once.
        self.store.apply_broker_order(self.coid, self._snapshot("10.5"))
        self.assertEqual(len(self.store.fills(self.coid)), 1)
        self.assertAlmostEqual(self.store.ownership()["VTI"], 10.5)
        intent = self.store.intent(self.coid)
        self.assertEqual(intent.status, "filled")
        self.assertAlmostEqual(intent.recorded_filled_qty, 10.5)

        # Replaying the identical snapshot still changes nothing.
        self.store.apply_broker_order(self.coid, self._snapshot("10.5"))
        self.assertEqual(len(self.store.fills(self.coid)), 1)
        self.assertAlmostEqual(self.store.ownership()["VTI"], 10.5)

    def test_regressive_broker_filled_qty_is_rejected(self) -> None:
        self.store.apply_broker_order(self.coid, self._snapshot("10.0", status="partially_filled"))
        self.assertAlmostEqual(self.store.ownership()["VTI"], 10.0)
        with self.assertRaises(ValueError):
            # A stale/regressive snapshot must never produce a negative delta.
            self.store.apply_broker_order(self.coid, self._snapshot("4.0", status="partially_filled"))
        # State unchanged by the rejected snapshot.
        self.assertEqual(len(self.store.fills(self.coid)), 1)
        self.assertAlmostEqual(self.store.ownership()["VTI"], 10.0)
        self.assertAlmostEqual(self.store.intent(self.coid).recorded_filled_qty, 10.0)


class AttemptedStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = StateStore(Path(self._tmp.name) / "state" / "bot-state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_attempted_is_queryable_not_terminal_and_never_downgrades(self) -> None:
        self.store.begin_run(
            run_id="rebalance-2025-06-a1",
            kind="rebalance",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id="rebalance-2025-06-a1", symbol="VTI", side="buy", qty=None, notional=2646.0)
        self.store.mark_attempted(coid)
        intent = self.store.intent(coid)
        self.assertEqual(intent.status, "attempted")
        self.assertIsNotNone(intent.submitted_at)
        self.assertNotIn("attempted", TERMINAL_STATUSES)
        self.assertEqual(len(self.store.intents(statuses={"attempted"})), 1)
        # mark_attempted after a stronger state is a no-op (no downgrade).
        self.store.mark_submitted(coid, "broker-1")
        self.store.mark_attempted(coid)
        self.assertEqual(self.store.intent(coid).status, "submitted")


if __name__ == "__main__":
    unittest.main()
