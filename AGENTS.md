# Hermes execution instructions

## Mission

Maintain a **paper-only**, fully automated long-only ETF bot. The current strategy is a V1 hypothesis, not investment advice or a promised 10% annual return.

Hermes may manage scheduling and may use GPT Terra for operational work. GPT Terra must not become an unlogged, free-form trade authority.

## Non-negotiable safety rules

1. Never change `APCA_API_BASE_URL` away from `https://paper-api.alpaca.markets`.
2. Never read, print, commit, or transmit `.env` values.
3. Do not enable `BOT_TRADING_ENABLED=true` until the strategy backtest and paper dry-runs pass the acceptance checks below.
4. No margin, leverage, short sales, options, crypto, or symbols outside `VTI`, `VXUS`, and `SGOV` in V1.
5. Do not add risk after losses, override a loss limit, retry a timed-out order blindly, or create a duplicate order.
6. Alpaca's broker state is authoritative. Halt new orders on unreconciled cash, position, or open-order mismatches.

## Commands

```bash
python -m unittest discover -s tests -v
python -m bot.run                 # plan only; writes an audit log, submits nothing
python -m bot.run --execute       # guarded paper execution (see guard chain below)
python -m bot.backtest            # cost-aware V1 backtest; fetches adjusted daily bars; writes reports/backtest-*.json; submits nothing
python -m bot.reconcile           # read-only EOD reconciliation; writes logs/reconcile-*.json; submits nothing
```

`--execute` runs the guarded paper path. It is refused (before any network call for the config guards, before any POST for the rest) unless ALL of the following hold: market open per `/v2/clock`, first official market day of the month OR an open monthly run to recover, Alpaca account unblocked, zero open orders, no `unknown` durable intents, long-only positions restricted to `VTI`/`VXUS`/`SGOV` exactly matching the durable ownership ledger, owned value within `BOT_MAX_MANAGED_EQUITY` (explicit configuration; agreed initial value 5000, never hard-coded), and bootstrap additionally requires an empty broker with cash covering the full cap. Any failure halts with an audit log and a non-zero exit.

## Managed-ownership boundary

The agreed initial V1 allocation is a $5,000 managed paper portfolio (`BOT_MAX_MANAGED_EQUITY=5000` in `.env`). The cap is explicit configuration, never hard-coded in source. V1 base allocation: VTI 54%, VXUS 36%, SGOV 10% permanent reserve; failed trend sleeves move to SGOV and the volatility cap may reduce equities further into SGOV. No S&P 500 overlap or gold in deployable V1.

Before any future order submission, the bot holds durable tracked ownership of what it trades (implemented in `bot/state.py`, SQLite at `state/bot-state.sqlite3`, git-ignored):

- Every managed position is traceable to this bot's own persistent logged orders: an ownership ledger built from fill deltas of recorded order intents, persisted BEFORE the POST with deterministic, unique client order IDs.
- Pre-existing, untracked, or otherwise unmatched positions **cannot be traded**; detecting one halts the run instead of selling or buying around it.
- Unreconciled cash, position, or open-order mismatches likewise halt new orders.
- An ambiguous submit timeout stays `pending`/`unknown` and the broker is looked up by client order ID before any attempt to create an order; an unresolved `unknown` intent always blocks further orders.
- Sells are submitted before buys; buys use cash already on hand, never unfilled sell proceeds. The monthly target is persisted so later safe recovery runs complete a rebalance after prior sells fill. See `docs/execution-and-recovery.md`.

## Scheduled operations

Use a timezone-aware scheduler in `America/New_York`; do not use a local weekday-only assumption. Every scheduled command must still respect Alpaca's `/v2/clock` result.

| Job | Suggested time | Current action | Required outcome |
|---|---|---|---|
| Signal/plan | Each market day, 09:35 ET | `python -m bot.run` | Create an audit log; submit nothing. |
| Backtest evidence | On demand | `python -m bot.backtest` | JSON report under `reports/`: CAGR, max drawdown, volatility, worst calendar year, turnover, modeled costs, time in SGOV for buy-and-hold vs monthly trend vs trend+vol-cap, at 0/5/10/25 bps one-way slippage. No look-ahead: signals use bars completed before the trade date. Costs are MODELED, never live fees. Buys scale to available cash after modeled costs (never a negative modeled cash balance). Submits nothing. |
| Monthly paper rebalance | First official market day, 09:35 ET | `python -m bot.run --execute` | Guarded paper execution: all guards pass first, intents persisted before each POST, sells before buys, buys sized on existing cash. Halts write `logs/execute-*.json` with a non-zero exit. |
| Recovery | While an open monthly run exists | `python -m bot.run --execute` | Same guard chain; completes the rebalance after prior sells settle. Never re-POSTs a transmitted client order ID. |
| Reconciliation | After close | `python -m bot.reconcile` | Implemented (read-only report): records account, positions, open/closed orders, broker-reported PAPER daily P/L, and MODELED fill slippage to `logs/reconcile-*.json`; flags cash/position mismatches, non-long positions, and untracked symbols. Submits nothing. The execute path runs its own durable-state sync by client order ID. |

The scheduler must capture command exit status and alert on non-zero exit. Do not retry an order-creation run automatically after an ambiguous network failure.

## GPT Terra boundary

Use GPT Terra only for bounded, structured work such as news summaries, log explanations, and implementation assistance. If added later:

- Give it no Alpaca credentials.
- Require JSON output with a schema and save the raw response plus model/cost metadata.
- Treat output as research only; deterministic strategy and risk code make final order decisions.
- Enforce `BOT_AI_MONTHLY_BUDGET_USD`; stop calls when the budget is exhausted.

## Required implementation sequence

1. ~~Add a cost-aware backtester for the exact V1 rules.~~ **Implemented** as `bot/backtest.py` + `python -m bot.backtest` (pure, unit-tested engine; buy-and-hold vs monthly trend-only vs trend-plus-volatility-cap; 0/5/10/25 bps one-way slippage scenarios; JSON reports under `reports/`).
2. ~~Add end-of-day broker reconciliation and net-P/L reporting.~~ **Implemented in report form** as `bot/reconcile.py` + `python -m bot.reconcile` (read-only JSON audit in `logs/` with broker PAPER P/L and MODELED fill costs). Scheduler wiring, alerting on non-zero exit, and persistent daily records remain open.
3. ~~Add persistent order/run state and recovery handling for partial fills, cancellation, expiry, rejection, and timeout.~~ **Implemented** as `bot/state.py` (SQLite intents, fill-delta ledger, ownership ledger) plus the guarded execution path in `bot/run.py`; covered by `tests/test_state.py` and `tests/test_run.py` (mocked clients only — no network in tests).
4. Only then run repeated paper execution with a deliberately small `BOT_MAX_MANAGED_EQUITY` cap.

## Acceptance checks

Before enabling execution, all must pass:

- Unit tests pass.
- The backtest reports CAGR, max drawdown, volatility, turnover, modeled costs, and benchmark comparisons.
- Signals use completed bars only; no look-ahead bias.
- A dry run produces an audit log without an order.
- The order path is idempotent across a simulated timeout/restart. **Covered by tests:** an accepted-but-timed-out submit, followed by restart/reconcile, never creates a duplicate and records the broker order.
- Paper runs confirm the bot stays long-only and within configured cash/exposure limits. **Guard tests cover:** disabled/blocked/closed-market refusals before any network call, untracked or pre-existing holdings halting with zero submits, cap enforcement, submits only after all guards, and no duplication on restart or partial fill.
