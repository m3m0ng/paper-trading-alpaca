# V1 research notes

## What the evidence supports

- **Momentum/trend is a testable signal, not a return guarantee.** Ken French's momentum factor forms portfolios monthly from returns over months 2–12. That supports a lagged 12-month momentum signal rather than trying to call a daily bottom. [Factor construction](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_mom_factor.html)
- **Diversification helps concentration risk but cannot prevent losses.** [SEC guidance](https://www.sec.gov/investor/pubs/assetallocation.htm)
- **Fees and trading costs reduce returns.** Backtests must include a pessimistic spread/slippage assumption even where broker commissions are zero. [Investor.gov](https://www.investor.gov/introduction-investing/getting-started/understanding-fees)
- **Momentum can crash.** A drawdown limit and exposure control are risk hypotheses to test, not proof of safety. [Momentum crashes](https://doi.org/10.1016/j.jfineco.2015.12.002)
- **Alpaca paper results are not live results.** Paper trading does not model market impact, latency slippage, queue position, price improvement, fees, or dividends. [Alpaca paper-trading guide](https://docs.alpaca.markets/us/docs/paper-trading)
- **Orders are asynchronous.** Alpaca order states include partial fills, cancellations, expiry, and rejection; deterministic client order IDs and reconciliation are necessary. [Alpaca orders guide](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

## V1 strategy hypothesis

This project will test a low-turnover, long-only ETF portfolio rather than try to predict intraday bottoms:

| Sleeve | Base target | Invest when | Defensive holding when false |
|---|---:|---|---|
| US equities (`VTI`) | 54% | Close > 200-day SMA **and** 12–1 month return > 0 | `SGOV` |
| International equities (`VXUS`) | 36% | Close > 200-day SMA **and** 12–1 month return > 0 | `SGOV` |
| Reserve (`SGOV`) | 10% | Always — permanent reserve | — |

Risk assets are capped at 90%. A failed trend sleeve moves its full fixed weight into `SGOV`, and the trailing-volatility cap can further reduce equity exposure into `SGOV`. No S&P 500 overlap sleeve and no gold allocation is deployed in V1.

The strategy calculates signals from completed daily bars, rebalances monthly, has no leverage or shorts, and uses `SGOV` as the defensive sleeve. It is a hypothesis that must be backtested with multiple slippage assumptions and compared with buy-and-hold benchmarks.

The 10% annual figure is a measurement target only. It is never a promise or an input that makes the bot increase risk after losses.

## Bot patterns worth adopting

The generic flowchart is useful, with two changes:

1. An LLM is **not** a trade-authority in V1. Deterministic, backtestable rules produce the trade decision. An optional LLM can later summarize news into structured, logged research with a fixed budget.
2. A reconciliation/audit loop is as important as order submission. The broker is the source of truth for cash, positions, fills, and order status.

```text
completed market data
  -> deterministic strategy signals
  -> optional AI research (non-authoritative, budgeted, structured)
  -> target portfolio decision
  -> risk manager / order validator
  -> Alpaca paper API
  -> order + position reconciliation
  -> immutable audit log and net-P/L report
```

## Sources

- Chan, Jegadeesh, Lakonishok, *Momentum Strategies*: https://www.nber.org/papers/w5375
- Moskowitz, Ooi, Pedersen, *Time Series Momentum*: https://doi.org/10.1016/j.jfineco.2011.11.003
- Barroso & Santa-Clara, *Managing the Risk of Momentum*: https://doi.org/10.2139/ssrn.2041429
- FINRA algorithmic trading controls: https://www.finra.org/rules-guidance/key-topics/algorithmic-trading
- Alpaca market-data constraints: https://docs.alpaca.markets/us/docs/about-market-data-api
