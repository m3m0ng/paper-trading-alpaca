# Alpaca paper growth-portfolio bot

A paper-only, fully automated portfolio-bot foundation. V1 tests a low-turnover ETF trend strategy with a volatility cap; it does **not** predict bottoms, guarantee returns, or use an LLM as an order-decider.

## Architecture

```text
completed daily market data
  -> deterministic trend + volatility signals
  -> target ETF weights
  -> risk/order validation
  -> Alpaca paper API
  -> broker reconciliation + audit logs
```

The generic `AI -> BUY -> Alpaca` design is incomplete on its own. The risk manager and reconciliation loop are mandatory. An optional GPT Terra module may later summarize research, but it must be budgeted, structured, logged, and non-authoritative.

See [`research/v1-research.md`](research/v1-research.md) for the evidence, sources, and V1 hypothesis. See [`AGENTS.md`](AGENTS.md) for the Hermes handoff contract.

## V1 strategy hypothesis

Base allocation (agreed after independent review): at most 90% risk assets plus a permanent 10% `SGOV` reserve.

| Sleeve | Base target | Hold when | Otherwise |
|---|---:|---|---|
| `VTI` | 54% | close > 200-day SMA and 12–1 month return > 0 | its 54% sleeve moves to `SGOV` |
| `VXUS` | 36% | close > 200-day SMA and 12–1 month return > 0 | its 36% sleeve moves to `SGOV` |
| `SGOV` | 10% | always (permanent reserve) | never deployed to equities |

The resulting risky allocation (max 90%) is capped by trailing 63-trading-day realized volatility. With a `BOT_TARGET_ANNUAL_VOLATILITY=0.10`, the bot only reduces equity exposure when realized volatility exceeds 10%; the reduced weight lands in `SGOV`. It never uses leverage to increase exposure when volatility is low.

Trend controls and volatility controls manage risk. They may reduce drawdowns but can lag sharp recoveries, suffer whipsaw losses, and cannot guarantee a 10% annual return.

## Setup

1. Copy the non-secret settings from [`.env.example`](.env.example) into your existing `.env`.
2. Set `BOT_MAX_MANAGED_EQUITY`. The agreed initial V1 configuration is `5000` (a $5,000 managed paper allocation); it is explicit configuration, never hard-coded. Leave `BOT_TRADING_ENABLED=false`.
3. Run the strategy tests:

   ```bash
   python -m unittest discover -s tests -v
   ```

4. Generate a paper-only plan during market hours:

   ```bash
   python -m bot.run
   ```

The plan fetches Alpaca paper account/data state and writes an audit file under `logs/`. It never sends an order.

## Backtest evidence (`python -m bot.backtest`)

```bash
python -m bot.backtest                      # 10 years, slippage 0/5/10/25 bps
python -m bot.backtest --years 15 --initial-equity 10000
```

This is the V1 evidence layer (AGENTS.md implementation step 1). It fetches adjusted daily bars (`adjustment=all`, IEX feed) **only when you run it**, then compares three variants of the exact `bot/strategy.py` rules:

| Variant | Behavior |
|---|---|
| `buy_and_hold` | One initial buy at 54/36/10, never rebalanced |
| `monthly_trend` | Initial buy, then rebalance on the first trading day of each month using the trend rules |
| `monthly_trend_vol_cap` | Same, plus the 63-day realized-volatility cap |

Guarantees and limits:

- **No look-ahead:** every rebalance uses closes dated strictly before the trade date; the trade fills at that day's close. Each trade in the report records its `signal_through_date` to prove it.
- **Modeled costs only:** slippage is a configurable one-way fee in bps (default scenarios 0, 5, 10, 25). These are assumptions, **not** actual Alpaca or live-market fees, and the report says so. Buy fills are scaled to available cash after modeled costs, so slippage can never model a negative cash balance (see `min_cash_balance` in the metrics).
- **IEX data:** the bar feed is a subset of full-market consolidated data.
- Metrics per run: end equity, CAGR, annualized volatility, max drawdown, worst calendar year (plus every year), annualized turnover ratio, modeled slippage cost, time in SGOV (share of days with SGOV weight above the 10% base, including passive drift), mean SGOV weight, trade count.
- Reports are written to `reports/backtest-<timestamp>.json` (git-ignored).
- Submits no order.

## End-of-day reconciliation (`python -m bot.reconcile`)

```bash
python -m bot.reconcile
```

Read-only reporting (AGENTS.md implementation step 2, now implemented in report form). It fetches the paper account, positions, open orders, today's closed orders, and the broker P/L history, and writes `logs/reconcile-<timestamp>.json` (git-ignored). It contains:

- **Broker-reported P/L**, explicitly labeled: *Alpaca PAPER account, simulated fills; excludes real fees, slippage, market impact, and taxes.* Includes both the portfolio-history daily P/L and an `equity - last_equity` cross-check.
- **MODELED fill slippage/costs**, explicitly labeled as *not actual Alpaca or live-market fees*: per filled order, `|fill price - same-day IEX close| x filled qty`, positive = unfavorable. `filled_avg_price` collapses partial fills, so this is an approximation.
- **Consistency checks:** cash + position value vs equity, long-only verification, untracked symbols outside `VTI`/`VXUS`/`SGOV`, open orders, trading-blocked flags. Any inconsistency must halt future execution; this report surfaces it.
- Submits no order — there is no order-submission code path in `bot/reconcile.py`.

## Execution gate (`python -m bot.run --execute`)

Execution is now implemented behind a hard guard chain, with durable state in `state/bot-state.sqlite3` (git-ignored; see [`docs/execution-and-recovery.md`](docs/execution-and-recovery.md)):

- **Durable intents:** every order intent is written to SQLite BEFORE the POST, keyed by a deterministic client order ID (`UNIQUE` constrained). A restart reconciles against the broker by client order ID, so an accepted-but-timed-out order can never be duplicated; an ambiguous timeout whose broker lookup fails stays `unknown` and halts until manual review.
- **Fill-delta ledger + tracked ownership:** reconciliation is idempotent, and the ownership ledger is built ONLY from this bot's own fills. Broker positions must exactly match the ledger; a pre-existing or untracked holding halts instead of being traded.
- **Guard chain (before any POST):** `BOT_TRADING_ENABLED=true`, `BOT_MAX_MANAGED_EQUITY > 0` (agreed initial configuration 5000, in `.env`, never hard-coded), open market clock, unblocked account, no `unknown` intents, zero open orders, long-only `VTI`/`VXUS`/`SGOV` positions matching the ledger, owned value within the cap, and either the first official market day of the month or an open monthly run to recover. Bootstrap additionally requires an empty broker with cash covering the full cap.
- **Ordering:** sells first; buys sized on cash already on hand — never on unfilled sell proceeds. The monthly target is persisted so a later safe recovery run completes the rebalance once sells settle.

Plain `python -m bot.run` stays a plan-only run: it writes an audit file and submits nothing. Any guard failure or ambiguous submit writes `logs/execute-*.json`, halts with a non-zero exit, and submits nothing further.

## Deliberate V1 limits

- Paper trading only.
- Long-only ETFs: `VTI`, `VXUS`, `SGOV`.
- No margin, shorting, options, crypto, or intraday prediction.
- No live broker execution. The paper URL (`https://paper-api.alpaca.markets`) is enforced in `bot/config.py`; there is no live fallback.
- Backtest and reconciliation reports use modeled costs and IEX data; neither claims to reproduce live fees, fill quality, or consolidated-market prices.
- Execution is paper-only and still simulation-bound: fills are broker-simulated, per-fill slippage is approximated by `filled_avg_price`, and immediate post-POST fill snapshots are only completed on the next reconcile/execute run. See [`docs/execution-and-recovery.md`](docs/execution-and-recovery.md).
- State loss is unrecoverable by design: a missing ownership ledger with broker holdings halts execution rather than trading around it. Back up `state/` like any database.
- No LLM market calls yet.
- No claim that paper P/L includes live fees, fill quality, or slippage.
