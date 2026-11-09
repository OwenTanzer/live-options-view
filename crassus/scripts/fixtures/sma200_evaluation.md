# SMA(200) replay evaluation — SPY, 2006-11-07 to 2026-11-06

Fixed-rule research replay; **not optimized** and not a live result.

## Convention

- Signal: adjusted daily closes, 200-day SMA including the signal day.
- Execution: next session’s adjusted open; no fill at the signal close.
- Sizing: 2× entry equity for the strategy; 1× starting capital for buy-and-hold.
- Costs: 5 bp slippage + 1 bp commission per side.
- Financing: 6% annual on negative cash, accrued daily.
- In-sample: 2006-11-07 through 2019-12-31. Out-of-sample: 2020-01-01 through 2026-11-06.
- Data: Yahoo Finance SPY daily chart snapshot stored in
  `spy_daily_20y.json` (5,031 rows); adjusted close convention.
- 105 fills; total financing paid **$12,993.88**. Turnover is cumulative
  notional divided by starting equity, not annualized.

## Results

| Period | Strategy annualized | Strategy Sharpe | Strategy max drawdown | Strategy turnover | Buy-and-hold annualized | B&H Sharpe | B&H max drawdown | B&H turnover |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Full 2006-11-07..2026-11-06 | 10.45% | 0.581 | -41.42% | 439.0× | 10.70% | 0.621 | -55.08% | 0.99× |
| In-sample ..2019-12-31 | 7.99% | 0.485 | -41.42% | 186.2× | 8.82% | 0.538 | -55.08% | 0.99× |
| Out-of-sample 2020-01-01.. | 15.07% | 0.734 | -34.04% | 90.5× | 13.97% | 0.769 | -32.79% | 0.97× |

Full-period final equity: strategy **$72,954.07** versus buy-and-hold
**$76,492.77**. Full-period volatility: 20.96% versus 19.45%. Full-period
turnover: 439.0× versus 0.99×.

## Reading

The rule did not beat buy-and-hold on full-period return or Sharpe in this
sample; it did reduce maximum drawdown substantially. Out-of-sample it
produced a slightly higher annualized return but slightly lower Sharpe and a
deeper drawdown than buy-and-hold. Those differences are small enough that
this is not evidence of a durable edge. The result is sensitive to the
financing assumption, the use of adjusted prices, execution at the next open,
and the chosen split; it is deliberately not tuned.

## Limits

Yahoo is a research data source, not an approved production feed. Adjusted
closes embed dividend/split adjustments; no separate tax, borrow-locale,
liquidity, market-impact or margin-call model is included. The paper backend
charges financing only at close of a leveraged round trip and does not model
daily variation margin. No deployed account or live-session acceptance is
claimed.

The 5 bp slippage and 1 bp commission in this replay are **not** the paper
fill convention. The live paper path fills at the next executable live
quote's bid/ask (the spread is the cost and no separate 5/1 bp fee is
deducted), and its financing is charged only when the leveraged position is
closed. The replay executes at the next session's adjusted open as a daily
proxy and accrues financing daily. These results are therefore a research
comparison, not a fill-level equivalence claim.
