# Internal product specifications

These specifications are repository documentation, not served assets. Return to the [documentation homes](../README.md) for operations and public references. Source paths mentioned in the specification bodies are relative to the repository root.

| Specification | Use it to understand | Boundary |
|---|---|---|
| [SPY collection](SPY_COLLECTION.md) | Parallel snapshot keys, symbol isolation, health and browser selection | No substitution of QQQ for missing SPY; current publication needs a live observation |
| [Qualified ask-to-later-bid](OA203_QUALIFIED_ASK_BID_V1.md) | Endpoint eligibility and retrospective rankings | Qualified quotes do not establish fills or capacity |
| [Active Big Banana lookup](OA203_ACTIVE_LOOKUP.md) | Cutoff-pinned daily-session products, bounds and November 8 activation policy | Earlier candidate proposals in its history are superseded where the policy says so |
| [Multi-session windows](OA203_MULTI_SESSION_WINDOWS.md) | Contract continuity and weekly/overnight definitions | Daily active lookup is not evidence of deployed multi-session windows |
| [Cross-market overview](CROSS_MARKET_OVERVIEW.md) | Current MCP strip consumer and broader proposed coverage | The proposed stock universe is not current subscriptions |
| [NASDAQ-100 snapshot](NASDAQ_SNAPSHOT.md) | Hourly NASDAQ-100 + NDX snapshot contract, provenance sidecar and read-only MCP tool | No R2 artifact is published yet; the panel reads the artifact only after a maintainer publishes it |
| [Adaptive quote panel](ADAPTIVE_QUOTE_PANEL.md) | Core/pins/movers behaviour, device-local pin scope, one refresh controller and release path | Real extended-session observation depends on the deployed #178 lane |
| [Extended price lane](EXTENDED_PRICE_LANE.md) | Continuous price-only collection, explicit session rules, rolling references and rollback | Fixtures do not establish sustained overnight/weekend provider availability |

The original root filenames remain small compatibility pointers; the full text has one maintained home here. Public plans stay under `site/plans/` with unchanged public paths, rather than being silently unpublished or duplicated into this directory.
