# Strategy Archive

Open **Strategy Archive** from Options View. Sign in on the home page using a member's paper account. The shared archive uses account identity to credit submitters. Keep the original author in the record even when another agent submits it.

## Qualitative rule index

Each family contains rules, and each rule can have multiple parameterized versions. On a version, `data.rule` has five independently filterable text fields.

| Field | Meaning |
| --- | --- |
| `edgeHypothesis` | Why the opportunity is expected to exist. |
| `entryCondition` | The full setup and trigger, including sequence or conjunction. |
| `riskReward` | What the position gains or loses as the market moves. |
| `monitoringCadence` | When the algorithm reassesses its conditions, such as each completed bar or on an event. |
| `exitCondition` | What ends the position. |

The browser groups versions with matching family and complete qualitative descriptions (case-insensitive). Incomplete descriptions stay individually accessible. Missing fields or null mean unknown. A 150-session fork of the 200-session rule inherits these five fields; changing to a short position after the bearish cross changes the qualitative rule. Filters also apply to exports. The API accepts the five field names as combinable exact-text query parameters (case-insensitive); an empty value requests undocumented entries.

## Intake for agents

Send a JSON object or an ordered array of up to 40 objects (192 KiB total) to `POST /api/strategy-archive` with the normal session cookie and `Content-Type: application/json`. Each response item gives the canonical ID or the row error. Retry an interrupted batch with the same IDs and payloads. Put version definitions before records that refer to them. Conflicting records stay immutable. Inspect the existing item, then submit a separate observation describing the disagreement or fork with a new version ID. Matching configurations are flagged for review across roots and forks; roots sharing a source are also flagged. A fork must change its rule or configuration.

| kind | version link | data |
| --- | --- | --- |
| version | none | family, originator, source URL, stage (documented or proposed), parameters. Optional aliases, instrument, signal, horizon, rules and prerequisites. A fork records parent and reason. |
| observation | required | originator, source URL, asOf, summary. Cite a mirror here and reference the original evidence in the summary. |
| evidence | required | kind (paper, live, backtest or observation), scope (local or external), original source URL, resultRef (stable locator within it), period, sample, assumptions, summary. Cite the same underlying result with the original URL and locator. Reworded citations earn credit and return the canonical ID with `differs: true`. The first description is retained. Differences in kind, scope, period or sample require review. |
| handoff | required | design with Population, Trigger, Comparator, Outcome, Horizon and Evaluation plan. Saved before download and available to subsequent agents. |
| review | required | decision (adopt, try, modify, defer or reject), reason and next owner. The server records reviewer and time. |
| trial | required | trialId, owner, status (assigned, active, paused, completed or cancelled), asOf (YYYY-MM-DD) and source URL for the assignment or run. Append status changes using a fresh record ID and the same trialId. Future dates are rejected. Paused trials retain their owner. The latest effective date wins, then submission order. |

Example of an incomplete discovery:

```json
{"id":"example-term-skew","kind":"version","data":{"family":"Term skew","originator":"Research agent","source":"https://example.org/study","stage":"documented","parameters":{"threshold":{"type":"number","unit":"volatility points","state":"unknown"}}}}
```

Use numeric, integer, category or boolean values (types `number`, `integer`, `category`, `boolean`). Record units and conditional applicability in `when`. A parameter can have a documented `domain` with `min`/`max` or `choices`. A fork retains the source version so its overrides can be compared directly. Mark missing values `unknown` or `inapplicable`. Use consistent units for a family's named input. Bulk intake accepts optional source-specific fields and exports them intact.

Read `GET /api/strategy-archive`. Add `?version=spy-200` for one version and its linked records or `?family=Moving-average%20crossover` for a family. Continue with the returned `next` as `after` until it is null, preserving filters. Restart a scan to refresh credits on previously seen records. The browser’s filtered export groups records and submitters by version. The hypothesis export carries the exact configuration, trial links, evidence, and reviewer-supplied design fields for One Clock.

## Review and release

Use the disposable local preview described in [Site releases](SITE_RELEASES.md). Register the fixture-only username `archive_reviewer` in that preview, then open the archive. The production migration creates empty tables. The preview-only seed `spy-200` cites the dated [#97 paper runner receipt](https://github.com/OwenTanzer/live-options-view/issues/97). Zero trades at that checkpoint is an observation. A new fork starts with its own empty evidence history.

Release order: apply the additive `backend/migrations/0005_strategy_archive.sql` once to the API database, configure the API Worker variable `STRATEGY_ARCHIVE_MEMBERS` with the comma-separated existing usernames authorized for shared read/write access, release the API, verify a member read, then promote staged site assets. Empty membership denies access. For rollback, revert the site followed by the API and retain the additive tables and records. No collector activation or external hypothesis engine is required.
