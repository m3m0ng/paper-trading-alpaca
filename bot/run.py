"""Plan-only audit runs plus the guarded, state-backed paper execution path.

`python -m bot.run`            -> plan only: audit file, no orders, no state writes.
`python -m bot.run --execute`  -> guarded paper execution. Requires
BOT_TRADING_ENABLED=true, an open Alpaca market clock, an unblocked account,
zero open orders, long-only positions restricted to VTI/VXUS/SGOV that exactly
match this bot's durable ownership ledger, broker equity that reconciles with
cash plus position market value, a positive configured
BOT_MAX_MANAGED_EQUITY, and either the first official market day of the month
or an open monthly run left over to recover. Any mismatch halts before any
order is created.

Every order intent is persisted in SQLite (bot/state.py) BEFORE the POST,
keyed by a deterministic client order ID, so a restart reconciles against the
broker by ID instead of resubmitting: an accepted-but-timed-out order can
never be duplicated. Buys are sized on cash already on hand — never on
unfilled sell proceeds. An invocation that submits a sell stops before any
buy; a later recovery run completes the rebalance only after broker
reconciliation confirms the sell is terminal.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.alpaca import AlpacaClient, AlpacaError
from bot.config import Settings
from bot.state import ExecutionLease, QTY_MATCH_TOLERANCE, TERMINAL_STATUSES, StateStore, client_order_id
from bot.strategy import (
    INTERNATIONAL_EQUITY,
    SYMBOLS,
    US_EQUITY,
    Signal,
    apply_volatility_cap,
    evaluate_signal,
    target_weights,
)

EASTERN = ZoneInfo("America/New_York")
MIN_ORDER_NOTIONAL = 1.00
# Extra tolerance when checking that bot-owned position value stays within cap.
MANAGED_CAP_TOLERANCE_PERCENT = 1.0
# Named tolerance (USD) for the broker accounting identity
# equity == cash + sum(position market_value) checked before any POST.
EQUITY_ACCOUNTING_TOLERANCE_USD = 1.00

# 'attempted' means a transmission attempt was durably stamped before a POST;
# on restart such intents are resolved by broker lookup, never re-POSTed.
LIVE_INTENT_STATUSES = frozenset({"pending", "attempted", "submitted", "partially_filled", "unknown"})


@dataclass(frozen=True)
class PlannedOrder:
    symbol: str
    side: str
    notional: float | None = None
    qty: float | None = None


def _completed_closes(bars: list[dict], today: date) -> list[float]:
    # The current daily candle is incomplete during market hours, so it cannot be a signal input.
    closes: list[float] = []
    for bar in bars:
        timestamp = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(EASTERN)
        if timestamp.date() < today:
            closes.append(float(bar["c"]))
    return closes


def _position_value(position: dict | None, stale_price: float) -> float:
    """Broker-current market value for a position, preferred over qty * close.

    The signal price is a prior completed IEX close and can sit far below the
    broker's live valuation. Sizing from the stale value could plan buys that
    push broker-valued managed exposure above BOT_MAX_MANAGED_EQUITY, so the
    broker-reported market_value wins whenever it is usable. Returns 0.0 when
    there is no tracked position."""
    if position is None:
        return 0.0
    qty = float(position.get("qty") or 0)
    market_value = position.get("market_value")
    if qty > 0 and market_value is not None:
        value = float(market_value)
        if value > 0:
            return value
    return qty * stale_price


def _validated_price(position: dict | None, stale_price: float) -> float:
    """Per-share price implied by the broker position (market_value / qty).

    Used for sell quantities so the sold notional matches broker value rather
    than a stale close. Falls back to the signal close when the broker data
    cannot imply a positive, finite price."""
    if position is None:
        return stale_price
    qty = float(position.get("qty") or 0)
    if qty <= 0:
        return stale_price
    price = _position_value(position, stale_price) / qty
    return price if price > 0 and math.isfinite(price) else stale_price


def build_orders_from_targets(
    *,
    targets: dict[str, float],
    prices: dict[str, float],
    positions: list[dict],
    cash: float,
    managed_equity: float,
    cash_buffer_percent: float,
) -> list[PlannedOrder]:
    """Long-only target changes. Sells come first; buys are sized on cash already
    on hand (never on unfilled sell proceeds), scaled down if cash is short.

    Held value is taken from the broker's current market_value (not qty times a
    prior close), so planned buys can never grow broker-valued managed exposure
    past the configured cap via a stale-price gap."""
    position_by_symbol = {position["symbol"]: position for position in positions if position["symbol"] in SYMBOLS}
    orders: list[PlannedOrder] = []

    for symbol in SYMBOLS:
        position = position_by_symbol.get(symbol)
        held_value = _position_value(position, prices[symbol])
        target_value = managed_equity * targets[symbol]
        if held_value - target_value >= MIN_ORDER_NOTIONAL:
            sell_price = _validated_price(position, prices[symbol])
            orders.append(PlannedOrder(symbol=symbol, side="sell", qty=(held_value - target_value) / sell_price))

    buy_budget = max(0.0, cash * (1 - cash_buffer_percent / 100))
    requested_buys: list[tuple[str, float]] = []
    for symbol in SYMBOLS:
        held_value = _position_value(position_by_symbol.get(symbol), prices[symbol])
        target_value = managed_equity * targets[symbol]
        if target_value - held_value >= MIN_ORDER_NOTIONAL:
            requested_buys.append((symbol, target_value - held_value))

    total_requested = sum(amount for _, amount in requested_buys)
    scale = min(1.0, buy_budget / total_requested) if total_requested else 0.0
    for symbol, amount in requested_buys:
        notional = amount * scale
        if notional >= MIN_ORDER_NOTIONAL:
            orders.append(PlannedOrder(symbol=symbol, side="buy", notional=notional))
    return orders


def build_plan(
    *,
    targets: dict[str, float],
    signals: dict[str, Signal],
    positions: list[dict],
    cash: float,
    managed_equity: float,
    cash_buffer_percent: float,
) -> list[PlannedOrder]:
    """Plan-mode wrapper: build orders from the already-audited target weights."""
    prices = {symbol: signal.latest_close for symbol, signal in signals.items()}
    return build_orders_from_targets(
        targets=targets,
        prices=prices,
        positions=positions,
        cash=cash,
        managed_equity=managed_equity,
        cash_buffer_percent=cash_buffer_percent,
    )


def _is_first_market_day(client: AlpacaClient, today: date) -> bool:
    days = client.calendar(today.replace(day=1), today)
    return bool(days) and date.fromisoformat(days[0]["date"]) == today


def _signals_and_closes_from_bars(client: AlpacaClient, today: date) -> tuple[dict[str, Signal], dict[str, list[float]]]:
    bars = client.daily_bars(list(SYMBOLS), today - timedelta(days=450), today)
    closes = {symbol: _completed_closes(bars.get(symbol, []), today) for symbol in SYMBOLS}
    # SGOV's signal is not used for targeting; its latest close is still needed for sizing.
    signals = {symbol: evaluate_signal(symbol, closes[symbol]) for symbol in SYMBOLS}
    return signals, closes


# ---------------------------------------------------------------------------
# Durable-state reconciliation
# ---------------------------------------------------------------------------

def _resolve_intent_ambiguity(store: StateStore, client: AlpacaClient, coid: str, context: str) -> dict | None:
    """Shared ambiguity resolution for a possibly-transmitted order intent.

    Looks the broker order up by client order ID (the only reliable key after a
    timeout or crash) and folds a found order into durable state. Returns the
    broker order when found. On ANY unresolved outcome — the lookup itself
    fails, or the broker has no order with this ID — the intent is durably
    marked 'unknown' (conservative attempted/unknown state) and None is
    returned; 'unknown' is never repost-eligible and always halts execution.
    """
    try:
        broker_order = client.order_by_client_order_id(coid)
    except AlpacaError as error:
        store.mark_status(coid, "unknown", note=f"{context}: broker lookup failed: {error}")
        return None
    if broker_order is None:
        store.mark_status(
            coid,
            "unknown",
            note=f"{context}: broker has no order with this client order id",
        )
        return None
    store.mark_submitted(coid, broker_order.get("id"))
    store.apply_broker_order(coid, broker_order)
    return broker_order


def sync_state_with_broker(store: StateStore, client: AlpacaClient, *, month_key: str) -> tuple[list[str], dict]:
    """Fold broker truth into durable state before any new order work.

    Read-only at the broker: for every non-terminal intent that was possibly
    sent (submitted/partially_filled/unknown, plus 'attempted' — a transmission
    stamped before a POST, then a crash), look the order up by client order ID
    and apply the fill delta (idempotent). Never blindly resubmit a sent intent.
    A POST that failed ambiguously stays 'unknown' unless the broker lookup can
    resolve it; 'unknown' is always a halt.

    Orphaned 'pending' intents (recorded, then crashed before the POST) are
    NOT submitted here: their ID was never transmitted, but a POST is only
    allowed after ALL execution guards pass. Stale orphans from other months
    are marked 'abandoned' (state write only); current-month orphans stay
    'pending' until the guarded `submit_pending_orphans` step.
    """
    halt_reasons: list[str] = []
    summary = {"reconciled_intents": 0, "abandoned_orphans": 0, "unknown_intents": 0}

    # 1. Reconcile every non-terminal intent that was possibly sent to the
    #    broker. 'attempted' intents (transmission stamped before the POST,
    #    then a crash) MUST be resolved by client-order-ID lookup here; only
    #    intents still 'pending' are proven never attempted.
    for intent in store.intents(statuses={"submitted", "partially_filled", "unknown", "attempted"}):
        if intent.status in {"submitted", "partially_filled"} and intent.submitted_at is None:
            continue  # defensive: status says sent, timestamp says never; treated as orphan below
        if _resolve_intent_ambiguity(store, client, intent.client_order_id, "reconciliation") is None:
            halt_reasons.append(
                f"Ambiguous order intent {intent.client_order_id} ({intent.symbol} {intent.side}); "
                "manual review required before any further orders."
            )
        else:
            summary["reconciled_intents"] += 1

    # 2. Stale orphans from other months: mark abandoned (state write only;
    #    no POST ever happens during reconciliation).
    for intent in store.intents(statuses={"pending"}):
        if intent.submitted_at is not None:
            continue
        run_prefix = intent.run_id.rsplit("-a", 1)[0]
        if run_prefix not in {f"bootstrap-{month_key}", f"rebalance-{month_key}", f"recovery-{month_key}"}:
            store.mark_status(intent.client_order_id, "abandoned", note="never sent; belongs to another month")
            summary["abandoned_orphans"] += 1

    summary["unknown_intents"] = len(store.intents(statuses={"unknown"}))
    return halt_reasons, summary


def _pending_orphan_revalidation_error(
    intent,
    *,
    managed_cap: float,
    owned_value: float,
    cash: float,
    cash_buffer_percent: float,
    broker_qty: dict[str, float],
) -> str | None:
    """Reject a stale never-sent intent that is unsafe under today's state.

    Pending intents contain the old plan's sizing, so they cannot inherit its
    cap or cash assumptions after a configuration change. Quantity buys cannot
    be bounded from the durable intent without a current price, so fail closed
    rather than estimating one here.
    """
    if intent.symbol not in SYMBOLS or intent.side not in {"buy", "sell"}:
        return f"Pending orphan {intent.client_order_id} has an unsupported symbol or side."
    if not all(math.isfinite(value) for value in (managed_cap, owned_value, cash, cash_buffer_percent)):
        return f"Pending orphan {intent.client_order_id} cannot be revalidated from non-finite account values."

    if intent.side == "buy":
        if intent.qty is not None or intent.notional is None or not math.isfinite(intent.notional) or intent.notional <= 0:
            return f"Pending buy orphan {intent.client_order_id} lacks a safe positive notional."
        available_cash = max(0.0, cash * (1 - cash_buffer_percent / 100))
        cap_headroom = max(0.0, managed_cap - owned_value)
        if intent.notional > available_cash:
            return (
                f"Pending buy orphan {intent.client_order_id} exceeds current cash buffer "
                f"(${intent.notional:.2f} > ${available_cash:.2f})."
            )
        if intent.notional > cap_headroom:
            return (
                f"Pending buy orphan {intent.client_order_id} exceeds current managed-equity cap headroom "
                f"(${intent.notional:.2f} > ${cap_headroom:.2f})."
            )
        return None

    if intent.notional is not None or intent.qty is None or not math.isfinite(intent.qty) or intent.qty <= 0:
        return f"Pending sell orphan {intent.client_order_id} lacks a safe positive quantity."
    if intent.qty > broker_qty.get(intent.symbol, 0.0) + QTY_MATCH_TOLERANCE:
        return f"Pending sell orphan {intent.client_order_id} exceeds the current managed position quantity."
    return None


def submit_pending_orphans(
    store: StateStore,
    client: AlpacaClient,
    *,
    month_key: str,
    lease: ExecutionLease,
    managed_cap: float,
    owned_value: float,
    cash: float,
    cash_buffer_percent: float,
    broker_qty: dict[str, float],
) -> tuple[list[str], dict]:
    """POST orphaned current-month 'pending' intents under their original
    client order IDs.

    GUARDED: only call after every execution guard has passed (market open,
    account unblocked, zero open orders, long-only allowed+tracked positions,
    equity accounting, cap, bootstrap cash, monthly-run eligibility). It also
    revalidates each stale intent's sizing against the current cap, broker
    value, cash buffer, and sell-before-buy recovery ordering before stamping
    it attempted. This is the only place a never-transmitted intent may be
    sent, and it must never run while any guard is failing.
    """
    halt_reasons: list[str] = []
    summary = {"resubmitted_orphans": 0, "orphan_post_attempted": False}
    current_run_prefixes = {
        f"bootstrap-{month_key}",
        f"rebalance-{month_key}",
        f"recovery-{month_key}",
    }
    current_orphans = [
        intent
        for intent in store.intents(statuses={"pending"})
        if intent.submitted_at is None and intent.run_id.rsplit("-a", 1)[0] in current_run_prefixes
    ]
    # A pending sell must recover before any pending buy. We return after one
    # POST, so later buys wait for a fresh run and settled, already-held cash.
    current_orphans.sort(key=lambda intent: intent.side != "sell")
    for intent in current_orphans:
        validation_error = _pending_orphan_revalidation_error(
            intent,
            managed_cap=managed_cap,
            owned_value=owned_value,
            cash=cash,
            cash_buffer_percent=cash_buffer_percent,
            broker_qty=broker_qty,
        )
        if validation_error:
            halt_reasons.append(validation_error)
            return halt_reasons, summary
        # Fence every POST. Losing the durable lease is a fail-closed halt;
        # do not even stamp an intent as attempted when this process is stale.
        if not store.holds_execution_lease(lease):
            halt_reasons.append("Execution lease was lost before orphan recovery POST; halting.")
            return halt_reasons, summary
        # Stamp the transmission attempt BEFORE the POST, committed first:
        # after this point the intent can never be treated as never-attempted.
        store.mark_attempted(intent.client_order_id)
        summary["orphan_post_attempted"] = True
        try:
            broker_order = client.submit_market_order(
                symbol=intent.symbol,
                side=intent.side,
                client_order_id=intent.client_order_id,
                qty=intent.qty,
                notional=intent.notional,
            )
        except AlpacaError as error:
            store.add_note(intent.client_order_id, f"resubmission error: {error}")
            resolved = _resolve_intent_ambiguity(store, client, intent.client_order_id, "resubmission")
            if resolved is None:
                # Conservative attempted/unknown state: no pending repost-
                # eligible intent remains, and the audit halts.
                halt_reasons.append(f"Ambiguous resubmission of {intent.client_order_id}; halting.")
            else:
                # Resolved by lookup: the order IS live at the broker.
                summary["resubmitted_orphans"] += 1
        else:
            store.mark_submitted(intent.client_order_id, broker_order.get("id"))
            store.apply_broker_order(intent.client_order_id, broker_order)
            summary["resubmitted_orphans"] += 1
        # A POST (including a resolved timeout) changes broker state. Return
        # immediately: never submit another orphan or plan from stale guards.
        return halt_reasons, summary
    return halt_reasons, summary


def _submit_one(
    store: StateStore,
    client: AlpacaClient,
    *,
    run_id: str,
    symbol: str,
    side: str,
    coid: str,
    qty: float | None,
    notional: float | None,
    submitted_log: list[dict],
    lease: ExecutionLease,
) -> str | None:
    """Record the intent BEFORE the POST, submit, and fold the result into
    state. Returns a halt reason on ambiguity, else None."""
    # Idempotent: if this exact intent was already recorded (crash retry), this
    # call is a no-op instead of creating a second order.
    store.record_intent(client_order_id=coid, run_id=run_id, symbol=symbol, side=side, qty=qty, notional=notional)
    existing = store.intent(coid)
    if existing.status != "pending" or existing.submitted_at is not None:
        # Defense in depth: only an intent proven never attempted (pending,
        # submitted_at NULL) may be POSTed from here. Anything attempted or
        # further along is resolved by broker lookup during state sync instead.
        return None
    # Fence every POST. A stale holder must halt before changing intent state
    # or sending an order after another execution has taken the lease.
    if not store.holds_execution_lease(lease):
        return "Execution lease was lost before order POST; halting."
    # Durable transmission-attempt state, committed BEFORE the POST. From this
    # moment the intent is no longer "never attempted": a crash here leaves an
    # 'attempted' intent that restart must resolve by broker lookup.
    store.mark_attempted(coid)
    try:
        broker_order = client.submit_market_order(
            symbol=symbol, side=side, client_order_id=coid, qty=qty, notional=notional
        )
    except AlpacaError as error:
        store.add_note(coid, f"submit error: {error}")
        # Ambiguous outcome: resolve by client order ID before any further
        # order may be created. Never blindly retry.
        if _resolve_intent_ambiguity(store, client, coid, "submit") is None:
            return (
                f"Ambiguous submit for {coid} ({symbol} {side}); halted before creating any further order. "
                "Manual review required."
            )
        submitted_log.append({"client_order_id": coid, "symbol": symbol, "side": side, "resolved_after_error": True})
        return None
    store.mark_submitted(coid, broker_order.get("id"))
    store.apply_broker_order(coid, broker_order)
    submitted_log.append(
        {
            "client_order_id": coid,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "notional": notional,
            "broker_order_id": broker_order.get("id"),
        }
    )
    return None


# ---------------------------------------------------------------------------
# Guarded execution
# ---------------------------------------------------------------------------

def run_execution(settings: Settings, client: AlpacaClient, store: StateStore) -> dict:
    """One guarded execution attempt with a durable exclusive lease.

    Configuration guards remain network-free. Once enabled, the lease is held
    through every broker-state guard and POST, then released even on a halt or
    exception. A process crash intentionally leaves the lease fail-closed.
    """
    audit: dict = {"mode": "execute", "guards": [], "submitted_intents": [], "halted": None}

    def halt(reason: str) -> dict:
        audit["halted"] = reason
        return audit

    if not settings.trading_enabled:
        return halt("BOT_TRADING_ENABLED is not true; execution is disabled.")
    if not math.isfinite(settings.max_managed_equity) or settings.max_managed_equity <= 0:
        return halt("BOT_MAX_MANAGED_EQUITY must be a finite positive number for execution.")

    lease = store.acquire_execution_lease()
    if lease is None:
        audit["guards"].append(
            {"check": "execution_lease", "passed": False, "detail": "Another execution holds the durable lease."}
        )
        return halt("Execution refused: another execution holds the durable lease.")
    audit["guards"].append(
        {"check": "execution_lease", "passed": True, "detail": f"Acquired execution fence {lease.fence}."}
    )
    try:
        return _run_execution_with_lease(settings, client, store, audit, lease)
    finally:
        store.release_execution_lease(lease)


def _run_execution_with_lease(
    settings: Settings, client: AlpacaClient, store: StateStore, audit: dict, lease: ExecutionLease
) -> dict:
    """Run the broker guards and submissions while `lease` remains held."""
    def halt(reason: str) -> dict:
        audit["halted"] = reason
        return audit

    def guard(name: str, ok: bool, detail: str) -> str | None:
        audit["guards"].append({"check": name, "passed": ok, "detail": detail})
        return None if ok else detail

    managed_cap = settings.max_managed_equity
    clock = client.clock()
    now = datetime.fromisoformat(clock["timestamp"].replace("Z", "+00:00")).astimezone(EASTERN)
    today = now.date()
    month_key = today.strftime("%Y-%m")
    audit["timestamp_et"] = now.isoformat()
    if guard("market_open", bool(clock["is_open"]), "Alpaca clock must report the market open."):
        return halt("Market closed per Alpaca clock.")

    account = client.account()
    blocked = bool(account.get("trading_blocked") or account.get("account_blocked"))
    if guard("account_unblocked", not blocked, "Alpaca must not report a trading/account block."):
        return halt("Execution refused: Alpaca reports the account is blocked for trading.")

    cash = float(account["cash"])
    equity = float(account["equity"])
    audit["account"] = {"cash": cash, "equity": equity, "managed_cap": managed_cap}

    # Reconcile durable state against the broker BEFORE any new order work.
    halt_reasons, sync_summary = sync_state_with_broker(store, client, month_key=month_key)
    audit["state_sync"] = sync_summary
    if halt_reasons:
        return halt("; ".join(halt_reasons))

    positions = client.positions()
    broker_qty: dict[str, float] = {}
    owned_value = 0.0
    for position in positions:
        symbol = position.get("symbol", "")
        qty = float(position.get("qty") or 0)
        if symbol not in SYMBOLS:
            return halt(f"Untracked broker position {symbol}; this bot never trades symbols it did not acquire.")
        if qty < 0 or (position.get("side") or "long") != "long":
            return halt(f"Non-long position {symbol}; the bot is long-only.")
        broker_qty[symbol] = qty
        owned_value += float(position.get("market_value") or 0)

    # Broker accounting sanity before any POST: equity must equal cash plus
    # the market value of the positions the broker reports. A mismatch means
    # the broker state is not what the guards below assume, so stop here.
    if guard(
        "equity_accounting",
        abs(equity - (cash + owned_value)) <= EQUITY_ACCOUNTING_TOLERANCE_USD,
        f"equity ${equity:.2f} vs cash ${cash:.2f} + positions ${owned_value:.2f}",
    ):
        return halt(
            f"Account accounting mismatch: equity ${equity:.2f} != cash ${cash:.2f} + position market value "
            f"${owned_value:.2f} (tolerance ${EQUITY_ACCOUNTING_TOLERANCE_USD:.2f}); halted before any order."
        )

    if guard("no_open_orders", client.open_orders() == [], "All orders must be closed before planning."):
        return halt("Open orders present; wait for fills/cancellation, then rerun.")

    # Tracked ownership: broker positions must exactly match this bot's own ledger.
    tracked = store.ownership()
    mismatches = [
        f"{symbol}: tracked={tracked.get(symbol, 0.0)} broker={broker_qty.get(symbol, 0.0)}"
        for symbol in sorted(set(tracked) | set(broker_qty))
        if abs(tracked.get(symbol, 0.0) - broker_qty.get(symbol, 0.0)) > QTY_MATCH_TOLERANCE
    ]
    if guard("ownership_matches_ledger", not mismatches, "Broker positions must equal the ownership ledger."):
        return halt("Ownership mismatch — " + "; ".join(mismatches))

    # Managed cap: bot-owned positions may never exceed the configured cap.
    if guard(
        "managed_cap",
        owned_value <= managed_cap * (1 + MANAGED_CAP_TOLERANCE_PERCENT / 100),
        f"owned value ${owned_value:.2f} vs cap ${managed_cap:.2f}",
    ):
        return halt(f"Bot-owned position value ${owned_value:.2f} exceeds the configured cap ${managed_cap:.2f}.")

    # Bootstrap: only from a clean slate with enough cash to cover the whole cap.
    bootstrap = not broker_qty and not tracked
    if bootstrap:
        if guard("bootstrap_cash_covers_cap", cash >= managed_cap, f"cash ${cash:.2f} vs cap ${managed_cap:.2f}"):
            return halt(
                f"Bootstrap requires available cash >= BOT_MAX_MANAGED_EQUITY (${cash:.2f} < ${managed_cap:.2f})."
            )

    existing_run = store.run_for_month(month_key)
    if existing_run and existing_run.status == "complete":
        return halt(f"Monthly run for {month_key} is already complete; next rebalance on the first market day.")
    attempt = store.count_runs_for_month(month_key) + 1
    is_first_market_day = _is_first_market_day(client, today)
    if existing_run is None and not is_first_market_day:
        return halt("Today is not the first official market day and there is no open run to recover.")

    # Orphan recovery POSTs only now: every guard above has passed (market
    # open, account unblocked, positions, accounting, open orders, cap,
    # bootstrap cash, monthly-run eligibility).
    orphan_reasons, orphan_summary = submit_pending_orphans(
        store,
        client,
        month_key=month_key,
        lease=lease,
        managed_cap=managed_cap,
        owned_value=owned_value,
        cash=cash,
        cash_buffer_percent=settings.cash_buffer_percent,
        broker_qty=broker_qty,
    )
    audit["orphan_submission"] = orphan_summary
    if orphan_reasons:
        return halt("; ".join(orphan_reasons))
    # ANY orphan submitted or resolved means broker state moved after the
    # guards were checked (fills may already have landed, even if open_orders
    # is empty again). Stop here and let a later recovery invocation re-plan
    # from fresh broker data instead of acting on stale cached account/positions.
    if orphan_summary["orphan_post_attempted"]:
        return halt(
            f"Orphan recovery submitted or resolved {orphan_summary['resubmitted_orphans']} order(s); "
            "halting so a later recovery run re-plans from fresh broker state."
        )

    # Signals use only completed bars (no look-ahead). A recovery run keeps the
    # monthly target persisted on its first attempt.
    signals, closes = _signals_and_closes_from_bars(client, today)
    prices = {symbol: signal.latest_close for symbol, signal in signals.items()}
    if existing_run is None:
        base_targets = target_weights(signals)
        targets, realized_volatility, volatility_multiplier = apply_volatility_cap(
            base_targets,
            {US_EQUITY: closes[US_EQUITY], INTERNATIONAL_EQUITY: closes[INTERNATIONAL_EQUITY]},
            settings.target_annual_volatility,
        )
        audit["realized_annual_volatility"] = realized_volatility
        audit["volatility_multiplier"] = volatility_multiplier
    else:
        targets = existing_run.target_weights
    audit["target_weights"] = targets

    orders = build_orders_from_targets(
        targets=targets,
        prices=prices,
        positions=positions,
        cash=cash,
        managed_equity=min(equity, managed_cap),
        cash_buffer_percent=settings.cash_buffer_percent,
    )
    audit["planned_orders"] = [asdict(order) for order in orders]

    kind = "recovery" if existing_run is not None else ("bootstrap" if bootstrap else "rebalance")
    run_id = f"{kind}-{month_key}-a{attempt}"
    store.begin_run(
        run_id=run_id,
        kind=kind,
        month_key=month_key,
        trade_date=today.isoformat(),
        target_weights=targets,
        managed_equity=managed_cap,
    )

    halted_reason = None
    for order in orders:
        coid = client_order_id(month_key, attempt, order.symbol, order.side)
        previous = store.intent(coid)
        if previous is not None and (previous.submitted_at is not None or previous.status != "pending"):
            audit["submitted_intents"].append({"client_order_id": coid, "skipped": "already recorded/submitted"})
            continue
        halted_reason = _submit_one(
            store,
            client,
            run_id=run_id,
            symbol=order.symbol,
            side=order.side,
            coid=coid,
            qty=order.qty,
            notional=order.notional,
            submitted_log=audit["submitted_intents"],
            lease=lease,
        )
        if halted_reason:
            break
        if order.side == "sell":
            # A sell changes cash and positions after the guards and plan were
            # read. Do not submit a buy until a later invocation reconciles
            # terminal sell status against fresh broker state.
            halted_reason = "Sell submitted; halting until a later recovery run reconciles fresh broker state."
            break

    # build_orders_from_targets plans sells before buys; verify the submitted
    # sequence actually kept that ordering.
    submitted_sides = [entry["side"] for entry in audit["submitted_intents"] if not entry.get("skipped")]
    if submitted_sides and submitted_sides != sorted(submitted_sides, key=lambda s: 0 if s == "sell" else 1):
        return halt("Internal ordering violation: a buy was submitted before a sell.")

    if halted_reason:
        return halt(halted_reason)

    # The monthly run completes only once nothing live is left for this month.
    live_intents = [
        intent
        for intent in store.intents(month_key=month_key)
        if intent.status in LIVE_INTENT_STATUSES
    ]
    if live_intents:
        audit["run_status"] = "open"
    else:
        for run in store.runs_for_month(month_key):
            store.set_run_status(run.run_id, "complete")
        audit["run_status"] = "complete"
    return audit


def _write_audit(payload: dict, *, filename_prefix: str = "run") -> Path:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(tz=EASTERN).strftime("%Y%m%dT%H%M%S%z")
    path = log_dir / f"{filename_prefix}-{timestamp}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _run_plan(settings: Settings, client: AlpacaClient) -> None:
    clock = client.clock()
    now = datetime.fromisoformat(clock["timestamp"].replace("Z", "+00:00")).astimezone(EASTERN)
    today = now.date()
    if not clock["is_open"]:
        raise SystemExit("No action: the official Alpaca market clock is closed.")

    account = client.account()
    if account.get("trading_blocked") or account.get("account_blocked"):
        raise SystemExit("Execution refused: Alpaca reports the account is blocked for trading.")

    signals, closes = _signals_and_closes_from_bars(client, today)
    base_targets = target_weights(signals)
    targets, realized_volatility, volatility_multiplier = apply_volatility_cap(
        base_targets,
        {US_EQUITY: closes[US_EQUITY], INTERNATIONAL_EQUITY: closes[INTERNATIONAL_EQUITY]},
        settings.target_annual_volatility,
    )
    positions = client.positions()
    cash = float(account["cash"])
    equity = float(account["equity"])
    # BOT_MAX_MANAGED_EQUITY caps what this bot may ever manage. The agreed
    # initial V1 configuration is 5000; it stays in .env, not hard-coded.
    managed_equity = min(equity, settings.max_managed_equity) if settings.max_managed_equity else equity
    orders = build_plan(
        targets=targets,
        signals=signals,
        positions=positions,
        cash=cash,
        managed_equity=managed_equity,
        cash_buffer_percent=settings.cash_buffer_percent,
    )

    audit = {
        "mode": "plan",
        "timestamp_et": now.isoformat(),
        "account": {"cash": cash, "equity": equity, "managed_equity": managed_equity},
        "signals": {symbol: asdict(signal) for symbol, signal in signals.items()},
        "base_target_weights": base_targets,
        "target_weights": targets,
        "realized_annual_volatility": realized_volatility,
        "volatility_multiplier": volatility_multiplier,
        "planned_orders": [asdict(order) for order in orders],
        "submitted_orders": [],
    }

    path = _write_audit(audit)
    print(f"PLAN complete. Planned orders: {len(orders)}. Audit: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan the V1 paper portfolio, or run guarded paper execution.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the guarded paper execution path (requires BOT_TRADING_ENABLED=true and all safety guards).",
    )
    args = parser.parse_args()

    settings = Settings.load()
    client = AlpacaClient(settings)

    if args.execute:
        store = StateStore()
        try:
            audit = run_execution(settings, client, store)
        finally:
            store.close()
        path = _write_audit(audit, filename_prefix="execute")
        if audit["halted"]:
            raise SystemExit(f"EXECUTION HALTED: {audit['halted']} (audit: {path})")
        print(
            f"EXECUTION complete ({audit['run_status']}). Submitted: {len(audit['submitted_intents'])}. Audit: {path}"
        )
        return

    _run_plan(settings, client)


if __name__ == "__main__":
    main()
