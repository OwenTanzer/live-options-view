# Website and paper-account backend

[backend/index.js](index.js) is the stable Cloudflare entry point. It dispatches to
these modules in the separate `live-options-api` service;
[wrangler.api.toml](../wrangler.api.toml) defines operational bindings and cron.
The public asset gateway uses [wrangler.assets.toml](../wrangler.assets.toml)
through the [staged release procedure](../docs/SITE_RELEASES.md).
The browser's implementation lives separately in [site/](../site).

| Responsibility | Source |
|---|---|
| Allowed archive access and live-quote proxy | [browser-data.js](browser-data.js) |
| Accounts and sessions | [auth.js](auth.js) |
| Bot roster and membership reconciliation | [bots.js](bots.js) |
| Quote-backed execution and request records | [execution.js](execution.js), [paper-trade.js](paper-trade.js) |
| Expiration settlement and scheduled sweep | [settlement.js](settlement.js) |
| Shared helpers | [common.js](common.js) |

## Boundaries worth preserving

The live-quote route proxies the collector's in-memory feed. Archive reads use
stored R2 artifacts; the public site's archive-base selection is explained in
[Architecture](../ARCHITECTURE.md#browser-and-data-boundaries).

Paper orders are execution requests, not arbitrary client-supplied fills.
The execution path validates quote freshness and the requested side, and persists
canonical request records. Read the implementation and fixture tests before
changing retry or identity handling.

The configured bot index mode is `r2`; the migration is not waiting to start.
Membership and account state are different records: a retained closed account
must not silently disappear from the roster. The dated
[cutover record](../site/operations/2026-09-28-bot-index-cutover.md) preserves rollout
evidence. Do not replay its migration steps as routine startup instructions.

The settlement cron runs every fifteen minutes during hours 12–23 UTC on weekdays,
even with no browser open. Browser-triggered and scheduled settlement can obtain
spot marks differently. Source availability and closed-account policy are
separate questions; a successful sweep is not proof that every historical mark
exists. See the [system map](../ARCHITECTURE.md#following-one-value).

[Web CI](../.github/workflows/web-ci.yml) and
[consumer CI](../.github/workflows/market-mcp-ci.yml) list offline checks.
Old design and migration instructions are linked from
[documentation history](../docs/HISTORY.md), not treated as current deployment instructions.

## Trade journal reader (OA-238)

[`trade-journal.js`](trade-journal.js) is a **read-only** mirror of the Trade
Retrospectives journal in Notion (data source
`2a32c187-f187-4570-9d53-05e611943df0`) for the private trade display at
`/trade-journal.html`. It reads the full data source with pagination, then each
page body (also paginated), because the fills exist only in the body. It
normalises fills into the shared execution model, refreshes changed rows in
place with a revision history, and flags rows that leave the source (`archived`,
`removed`, `removed_from_source`, `missing_unverified`) without hiding them. It
also records the last successful sync and the last read error. A row in an
unknown body format is stored and shown as unsupported, never guessed. A row is
only marked as having left the source after a complete scan.

| Route | Auth | Effect |
|---|---|---|
| `GET /api/trade-journal?date=YYYY-MM-DD` | session + viewer | Positions with an execution on that America/New_York date, unsupported rows dated that day, totals, sync status |
| `GET /api/trade-journal/row?page_id=` | session + viewer | One stored row, with `source_last_edited` (Notion) and `app_read_at` |
| `POST /api/trade-journal/sync` | session + viewer + origin | Reads Notion and updates D1. Writes are paused by the cutover fence like the other mutating routes |

Each sync makes at most 20 query pages, 35 body requests (+10 so the last body
can finish its pages) and 5 removal checks. Rows left over are reported as
`deferred` and finished by the next sync. Notion reports `last_edited_time` to
the minute, so a row is re-read until one read lands a full minute past its
edit minute.

Settings this needs before it can serve (names only; values are operator-managed):

| Setting | Kind | Value |
|---|---|---|
| `NOTION_TRADE_JOURNAL_TOKEN` | Worker secret on `live-options-api` | Token of a Notion internal integration with only the **Read content** capability, connected only to the Trade Retrospectives database |
| `TRADE_JOURNAL_VIEWERS` | `[vars]` on `live-options-api` | Comma-separated usernames allowed to view. Unset means nobody |
| `backend/migrations/0003_trade_journal.sql` | D1 migration on `live-options` | Additive: `trade_journal_rows` and `trade_journal_sync` |

The token is sent only in the `Authorization` header to `api.notion.com`. It is
never logged, stored or returned. Receipt link destinations in page bodies are
dropped before parsing. Tests: `node tests/trade_journal.test.mjs`.
