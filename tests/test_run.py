"""Guarded-execution tests. Every test uses an injected fake Alpaca client and
a temporary SQLite store — no network calls, no .env reads, no real orders."""
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from bot.alpaca import AlpacaError
from bot.config import Settings
from bot.run import build_orders_from_targets, build_plan, run_execution
from bot.state import StateStore, client_order_id
from bot.strategy import BASE_WEIGHTS, DEFENSIVE, INTERNATIONAL_EQUITY, Signal, US_EQUITY

PAPER_URL = "https://paper-api.alpaca.markets"
# 2025-06-02 is the first official market day of June 2025 (a Monday).
TODAY = date(2025, 6, 2)
NOW_UTC = "2025-06-02T13:35:00Z"  # 09:35 ET
LIVE_BROKER_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held"}


def _settings(**overrides) -> Settings:
    values = {
        "api_key": "test-key",
        "secret_key": "test-secret",
        "api_base_url": PAPER_URL,
        "trading_enabled": True,
        "max_managed_equity": 5000.0,
        "cash_buffer_percent": 2.0,
        "target_annual_volatility": 0.10,
        "ai_enabled": False,
        "ai_monthly_budget_usd": 0.0,
    }
    values.update(overrides)
    return Settings(**values)


def _bar_day(offset_from_today: int) -> str:
    return (TODAY - timedelta(days=offset_from_today)).isoformat()


class PlanTargetTests(unittest.TestCase):
    def test_plan_orders_use_volatility_capped_targets(self) -> None:
        signals = {
            US_EQUITY: Signal(US_EQUITY, 100.0, 90.0, 0.1, True),
            INTERNATIONAL_EQUITY: Signal(INTERNATIONAL_EQUITY, 100.0, 90.0, 0.1, True),
            DEFENSIVE: Signal(DEFENSIVE, 100.0, 99.0, 0.01, True),
        }
        targets = {US_EQUITY: 0.49, INTERNATIONAL_EQUITY: 0.33, DEFENSIVE: 0.18}
        orders = build_plan(
            targets=targets,
            signals=signals,
            positions=[],
            cash=5000.0,
            managed_equity=5000.0,
            cash_buffer_percent=0.0,
        )
        notionals = {order.symbol: order.notional for order in orders}
        self.assertEqual(notionals, {US_EQUITY: 2450.0, INTERNATIONAL_EQUITY: 1650.0, DEFENSIVE: 900.0})


class SettingsLoadTests(unittest.TestCase):
    def test_direct_live_endpoint_settings_are_rejected_before_client_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "paper-only"):
            Settings(
                api_key="test-key",
                secret_key="test-secret",
                api_base_url="https://api.alpaca.markets",
                trading_enabled=False,
                max_managed_equity=0.0,
                cash_buffer_percent=2.0,
                target_annual_volatility=0.10,
                ai_enabled=False,
                ai_monthly_budget_usd=0.0,
            )

    def test_direct_boolean_settings_reject_truthy_non_booleans_before_client_calls(self) -> None:
        for field in ("trading_enabled", "ai_enabled"):
            for value in ("false", 1):
                with self.subTest(field=field, value=value):
                    client = FakeAlpaca()
                    with self.assertRaisesRegex(ValueError, "must be a boolean"):
                        _settings(**{field: value})
                    self.assertEqual(client.events, [])
                    self.assertEqual(client.submits, [])

    def test_direct_boolean_settings_accept_true_and_false(self) -> None:
        for field in ("trading_enabled", "ai_enabled"):
            for value in (True, False):
                with self.subTest(field=field, value=value):
                    settings = _settings(**{field: value})
                    self.assertIs(getattr(settings, field), value)

    def test_direct_cash_buffer_validation_rejects_invalid_values_before_client_calls(self) -> None:
        for value, error in ((-1.0, "cannot be negative"), (float("-inf"), "must be finite")):
            with self.subTest(value=value):
                client = FakeAlpaca()
                with self.assertRaisesRegex(ValueError, f"BOT_CASH_BUFFER_PERCENT {error}"):
                    _settings(cash_buffer_percent=value)
                self.assertEqual(client.events, [])
                self.assertEqual(client.submits, [])

    def test_direct_nonfinite_numeric_settings_are_rejected(self) -> None:
        for field in (
            "max_managed_equity",
            "cash_buffer_percent",
            "target_annual_volatility",
            "ai_monthly_budget_usd",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "must be finite"):
                    _settings(**{field: float("inf")})

    def test_nonfinite_numeric_settings_are_rejected(self) -> None:
        numeric_settings = (
            "BOT_MAX_MANAGED_EQUITY",
            "BOT_CASH_BUFFER_PERCENT",
            "BOT_TARGET_ANNUAL_VOLATILITY",
            "BOT_AI_MONTHLY_BUDGET_USD",
        )
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "settings.env"
            for key in numeric_settings:
                for value in ("inf", "nan"):
                    with self.subTest(key=key, value=value):
                        env_path.write_text(
                            f"APCA_API_KEY_ID=test-key\nAPCA_API_SECRET_KEY=test-secret\n{key}={value}\n",
                            encoding="utf-8",
                        )
                        with self.assertRaisesRegex(ValueError, f"{key} must be finite"):
                            Settings.load(env_path)


class FakeAlpaca:
    """In-memory stand-in for AlpacaClient. Records every call so tests can
    assert guard ordering and exact submission counts."""

    def __init__(
        self,
        *,
        is_open: bool = True,
        blocked: bool = False,
        positions: list[dict] | None = None,
        cash: str = "5000.00",
        equity: str = "5000.00",
        calendar_days: list[str] | None = None,
        fail_submits: bool = False,
        record_broker_order_on_failure: bool = False,
        fail_lookups: bool = False,
        crash_on_submit: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.submits: list[dict] = []
        self.broker_orders: dict[str, dict] = {}
        self._next_id = 1
        self._is_open = is_open
        self._blocked = blocked
        self._positions = positions or []
        self._cash = cash
        self._equity = equity
        self._calendar_days = calendar_days or [_bar_day(0)]
        self._fail_submits = fail_submits
        self._record_broker_order_on_failure = record_broker_order_on_failure
        self._fail_lookups = fail_lookups
        self._crash_on_submit = crash_on_submit

    def clock(self) -> dict:
        self.events.append("clock")
        return {"is_open": self._is_open, "timestamp": NOW_UTC}

    def calendar(self, start, end) -> list[dict]:
        self.events.append("calendar")
        return [{"date": day} for day in self._calendar_days]

    def account(self) -> dict:
        self.events.append("account")
        return {
            "status": "ACTIVE",
            "equity": self._equity,
            "cash": self._cash,
            "last_equity": self._equity,
            "trading_blocked": self._blocked,
            "account_blocked": self._blocked,
        }

    def positions(self) -> list[dict]:
        self.events.append("positions")
        return list(self._positions)

    def open_orders(self) -> list[dict]:
        self.events.append("open_orders")
        return [order for order in self.broker_orders.values() if order["status"] in LIVE_BROKER_STATUSES]

    def daily_bars(self, symbols, start, end) -> dict[str, list[dict]]:
        self.events.append("daily_bars")
        # 300 completed daily bars ending yesterday, in gentle uptrends so all
        # trend signals stay invested (realized volatility stays below target).
        bars: dict[str, list[dict]] = {}
        for index, symbol in enumerate(symbols):
            growth = 1.0005 + index * 0.0002
            bars[symbol] = [
                {"t": f"{_bar_day(offset)}T04:00:00Z", "c": 100 * growth ** (299 - offset)}
                for offset in range(299, -1, -1)
            ]
        return bars

    def submit_market_order(self, *, symbol, side, client_order_id, qty=None, notional=None) -> dict:
        self.events.append("submit")
        if self._crash_on_submit:
            # Simulated process death mid-POST: the broker accepts the order,
            # but the caller never sees the response and no state is updated.
            # A non-AlpacaError exception so no in-run handling can rescue it.
            self._crash_on_submit = False
            self.broker_orders[client_order_id] = {
                "id": f"broker-{self._next_id}",
                "status": "accepted",
                "symbol": symbol,
                "side": side,
                "client_order_id": client_order_id,
                "filled_qty": "0",
                "filled_avg_price": None,
            }
            self._next_id += 1
            raise RuntimeError("simulated process death before the POST response")
        if self._fail_submits:
            # Fail only the first POST; later ones succeed (lets one test cover
            # "resolve the ambiguity, then continue with the remaining orders").
            self._fail_submits = False
            # Accepted-but-timed-out: the broker DID create the order, but the
            # caller only got a timeout.
            if self._record_broker_order_on_failure:
                self.broker_orders[client_order_id] = {
                    "id": f"broker-{self._next_id}",
                    "status": "accepted",
                    "symbol": symbol,
                    "side": side,
                    "client_order_id": client_order_id,
                    "filled_qty": "0",
                    "filled_avg_price": None,
                }
                self._next_id += 1
            raise AlpacaError("request timed out after the broker accepted the order")
        order = {
            "id": f"broker-{self._next_id}",
            "status": "new",
            "symbol": symbol,
            "side": side,
            "client_order_id": client_order_id,
            "filled_qty": "0",
            "filled_avg_price": None,
        }
        self._next_id += 1
        self.broker_orders[client_order_id] = order
        self.submits.append({"symbol": symbol, "side": side, "client_order_id": client_order_id, "qty": qty, "notional": notional})
        return order

    def order_by_client_order_id(self, client_order_id: str) -> dict | None:
        self.events.append("lookup")
        if self._fail_lookups:
            raise AlpacaError("reconciliation lookup could not reach the broker")
        return self.broker_orders.get(client_order_id)  # missing == HTTP 404


class LeaseCompetitionFake(FakeAlpaca):
    """Starts a competing execution during the first holder's POST window."""

    def __init__(self, *, store: StateStore) -> None:
        super().__init__()
        self._store = store
        self.competing_audit: dict | None = None

    def submit_market_order(self, **kwargs) -> dict:
        if self.competing_audit is None:
            self.competing_audit = run_execution(_settings(), self, self._store)
        return super().submit_market_order(**kwargs)


class RunExecutionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = StateStore(Path(self._tmp.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _seed_broker_sell_intent(self, *, run_id: str, symbol: str, qty: float, attempt: int = 1, broker_id: str):
        """Seed a previously submitted (possibly partially filled) sell intent."""
        coid = client_order_id("2025-06", attempt, symbol, "sell")
        self.store.record_intent(client_order_id=coid, run_id=run_id, symbol=symbol, side="sell", qty=qty, notional=None)
        self.store.mark_submitted(coid, broker_id)
        return coid


class DisabledAndSafetyTests(RunExecutionTestCase):
    def test_trading_disabled_refuses_before_any_network_call(self) -> None:
        client = FakeAlpaca()
        audit = run_execution(_settings(trading_enabled=False), client, self.store)
        self.assertIsNotNone(audit["halted"])
        self.assertIn("BOT_TRADING_ENABLED", audit["halted"])
        # Config guard runs first: not even the clock was fetched.
        self.assertEqual(client.events, [])
        self.assertEqual(client.submits, [])

    def test_missing_cap_refuses(self) -> None:
        client = FakeAlpaca()
        audit = run_execution(_settings(max_managed_equity=0.0), client, self.store)
        self.assertIsNotNone(audit["halted"])
        self.assertIn("BOT_MAX_MANAGED_EQUITY", audit["halted"])
        self.assertEqual(client.events, [])

    def test_nonfinite_directly_constructed_cap_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "BOT_MAX_MANAGED_EQUITY must be finite"):
            _settings(max_managed_equity=float("inf"))

    def test_closed_market_halts_before_account_or_orders(self) -> None:
        client = FakeAlpaca(is_open=False)
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Market closed", audit["halted"])
        self.assertEqual(client.submits, [])
        self.assertNotIn("positions", client.events)

    def test_blocked_account_halts_with_zero_submits(self) -> None:
        client = FakeAlpaca(blocked=True)
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("blocked", audit["halted"])
        self.assertEqual(client.submits, [])


class OwnershipAndCapTests(RunExecutionTestCase):
    def test_preexisting_holding_without_state_halts_with_zero_submits(self) -> None:
        # Broker holds VTI but this store's ownership ledger is empty: the bot
        # did not acquire it, so it must never trade around it.
        client = FakeAlpaca(
            positions=[{"symbol": "VTI", "qty": "10", "side": "long", "market_value": "1348.00"}],
            cash="3652.00",
        )
        audit = run_execution(_settings(), client, self.store)
        self.assertIsNotNone(audit["halted"])
        self.assertIn("Ownership mismatch", audit["halted"])
        self.assertEqual(client.submits, [])

    def test_untracked_symbol_halts_with_zero_submits(self) -> None:
        client = FakeAlpaca(
            positions=[{"symbol": "AAPL", "qty": "1", "side": "long", "market_value": "200.00"}],
        )
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Untracked broker position AAPL", audit["halted"])
        self.assertEqual(client.submits, [])

    def test_open_orders_halt_with_zero_submits(self) -> None:
        client = FakeAlpaca()
        client.broker_orders["stray"] = {"id": "stray", "status": "new", "symbol": "VTI", "side": "buy"}
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Open orders", audit["halted"])
        self.assertEqual(client.submits, [])

    def test_bootstrap_refused_when_cash_below_cap(self) -> None:
        client = FakeAlpaca(cash="4000.00", equity="4000.00")
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Bootstrap requires available cash >= BOT_MAX_MANAGED_EQUITY", audit["halted"])
        self.assertEqual(client.submits, [])

    def test_owned_value_above_cap_halts(self) -> None:
        # Ownership ledger matches the broker, but the position value exceeds
        # the configured cap (e.g. the cap was lowered after buying).
        self.store.apply_fill("VTI", "buy", 20.0)
        client = FakeAlpaca(
            positions=[{"symbol": "VTI", "qty": "20", "side": "long", "market_value": "2696.00"}],
            cash="2304.00",
        )
        audit = run_execution(_settings(max_managed_equity=2000.0), client, self.store)
        self.assertIn("exceeds the configured cap", audit["halted"])
        self.assertEqual(client.submits, [])

    def test_equity_accounting_mismatch_halts_before_any_post(self) -> None:
        # Tracked, allowed position; but equity does not equal cash + market
        # value (off by 10 USD, beyond the named tolerance). The bot must halt
        # with an audit reason and submit nothing.
        self.store.apply_fill("VTI", "buy", 20.0)
        client = FakeAlpaca(
            positions=[{"symbol": "VTI", "qty": "20", "side": "long", "market_value": "2696.00"}],
            cash="2304.00",
            equity="4990.00",
        )
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("accounting mismatch", audit["halted"])
        self.assertEqual(client.submits, [])
        self.assertIn("equity_accounting", [guard["check"] for guard in audit["guards"]])


class GuardedSubmissionTests(RunExecutionTestCase):
    def test_overlapping_execution_halts_under_lease_without_a_second_post_sequence(self) -> None:
        client = LeaseCompetitionFake(store=self.store)
        audit = run_execution(_settings(), client, self.store)

        self.assertIsNone(audit["halted"], audit["halted"])
        self.assertIsNotNone(client.competing_audit)
        self.assertIn("another execution holds the durable lease", client.competing_audit["halted"])
        self.assertEqual(client.competing_audit["submitted_intents"], [])
        self.assertEqual(client.events.count("submit"), len(audit["submitted_intents"]))
        self.assertNotIn("clock", client.events[client.events.index("submit") + 1 :])

    def test_bootstrap_submits_buys_only_after_all_guards_and_respects_cap(self) -> None:
        client = FakeAlpaca()
        audit = run_execution(_settings(), client, self.store)
        self.assertIsNone(audit["halted"], audit["halted"])
        self.assertTrue(client.submits)
        self.assertTrue(all(entry["side"] == "buy" for entry in client.submits))

        # Guard ordering: every guard event precedes the first submit.
        first_submit = client.events.index("submit")
        for guard_event in ("clock", "account", "positions", "open_orders", "calendar", "daily_bars"):
            self.assertLess(client.events.index(guard_event), first_submit, guard_event)

        # The configured cap bounds every sleeve: total buy notional stays at
        # or below the cap (buffer leaves room), each sleeve below its target.
        total = sum(entry["notional"] for entry in client.submits)
        self.assertLessEqual(total, 5000.0 + 0.01)
        targets = audit["target_weights"]
        for entry in client.submits:
            self.assertLessEqual(entry["notional"], 5000.0 * targets[entry["symbol"]] + 0.01)
        # Sells-before-buys invariant holds (no sells here, trivially ordered).
        self.assertEqual(audit["submitted_intents"][0]["client_order_id"], client.submits[0]["client_order_id"])

    def test_sell_submission_defers_buy_until_fresh_recovery_reconciliation(self) -> None:
        # $5,000 of managed holdings need a $300 VTI sell and a $300 VXUS buy.
        # The first invocation must submit only the sell, despite $1,000 cash.
        for symbol, qty in (("VTI", 30.0), ("VXUS", 15.0), ("SGOV", 5.0)):
            self.store.apply_fill(symbol, "buy", qty)
        client = FakeAlpaca(
            positions=[
                {"symbol": "VTI", "qty": "30", "side": "long", "market_value": "3000.00"},
                {"symbol": "VXUS", "qty": "15", "side": "long", "market_value": "1500.00"},
                {"symbol": "SGOV", "qty": "5", "side": "long", "market_value": "500.00"},
            ],
            cash="1000.00",
            equity="6000.00",
        )
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Sell submitted", audit["halted"])
        self.assertEqual([entry["side"] for entry in client.submits], ["sell"])
        self.assertEqual(client.submits[0]["symbol"], "VTI")
        self.assertAlmostEqual(client.submits[0]["qty"], 3.0)

        # A later invocation gets a terminal sell and fresh account/position
        # state. It may then plan and submit only the delayed VXUS buy.
        sell_id = client.submits[0]["client_order_id"]
        client.broker_orders[sell_id].update({"status": "filled", "filled_qty": "3", "filled_avg_price": "100.00"})
        client._positions = [
            {"symbol": "VTI", "qty": "27", "side": "long", "market_value": "2700.00"},
            {"symbol": "VXUS", "qty": "15", "side": "long", "market_value": "1500.00"},
            {"symbol": "SGOV", "qty": "5", "side": "long", "market_value": "500.00"},
        ]
        client._cash = "1300.00"
        client._equity = "6000.00"
        submits_before = len(client.submits)
        recovery_audit = run_execution(_settings(), client, self.store)
        self.assertIsNone(recovery_audit["halted"], recovery_audit["halted"])
        recovery_submits = client.submits[submits_before:]
        self.assertEqual([entry["side"] for entry in recovery_submits], ["buy"])
        self.assertEqual(recovery_submits[0]["symbol"], "VXUS")
        self.assertAlmostEqual(recovery_submits[0]["notional"], 300.0)

    def test_monthly_target_persisted_and_recovery_run_recorded(self) -> None:
        client = FakeAlpaca()
        audit = run_execution(_settings(), client, self.store)
        self.assertIsNone(audit["halted"])
        run = self.store.run_for_month("2025-06")
        self.assertEqual(run.kind, "bootstrap")
        self.assertEqual(run.status, "open")  # orders submitted, run stays open
        self.assertAlmostEqual(run.target_weights[US_EQUITY], BASE_WEIGHTS[US_EQUITY])

        # Second attempt on the same month: broker orders still open -> halt,
        # and no duplicate submissions for the same client order IDs.
        first_ids = {entry["client_order_id"] for entry in client.submits}
        submits_count_before = len(client.submits)
        audit2 = run_execution(_settings(), client, self.store)
        self.assertIn("Open orders", audit2["halted"])
        new_submits = client.submits[submits_count_before:]
        self.assertEqual(new_submits, [])  # zero new POSTs while orders are open

    def test_not_first_market_day_and_no_open_run_halts(self) -> None:
        client = FakeAlpaca(calendar_days=[_bar_day(3)])
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("not the first official market day", audit["halted"])
        self.assertEqual(client.submits, [])

    def test_completed_month_blocks_reexecution(self) -> None:
        self.store.begin_run(
            run_id="rebalance-2025-06-a1",
            kind="rebalance",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        self.store.set_run_status("rebalance-2025-06-a1", "complete")
        client = FakeAlpaca()
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("already complete", audit["halted"])
        self.assertEqual(client.submits, [])


class CapSizingTests(RunExecutionTestCase):
    """Acceptance: broker market_value materially above qty x prior close must
    not let planned buys grow broker-valued managed exposure past the cap."""

    def test_unit_sizing_uses_broker_market_value_not_stale_close(self) -> None:
        # Broker says VTI is worth 4400 (10 sh @ 440); the prior close implies
        # only ~1161. Stale sizing would BUY VTI up to target; broker-value
        # sizing must instead treat VTI as over target and sell.
        positions = [{"symbol": "VTI", "qty": "10", "side": "long", "market_value": "4400.00"}]
        prices = {"VTI": 116.10, "VXUS": 60.00, "SGOV": 100.00}
        orders = build_orders_from_targets(
            targets={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            prices=prices,
            positions=positions,
            cash=600.0,
            managed_equity=5000.0,
            cash_buffer_percent=2.0,
        )
        vti_orders = [order for order in orders if order.symbol == "VTI"]
        self.assertTrue(all(order.side == "sell" for order in vti_orders))  # no VTI buy
        # Sell quantity uses the validated broker price (4400/10), not the stale close.
        sell = next(order for order in vti_orders if order.side == "sell")
        self.assertAlmostEqual(sell.qty, (4400.0 - 2700.0) / 440.0, places=6)
        # Counterfactual: sizing on the stale close would have planned a VTI buy
        # of ~1539, lifting broker-valued exposure to ~5939 > cap.
        stale_broker_value_after = 4400.0 + (2700.0 - 10.0 * 116.10)
        self.assertGreater(stale_broker_value_after, 5000.0)
        # Broker-value sizing: projected exposure after all planned trades.
        # VTI sells down to its target at the validated broker price; buys only
        # top up sleeves below target.
        projected = 2700.0  # VTI after its sell
        projected += sum(order.notional for order in orders if order.side == "buy")
        self.assertLessEqual(projected, 5000.0 + 0.01)

    def test_execute_planned_buys_cannot_push_broker_valued_exposure_past_cap(self) -> None:
        # Tracked VTI whose broker market_value (4400) far exceeds qty x stale
        # close (~1161). Stale sizing would plan a VTI buy and lift broker-valued
        # exposure to ~5939 > cap. Broker-value sizing must not.
        self.store.apply_fill("VTI", "buy", 10.0)
        client = FakeAlpaca(
            positions=[{"symbol": "VTI", "qty": "10", "side": "long", "market_value": "4400.00"}],
            cash="600.00",
            equity="5000.00",
        )
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Sell submitted", audit["halted"])
        self.assertTrue(all(entry["side"] == "sell" for entry in client.submits))

        # Stale (qty x prior-close) sizing would have planned a VTI buy of
        # ~1539 on top of broker value 4400: exposure ~5939 > cap 5000.
        # Project broker-valued exposure after the planned trades. The buys
        # are deliberately deferred to a later reconciled recovery run.
        targets = audit["target_weights"]
        broker_value = {"VTI": 4400.0, "VXUS": 0.0, "SGOV": 0.0}
        projected = 0.0
        for symbol in ("VTI", "VXUS", "SGOV"):
            symbol_buys = sum(e["notional"] for e in audit["planned_orders"] if e["symbol"] == symbol and e["side"] == "buy")
            symbol_sells = sum(e["qty"] for e in audit["planned_orders"] if e["symbol"] == symbol and e["side"] == "sell")
            if symbol_buys:
                projected += broker_value[symbol] + symbol_buys
            elif symbol_sells:
                projected += broker_value[symbol] - 440.0 * symbol_sells  # sell qty at broker price
            else:
                projected += broker_value[symbol]
        self.assertLessEqual(projected, 5000.0 + 0.01)
        self.assertGreater(targets["VTI"] * 5000.0, 0)  # sanity: targets present


class PendingOrphanGuardTests(RunExecutionTestCase):
    """A current-month pending orphan must only POST after ALL guards pass.
    Each test seeds an orphan and proves zero POSTs for one failing guard."""

    def _seed_pending_orphan(self, *, run_id: str = "bootstrap-2025-06-a1", symbol: str = "VTI", notional: float = 1000.0) -> str:
        self.store.begin_run(
            run_id=run_id,
            kind="bootstrap",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        coid = client_order_id("2025-06", 1, symbol, "buy")
        self.store.record_intent(client_order_id=coid, run_id=run_id, symbol=symbol, side="buy", qty=None, notional=notional)
        return coid

    def test_orphan_not_submitted_when_open_orders_present(self) -> None:
        coid = self._seed_pending_orphan()
        client = FakeAlpaca()
        client.broker_orders["stray"] = {"id": "stray", "status": "new", "symbol": "VTI", "side": "buy"}
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Open orders", audit["halted"])
        self.assertEqual(client.submits, [])  # zero POSTs
        self.assertEqual(self.store.intent(coid).status, "pending")  # orphan untouched

    def test_orphan_not_submitted_on_ownership_mismatch(self) -> None:
        coid = self._seed_pending_orphan()
        client = FakeAlpaca(
            positions=[{"symbol": "VTI", "qty": "10", "side": "long", "market_value": "1348.00"}],
            cash="3652.00",
        )
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Ownership mismatch", audit["halted"])
        self.assertEqual(client.submits, [])
        self.assertEqual(self.store.intent(coid).status, "pending")

    def test_recovery_orphan_with_insufficient_valid_cash_has_zero_submits(self) -> None:
        # Current broker state is valid and tracked, but this never-sent buy no
        # longer fits the cash remaining after the configured buffer.
        coid = self._seed_pending_orphan(notional=1000.0)
        self.store.apply_fill("SGOV", "buy", 50.0)
        client = FakeAlpaca(
            positions=[{"symbol": "SGOV", "qty": "50", "side": "long", "market_value": "5000.00"}],
            cash="500.00",
            equity="5500.00",
        )
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("exceeds current cash buffer", audit["halted"])
        self.assertEqual(client.submits, [])
        self.assertEqual(self.store.intent(coid).status, "pending")

    def test_orphan_not_submitted_when_its_old_notional_exceeds_current_cap(self) -> None:
        # The broker has no holdings, so the ordinary managed-cap guard passes.
        # A stale $4,900 orphan must still be blocked after the cap is lowered.
        coid = self._seed_pending_orphan(notional=4900.0)
        client = FakeAlpaca()
        audit = run_execution(_settings(max_managed_equity=1.0), client, self.store)
        self.assertIn("exceeds current managed-equity cap headroom", audit["halted"])
        self.assertEqual(client.submits, [])
        intent = self.store.intent(coid)
        self.assertEqual(intent.status, "pending")
        self.assertIsNone(intent.submitted_at)

    def test_orphan_not_submitted_on_cap_breach(self) -> None:
        coid = self._seed_pending_orphan()
        self.store.apply_fill("VTI", "buy", 20.0)  # ledger matches broker
        client = FakeAlpaca(
            positions=[{"symbol": "VTI", "qty": "20", "side": "long", "market_value": "2696.00"}],
            cash="2304.00",
        )
        audit = run_execution(_settings(max_managed_equity=2000.0), client, self.store)
        self.assertIn("exceeds the configured cap", audit["halted"])
        self.assertEqual(client.submits, [])
        self.assertEqual(self.store.intent(coid).status, "pending")

    def test_orphan_not_submitted_when_account_blocked(self) -> None:
        coid = self._seed_pending_orphan()
        client = FakeAlpaca(blocked=True)
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("blocked", audit["halted"])
        self.assertEqual(client.submits, [])
        self.assertEqual(self.store.intent(coid).status, "pending")

    def test_orphan_not_submitted_when_market_closed(self) -> None:
        coid = self._seed_pending_orphan()
        client = FakeAlpaca(is_open=False)
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Market closed", audit["halted"])
        self.assertEqual(client.submits, [])
        self.assertEqual(self.store.intent(coid).status, "pending")


class FilledSubmitFakeAlpaca(FakeAlpaca):
    """Market order that fills instantly: the orphan POST returns 'filled',
    so open_orders is empty again right after the submit."""

    def submit_market_order(self, **kwargs) -> dict:
        order = super().submit_market_order(**kwargs)
        order["status"] = "filled"
        return order


class RecoveryTests(RunExecutionTestCase):
    def test_ambiguous_timeout_with_dead_lookup_halts_and_marks_unknown(self) -> None:
        # Attempt 1: the POST is accepted broker-side but times out, and the
        # reconciliation lookup ALSO cannot reach the broker.
        client = FakeAlpaca(fail_submits=True, record_broker_order_on_failure=True, fail_lookups=True)
        audit = run_execution(_settings(), client, self.store)
        self.assertIsNotNone(audit["halted"])
        self.assertIn("Ambiguous submit", audit["halted"])
        self.assertEqual(client.events.count("submit"), 1)  # exactly one POST attempt
        self.assertEqual(client.submits, [])  # nothing was confirmed

        # The intent is durably recorded as unknown with a submit timestamp.
        unknown = self.store.intents(statuses={"unknown"})
        self.assertEqual(len(unknown), 1)
        self.assertIsNotNone(unknown[0].submitted_at)

        # Attempt 2 (restart): reconcile by client order ID finds the broker
        # order and records it; no new POST is ever created for it.
        client._fail_lookups = False
        submits_before = client.events.count("submit")
        audit2 = run_execution(_settings(), client, self.store)
        self.assertEqual(client.events.count("submit"), submits_before)  # no new POST
        self.assertIn("Open orders", audit2["halted"])  # the live broker order blocks; never duplicate
        intent = self.store.intent(unknown[0].client_order_id)
        self.assertEqual(intent.status, "submitted")
        self.assertEqual(intent.broker_order_id, "broker-1")
        self.assertIn(intent.client_order_id, client.broker_orders)

    def test_ambiguous_timeout_resolved_by_lookup_before_further_orders(self) -> None:
        # The POST times out, but the immediate broker lookup finds the order:
        # the run records it and only then continues with further orders.
        client = FakeAlpaca(fail_submits=True, record_broker_order_on_failure=True)
        audit = run_execution(_settings(), client, self.store)
        self.assertIsNone(audit["halted"], audit["halted"])
        # Every intended order was POSTed exactly once despite the timeout.
        intended = [entry["client_order_id"] for entry in audit["submitted_intents"]]
        self.assertTrue(intended)
        self.assertEqual(len(intended), len(set(intended)))
        self.assertEqual(client.events.count("submit"), len(intended))  # the timed-out order was NOT re-POSTed
        resolved = [entry for entry in audit["submitted_intents"] if entry.get("resolved_after_error")]
        self.assertEqual(len(resolved), 1)
        intent = self.store.intent(resolved[0]["client_order_id"])
        self.assertEqual(intent.status, "submitted")
        self.assertIsNotNone(intent.broker_order_id)

    def test_partial_fill_reconciliation_is_idempotent_and_recovering_run_does_not_duplicate(self) -> None:
        # Seed: a sell of 10 VTI was submitted in attempt 1; the broker filled 5
        # before the day expired. Broker now holds 5 VTI; ledger still says 10.
        run_id = "rebalance-2025-06-a1"
        self.store.begin_run(
            run_id=run_id,
            kind="rebalance",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        coid = self._seed_broker_sell_intent(run_id=run_id, symbol="VTI", qty=10.0, broker_id="broker-1")
        self.store.apply_fill("VTI", "buy", 10.0)  # original tracked acquisition
        client = FakeAlpaca(
            positions=[{"symbol": "VTI", "qty": "5", "side": "long", "market_value": "674.00"}],
            cash="4326.00",
        )
        client.broker_orders[coid] = {
            "id": "broker-1",
            "status": "expired",  # partial fill then day expiry
            "symbol": "VTI",
            "side": "sell",
            "client_order_id": coid,
            "filled_qty": "5",
            "filled_avg_price": "134.80",
        }

        audit = run_execution(_settings(), client, self.store)
        self.assertIsNone(audit["halted"], audit["halted"])

        # The fill delta was folded in exactly once: ledger 10 -> 5 == broker.
        self.assertAlmostEqual(self.store.ownership()["VTI"], 5.0)
        intent = self.store.intent(coid)
        self.assertEqual(intent.status, "expired")
        self.assertAlmostEqual(intent.recorded_filled_qty, 5.0)
        self.assertEqual(len(self.store.fills(coid)), 1)

        # The expired sell was NEVER resubmitted; recovery submitted only new
        # attempt-2 orders (top-up buys), each with a fresh deterministic ID.
        submitted_coids = [entry["client_order_id"] for entry in client.submits]
        self.assertNotIn(coid, submitted_coids)
        self.assertEqual(len(submitted_coids), len(set(submitted_coids)))
        self.assertTrue(all(entry["side"] == "buy" for entry in client.submits))

    def test_orphaned_pending_intent_is_resubmitted_once_under_its_original_id(self) -> None:
        # Crash between record_intent and the POST: intent persisted as
        # 'pending' with submitted_at NULL. Restart must send it exactly once.
        run_id = "bootstrap-2025-06-a1"
        self.store.begin_run(
            run_id=run_id,
            kind="bootstrap",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id=run_id, symbol="VTI", side="buy", qty=None, notional=1000.0)

        client = FakeAlpaca()
        audit = run_execution(_settings(), client, self.store)
        # The recovered orphan becomes a live broker order; the run halts
        # immediately after any orphan submission so a later recovery
        # invocation re-plans from fresh broker state.
        self.assertIsNotNone(audit["halted"])
        self.assertIn("Orphan recovery submitted or resolved", audit["halted"])

        matching = [entry for entry in client.submits if entry["client_order_id"] == coid]
        self.assertEqual(len(matching), 1)  # exactly one POST for the orphan
        self.assertEqual(matching[0]["notional"], 1000.0)
        intent = self.store.intent(coid)
        self.assertEqual(intent.status, "submitted")
        self.assertIsNotNone(intent.submitted_at)
        # No further order was planned in this same run.
        self.assertEqual(len(client.submits), 1)

    def test_multiple_orphans_stop_after_the_first_post_without_planning(self) -> None:
        run_id = "bootstrap-2025-06-a1"
        self.store.begin_run(
            run_id=run_id,
            kind="bootstrap",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        first = client_order_id("2025-06", 1, "VTI", "buy")
        second = client_order_id("2025-06", 1, "VXUS", "buy")
        self.store.record_intent(client_order_id=first, run_id=run_id, symbol="VTI", side="buy", qty=None, notional=1000.0)
        self.store.record_intent(client_order_id=second, run_id=run_id, symbol="VXUS", side="buy", qty=None, notional=1000.0)

        client = FilledSubmitFakeAlpaca()
        audit = run_execution(_settings(), client, self.store)
        self.assertIn("Orphan recovery submitted or resolved", audit["halted"])
        self.assertEqual(client.events.count("submit"), 1)
        self.assertEqual(len(client.submits), 1)
        self.assertEqual(client.submits[0]["client_order_id"], first)
        self.assertEqual(self.store.intent(first).status, "filled")
        self.assertEqual(self.store.intent(second).status, "pending")
        self.assertNotIn("daily_bars", client.events)

    def test_orphan_filled_and_open_orders_empty_halts_without_additional_planning(self) -> None:
        # Acceptance: an orphan POST that returns FILLED (open_orders empty)
        # must still halt — exactly one POST, and this run plans nothing else,
        # because broker state changed after the guards were evaluated.
        run_id = "bootstrap-2025-06-a1"
        self.store.begin_run(
            run_id=run_id,
            kind="bootstrap",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id=run_id, symbol="VTI", side="buy", qty=None, notional=1000.0)

        client = FilledSubmitFakeAlpaca()
        audit = run_execution(_settings(), client, self.store)
        self.assertIsNotNone(audit["halted"])
        self.assertIn("Orphan recovery submitted or resolved", audit["halted"])
        self.assertEqual(client.events.count("submit"), 1)  # exactly one POST
        self.assertEqual(len(client.submits), 1)
        # No further planning happened on stale cached account/positions: bars
        # were never fetched after the orphan POST.
        self.assertNotIn("daily_bars", client.events)
        self.assertEqual(self.store.intent(coid).status, "filled")


class TransmissionAttemptTests(RunExecutionTestCase):
    """Requirement: a durable transmission-attempt state committed BEFORE every
    POST. An 'attempted' intent is resolved by broker client-order-ID lookup on
    restart; only intents proven never attempted (pending, submitted_at NULL)
    may ever be posted automatically."""

    def test_attempt_marker_flips_pending_to_attempted_before_any_post(self) -> None:
        run_id = "bootstrap-2025-06-a1"
        self.store.begin_run(
            run_id=run_id,
            kind="bootstrap",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id=run_id, symbol="VTI", side="buy", qty=None, notional=1000.0)
        self.store.mark_attempted(coid)
        intent = self.store.intent(coid)
        self.assertEqual(intent.status, "attempted")
        self.assertIsNotNone(intent.submitted_at)  # stamped before the POST could run
        # Idempotent and never downgrades a stronger state.
        self.store.mark_attempted(coid)
        self.store.mark_submitted(coid, "broker-1")
        self.store.mark_attempted(coid)
        self.assertEqual(self.store.intent(coid).status, "submitted")

    def test_accepted_post_then_interruption_restart_resolves_with_zero_posts(self) -> None:
        # The broker ACCEPTS the POST, then the process dies before any response
        # or state update: the intent stays 'attempted' with the order live.
        client = FakeAlpaca(crash_on_submit=True)
        with self.assertRaises(RuntimeError):
            run_execution(_settings(), client, self.store)
        attempted = [intent for intent in self.store.intents() if intent.status == "attempted"]
        self.assertEqual(len(attempted), 1)
        self.assertIsNotNone(attempted[0].submitted_at)
        self.assertIn(attempted[0].client_order_id, client.broker_orders)  # broker accepted

        # Restart: state sync resolves the attempted intent by client-order-ID
        # lookup; it performs ZERO additional POSTs.
        submits_before = client.events.count("submit")
        audit = run_execution(_settings(), client, self.store)
        self.assertEqual(client.events.count("submit"), submits_before)  # no new POST
        self.assertEqual(client.submits, [])
        intent = self.store.intent(attempted[0].client_order_id)
        self.assertEqual(intent.status, "submitted")
        self.assertEqual(intent.broker_order_id, "broker-1")
        # The now-live broker order blocks further trading instead of duplicating.
        self.assertIn("Open orders", audit["halted"])


class OrphanSubmitErrorTests(RunExecutionTestCase):
    """Requirement: an orphan-POST error must resolve ambiguity through the
    shared helper, halt the audit, and leave NO pending repost-eligible intent."""

    def _seed_pending_orphan(self) -> str:
        run_id = "bootstrap-2025-06-a1"
        self.store.begin_run(
            run_id=run_id,
            kind="bootstrap",
            month_key="2025-06",
            trade_date="2025-06-02",
            target_weights={"VTI": 0.54, "VXUS": 0.36, "SGOV": 0.10},
            managed_equity=5000.0,
        )
        coid = client_order_id("2025-06", 1, "VTI", "buy")
        self.store.record_intent(client_order_id=coid, run_id=run_id, symbol="VTI", side="buy", qty=None, notional=1000.0)
        return coid

    def test_orphan_submit_error_halts_with_no_repost_eligible_intent_and_restart_posts_nothing(self) -> None:
        # The orphan POST raises AlpacaError and the lookup ALSO fails.
        coid = self._seed_pending_orphan()
        client = FakeAlpaca(fail_submits=True, fail_lookups=True)
        audit = run_execution(_settings(), client, self.store)
        self.assertIsNotNone(audit["halted"])
        self.assertIn("Ambiguous resubmission", audit["halted"])
        self.assertEqual(client.events.count("submit"), 1)  # exactly one POST attempt
        self.assertEqual(client.submits, [])
        intent = self.store.intent(coid)
        # Conservative attempted/unknown state: NOT pending, so not repost-eligible.
        self.assertEqual(intent.status, "unknown")
        self.assertIsNotNone(intent.submitted_at)

        # Restart: the unresolved unknown blocks the run; zero POSTs happen.
        client._fail_lookups = False
        submits_before = client.events.count("submit")
        audit2 = run_execution(_settings(), client, self.store)
        self.assertEqual(client.events.count("submit"), submits_before)
        self.assertEqual(client.submits, [])
        self.assertIsNotNone(audit2["halted"])  # still ambiguous: broker has no such order
        self.assertEqual(self.store.intent(coid).status, "unknown")

    def test_orphan_submit_timeout_resolved_by_lookup_records_order_exactly_once(self) -> None:
        # The orphan POST times out, but the broker accepted it: the shared
        # helper records the live order; no repost and no further orders this run.
        coid = self._seed_pending_orphan()
        client = FakeAlpaca(fail_submits=True, record_broker_order_on_failure=True)
        audit = run_execution(_settings(), client, self.store)
        self.assertEqual(client.events.count("submit"), 1)  # exactly one POST
        self.assertEqual(
            [entry for entry in client.submits if entry["client_order_id"] == coid],
            [],  # the submit list only records confirmed POSTs; this one was timed out
        )
        intent = self.store.intent(coid)
        self.assertEqual(intent.status, "submitted")
        self.assertEqual(intent.broker_order_id, "broker-1")
        # Any resolved orphan halts the run: a later recovery invocation must
        # re-plan from fresh broker state, never stale cached account data.
        self.assertIn("Orphan recovery submitted or resolved", audit["halted"])


if __name__ == "__main__":
    unittest.main()
