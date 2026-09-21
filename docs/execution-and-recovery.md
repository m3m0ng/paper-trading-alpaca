# Execution, durable state, and recovery (V1)

This document is the operator reference for `python -m bot.run --execute`. It describes what the guarded execution path enforces, where state lives, and how restarts recover. Nothing here submits a live order: the paper URL (`https://paper-api.alpaca.markets`) is enforced in `bot/config.py`.

## State

- `state/bot-state.sqlite3` (git-ignored). SQLite via stdlib `sqlite3`; schema and access in `bot/state.py`.
- Tables:
  - `runs` — one row per monthly intent/attempt. Persists the month's target weights BEFORE any order is planned, so a recovery run reuses the same target instead of re-deciding.
  - `order_intents` — one row per intended order, written BEFORE the POST. Keyed by a deterministic client order ID (`v1-<month>-a<attempt>-<symbol>-<side>-<hash>`). `UNIQUE(run_id, symbol, side)` plus the primary key make duplicate rows impossible.
  - `order_fills` — fill-delta ledger keyed by `(client_order_id, cumulative_filled_qty)`, so replaying the same broker snapshot is a no-op (idempotent reconciliation).
  - `ownership` — shares owned, derived ONLY from this bot's own fills (buy adds, sell removes).
  - `execution_leases` and `execution_lease_fences` — one durable, non-expiring execution holder plus a monotonic fence. A second execution halts before broker calls or POSTs; a crashed holder fails closed pending operator investigation.

## Deterministic client order IDs

The ID is a pure function of `(month_key, attempt, symbol, side)`. The same intended trade always maps to the same ID, which is what makes Alpaca-side deduplication and restart lookup possible. A recovery attempt increments `attempt`, so genuinely new orders get fresh IDs while already-sent intents keep theirs.

## Execution guard chain (in order, before any POST)

1. `BOT_TRADING_ENABLED=true` and `BOT_MAX_MANAGED_EQUITY > 0` (configuration only — the agreed initial value is 5000 in `.env`, never hard-coded). These two checks happen before any network call.
2. A durable exclusive execution lease is acquired. It remains held and fenced through every broker check and order POST.
3. Alpaca `/v2/clock` reports the market open.
4. Account not trading/account blocked.
5. Durable-state reconciliation (below) leaves no `unknown` intent.
6. Zero open orders.
7. Broker positions are long-only and restricted to `VTI`/`VXUS`/`SGOV`.
8. Broker positions exactly equal the ownership ledger (tolerance 0.0005 shares). A pre-existing or untracked holding halts — it is never sold or traded around.
9. Bot-owned position value within the configured cap (1% tolerance).
10. Bootstrap (empty broker + empty ledger) additionally requires available cash >= the configured cap.
11. Today is the first official market day of the month (Alpaca calendar), OR an open monthly run exists to recover.

Any failure halts before any order is created and writes the reason to the audit file under `logs/execute-*.json`.

## Ordering rules

- Sells are planned and submitted before buys.
- Buys are sized on cash already on hand (minus the configured cash buffer) — never on unfilled sell proceeds.
- The monthly target is persisted on the first attempt; a later safe recovery run (zero open orders, all prior intents terminal) completes the rebalance once sells have settled.

## Restart / timeout recovery

- Every non-terminal intent is reconciled by client order ID (`GET /v2/orders:by_client_order_id`) at the start of an execute run; the fill delta is folded into the ledger idempotently.
- A submit that fails ambiguously (timeout, dropped connection): the broker is looked up by client order ID BEFORE anything else. If found, the intent is marked submitted and reconciled — no duplicate. If not found, the intent stays `unknown` and the run halts; `unknown` requires manual review and always blocks further orders.
- Orphaned `pending` intents (recorded, then crashed before the POST) are safe to send because their ID was never transmitted — but only after the full guard chain passes: reconciliation stays read-only, older months' orphans are marked `abandoned`, and this month's are POSTed by the guarded `submit_pending_orphans` step at the end of the guard chain. The first attempted orphan POST always ends that execution attempt (whether filled, resolved after timeout, or ambiguous), so no second orphan or fresh plan uses stale broker state.
- Accepted-but-timed-out orders become live at the broker; the zero-open-orders guard then halts and later runs continue the rebalance. No path ever re-POSTs a client order ID that was already transmitted.

## Scheduler expectations

| Job | When (America/New_York) | Command | Notes |
|---|---|---|---|
| Plan | Each market day, 09:35 ET | `python -m bot.run` | Audit only; never submits. |
| Execute | First official market day of month, 09:35 ET | `python -m bot.run --execute` | Guard chain runs; halts write `logs/execute-*.json` and a non-zero exit. |
| Recovery | Same time while an open run exists | `python -m bot.run --execute` | Safe on later days only because the open monthly run exists. |
| Reconcile | After close | `python -m bot.reconcile` | Read-only report; alerts are advisory (state sync happens in the execute path). |
| Backtest | On demand | `python -m bot.backtest` | No orders, ever. |

The scheduler must capture exit status and alert on non-zero exits. Do not retry an execute run automatically after an ambiguous network failure — the run itself already halts and persists the ambiguity for the next safe invocation.

## Known paper-simulation limitations

- Paper fills are Alpaca-simulated; `filled_avg_price` collapses partial fills into one average, so per-fill slippage is approximated.
- The immediate `apply_broker_order` after a POST only sees the snapshot the POST returned; full fills are picked up on the next reconcile/execute run.
- Buy budgets use the account's broker-reported cash; paper cash does not include real-world settlement timing.
- Modeled costs (backtest slippage, reconciliation slippage) are MODELED, never actual fees.
- State corruption/loss is unrecoverable by design: a missing ledger with broker holdings halts (untracked positions) rather than trading around them. Back up `state/` like any database.
