# SMA(200) local evidence

- `sma200-card-desktop.png` and `sma200-card-narrow.png` — the shipped
  `site/js/bots-panel.js` rendered against a local fixture. The fixture shows
  the dedicated `SPY 200D` card with the bullish signal date, adjusted close,
  SMA, prior values, target/actual 2× position, negative cash representing the
  paper loan, 40 SPY shares at a 1× multiplier, and the strategy reason.
- `sma200-card.html` — the exact local page used for that render.
- `sma200_replay.json` — fill list, equity curve inputs and split metrics from
  the fixed replay.
- `sma200_fills.csv` — the replay's 105 auditable signal-dated fills with side,
  quantity, price, notional, cost and the signal bar that produced them.
- `sma200_evaluation.md` — the readable evaluation table and limits.
- `spy_daily_20y.json` — the fixed adjusted daily-bar snapshot; no network is
  needed to reproduce the replay.

These are local fixture/evidence artifacts. They do not prove a deployed
account, a production data entitlement or live execution.
