# Crassus / Golden Goose

A thin Python bot runtime and client library for automated strategies against
the [Options View](https://options.moopertonic.net) paper-trading layer.
Implements `crassus_golden_goose_guidance.v1` (Linear **MOO-24**).

Both names refer to the same system and are interchangeable.

> Build the smallest organism that can repeatedly touch reality and leave an
> intelligible fossil record.

The loop is: **observe → decide → validate → submit → reconcile → record → repeat.**

Fake-capital loss, absurd positions and account liquidation are all permitted
experimental outcomes. The one unacceptable failure is losing the causal
record of what the bot saw, decided, and did.

---

## Status

| Phase | | |
|---|---|---|
| **P0** deployment smoke test | 🔴 **Blocked** | Account endpoints are absent from the deployed Worker — see below |
| **P1** single-account closed loop | 🟢 Implemented, verified against a mock | 66/66 invariant checks pass |
| **P2** multi-account supervisor | 🟡 Runtime supports it; unexercised against a real server | |
| **P3** strategy ecology | ⚪ Not started | Only the smoke strategy exists |
| **P4** evaluation & hardening | ⚪ Not started | |

### P0 blocker — the deployed Worker predates the auth commit

The code is on `master`; it does not appear to be what is serving
`options.moopertonic.net`.

| Probe | Deployed | `master` (`2d8476d`) |
|---|---|---|
| `POST /api/register`, `/login`, `/logout`, `GET /api/me`, `POST /api/settle` | **404** (falls through to `env.ASSETS`) | routes exist |
| `GET /api/live-quotes` | 200, and 400 on missing `symbols` | matches |
| `POST /api/paper-trade`, no session | **403** `Forbidden` (`text/plain`, 9 bytes) | **401** — `requireSession` runs *before* `checkOrigin` |
| `POST /api/paper-trade`, allowlisted `Origin` | **403** | passes — `checkOrigin` only rejects a *present, non-allowlisted* origin |

The deployed `paper-trade` rejects an allowlisted origin and never reaches a
session check, so it cannot be running `master`'s handler. Together with the
404s this points at a build from before `9e6eac5` ("Add login-based paper
trading balances", merged as PR #15 / `2d8476d`).

**The fix looks like a redeploy of `master`, plus confirming the `USERS` and
`SESSIONS` KV namespaces are bound** — not a code change. P0's exit criterion
(one one-contract fill visible in `/api/me`) is unreachable until then.

The same P0 script passes 7/7 against `scripts/mock_worker.py`, which
implements the documented contract — so the failure is the deployment, not the
test.

Note this also means **`POST /api/paper-trade` currently rejects every client**,
browser included — worth checking whether paper trading works in the UI today.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# P0 -- test the real deployment
.venv/bin/python scripts/p0_smoke.py

# Prove the integrity invariants (spins up the mock Worker itself)
.venv/bin/python scripts/verify_invariants.py

# Run the loop. accounts.example.json is the tracked bot catalog; export the
# CRASSUS_PW_* variables it names. An ignored accounts.json can override fields
# by username or add private accounts, but cannot hide catalog bots.
.venv/bin/python -m crassus.runner --once --dry-run
.venv/bin/python -m crassus.runner --interval 300
```

Kill switch: `Ctrl-C`, `SIGTERM`, or `touch state/STOP`.

---

## Layout

| | |
|---|---|
| `crassus/clock.py` | ET-aware time and market phase |
| `crassus/config.py` | Accounts and credentials (never from source control) |
| `crassus/audit.py` | The decision ledger — append-only JSONL |
| `crassus/market.py` | R2 snapshot reader + on-demand live quotes |
| `crassus/client.py` | Sessions, execution, reconciliation — **owns every integrity invariant** |
| `crassus/strategy.py` | The strategy contract |
| `crassus/sentiment.py` | Reddit sentiment observation for `reddit_sentiment_qqq` (public JSON listing scrape + browser fallback + VADER, polled and cached like the market snapshot) |
| `crassus/trump_sentiment.py` | Trump Truth Social sentiment observation for `trump_whisperer_qqq` (`trumpstruth.org/feed` RSS + VADER, polled and cached the same way) |
| `crassus/strategies/` | Strategy implementations |
| `crassus/runner.py` | The loop |
| `Dockerfile` | Railway's deploy image (`railway.toml`'s `builder = "DOCKERFILE"`) -- bakes a version-matched Chromium in for `reddit_sentiment_qqq`'s browser fallback |
| `scripts/p0_smoke.py` | P0 deployment smoke test |
| `scripts/mock_worker.py` | Local stand-in for the Worker, with fault injection |
| `scripts/smoke_browser_launch.py` | Build-time check that Chromium actually launches in the image (see `Dockerfile`) |
| `scripts/verify_invariants.py` | Integrity checks across 15 scenarios, run hermetically against a local mock (`crassus/scripts/fixtures/snapshot.json` stands in for the R2 snapshot; no network access to production is required) |
| `scripts/verify_reddit_sentiment.py` | Hermetic checks for `reddit_sentiment_qqq`'s aggregation math and decision logic -- no Reddit credentials or network access required |
| `scripts/verify_reddit_ingestion.py` | Hermetic checks for `reddit_sentiment_qqq`'s ingestion layer (JSON fetch, browser DOM parsing, layer selection, 429 cooldown, crash recovery) -- no network access or real browser required |
| `scripts/verify_trump_whisperer.py` | Hermetic checks for `trump_whisperer_qqq`'s aggregation math and decision logic -- no network access required |
| `scripts/verify_trump_ingestion.py` | Hermetic checks for `trump_whisperer_qqq`'s feed ingestion layer -- no network access required |

## Integrity invariants

Enforced in `client.py`, each verified by a scenario in `verify_invariants.py`:

1. **`execution_request_id` is written to disk and fsynced before the request is sent.** A timeout must not erase the identity needed to determine whether the trade happened.
2. **One outstanding mutation per account.** The Worker's KV writes are best-effort, not compare-and-swap.
3. **After a timeout or 5xx, `/api/me` is queried before any retry.** The trade may already be there.
4. **`balance_cash` is cash movement, never P&L.** Open exposure is absent from it entirely.
5. **`no_trade` and failures are logged, not just fills.** A ledger of only executions is survivorship-filtered.
6. **429 and `Retry-After` are honored globally.** The binding limit is per-IP, so all accounts share one budget.
7. **HTTP 410 is terminal and recorded as a margin call.** The Worker deletes the user outright. ⚠️ This invariant is verified against the *documented* contract (the mock), not the real deployed Worker's actual settlement behavior — see "Known gaps" below.

## Server constraints worth knowing

- **No `account_id`** — an account *is* a login. Twelve catalog bots = twelve registrations, twelve cookie jars.
- **The `reason` field is silently dropped.** Strategy reasoning lives only in the local ledger.
- **No close endpoint, no positions endpoint, no server P&L.** Closing is an opposite-side trade; positions are derived average-cost from the `/api/me` trade list.
- **Execution quotes must be ≤15s old.** Outside market hours the collector's quotes age out, so trades will 409 — the runner declines rather than spending rate-limit budget on a certain rejection.
- **Insolvent settlement deletes the account.** Not a negative balance — permanent erasure.
- Bot accounts **do not pre-exist**; the runner registers them on first use.
- **Bot accounts are marked server-side, not by username convention.** Registering with an `X-Bot-Registration-Key` header matching the Worker's `BOT_REGISTRATION_KEY` secret adds the account to the `bot:` index that backs the public `GET /api/bots` roster and the site's Automated tab. Without the header the account registers as an ordinary human user and never appears there; with a *wrong* key registration is rejected outright rather than silently downgraded.
- **`strategy_id` must be re-synced when it changes.** The merged catalog/config is authoritative, but the Worker only learns a bot's strategy when told. `POST /api/bot-metadata` (same operator key) updates `strategy_id`/`alias` on an existing bot, so moving an account to a new strategy re-attributes its future performance instead of leaving the roster crediting the old one. The runner does this at startup for every configured account.

### Adding a Crassus bot or strategy

Register the strategy in `crassus/strategies`, add at least one public account definition to `accounts.example.json`, and provision the listed password environment variable plus `BOT_REGISTRATION_KEY` in the runtime. CI fails if a registered strategy has no catalog account. No Worker or front-end edit is needed: the runner registers or re-syncs the account, `GET /api/bots` returns it, and the Automated tab renders its card generically.

The UI has no bot or strategy whitelist, and the ignored `accounts.json` is only an overlay. That means a stale private config cannot hide bots added to the checked-in catalog by later PRs.

## Account ↔ strategy mapping (target, once P3 lands)

| Account | Strategy | |
|---|---|---|
| Ankit | `aggressive_calls_reload` | P3 |
| Bob | `patient_calls_dip` | P3 |
| Doktor Freuding | `aggressive_puts_reload` | P3 |
| Luigi | `patient_puts_dip` | P3 |
| Jesus | `cheap_atm_calls` | P3 |
| Doris | `cheap_atm_puts` | P3 |

The six accounts above are the eventual P3 mapping's -- see below for what
each runs today instead. Six additional accounts are dedicated to strategies
outside that mapping: **TrumpWhisperer** runs `trump_whisperer_qqq`, **Newton**
runs `momentum_qqq`, and **Max Pain**, **OI Skew**, and **Put-Call Ratio** run
`max_pain_qqq`, `oi_skew_qqq`, and `put_call_ratio_qqq`, respectively. **Canopus** runs the frozen `canopus_down_day_14` forward-test rule.

Eight strategies are implemented today -- `smoke_atm_roundtrip`,
`reddit_sentiment_qqq`, `trump_whisperer_qqq`, `momentum_qqq`, `max_pain_qqq`,
`oi_skew_qqq`, `put_call_ratio_qqq`, and `canopus_down_day_14` -- so `accounts.example.json` splits
the original six accounts three/three between `smoke_atm_roundtrip` (Ankit,
Bob, Doktor Freuding) and `reddit_sentiment_qqq` (Luigi, Jesus, Doris), then
assigns one dedicated account to each of the other six strategies. The three
`reddit_sentiment_qqq` accounts use conservative / default / aggressive
thresholds via `params`, so the Automated tab's per-strategy rollup compares
something real out of the box. Copying the file as-is is meant to work, not
just illustrate the eventual mapping. Swap in a P3 strategy_id for an account
only once that strategy is registered; the runner validates every configured
`strategy_id` against the registry at startup and refuses to start (rather than
crashing mid-run on the first unregistered one it happens to reach) if one
doesn't exist yet.

Strategy-level rules — max 3 positions, 2:50pm flatten, the 4-of-5 green-day
rule, daily loss limits — are **configuration, not platform invariants**, and
are deliberately not baked into the runtime.

## `reddit_sentiment_qqq`

A second strategy, outside the six-strategy P3 mapping above: buys one ATM
QQQ call when aggregate Reddit sentiment is bullish, one ATM put when it's
bearish, and closes whichever side it's holding the moment sentiment stops
supporting it -- including when sentiment merely goes neutral, not only when
it flips outright -- otherwise it declines. At most one contract at a time --
sizing, hysteresis and cool-downs are `ctx.params` / future-strategy
concerns, not platform invariants, same as everything else in this section.

"What's currently held" is read from `ctx.book.positions` directly, not
re-derived by asking "what's the ATM strike right now" and assuming that's
what was bought -- the underlying can drift enough between cycles that the
strike bought last cycle is no longer the ATM one, and a naive re-derivation
would both open a second position and be unable to find the first one to
close.

Its signal comes from `crassus/sentiment.py`, which scrapes
r/wallstreetbets, r/stocks, r/options and r/investing's public `new.json`
listings for QQQ-relevant posts and scores each one with VADER. This reuses
the ingestion-and-scoring shape of
[nama1arpit/reddit-streaming-pipeline](https://github.com/nama1arpit/reddit-streaming-pipeline)
(posts in, VADER `compound` score, an averaged aggregate as the signal) but
not that project's Kafka/Spark/Cassandra/Kubernetes stack -- crassus is one
lightweight process per account reading one number every few minutes, and
that pipeline's own README documents its Reddit API access as broken since
Reddit's 2023 pricing change, so its infrastructure could not be reused
here as-is even if it were the right shape. For the same reason, this reads
Reddit's public per-subreddit JSON listing directly (no OAuth, no app
registration) rather than PRAW -- lower rate limit and no uptime guarantee,
but nothing to provision or rotate either.

To enable it for an account, set `strategy_id` to `reddit_sentiment_qqq` in
`accounts.json`. No credentials are required; optionally set a distinctive
User-Agent as a courtesy to Reddit (a generic default is used otherwise):

```bash
export REDDIT_USER_AGENT="crassus-reddit-sentiment/1.0 by u/yourname"
```

Ingestion has two layers, tried per subreddit in order (see
`crassus/sentiment.py`): a plain scrape of the public `new.json` listing,
and -- only when that fails -- a real headless Chromium tab (Playwright)
that loads the rendered `/new/` page instead. The second layer exists
because Reddit has been observed serving a same-origin JS proof-of-work
page ("Please wait for verification") in place of the JSON body, which no
plain HTTP client can solve; a real browser engine executes that challenge
itself, the same way it would for a human visitor. Verified in practice: on
a network where the JSON layer returns HTTP 403 for every request, the
browser fallback still gets through to real `<shreddit-post>` content, but
only once the headless launch also masks `navigator.webdriver` and sets a
realistic UA/viewport/locale (`_default_browser_factory`) -- a stock
Playwright headless launch hits the same challenge page indefinitely, since
the challenge appears to key off exactly that automation fingerprint rather
than anything in the request headers.

The browser fallback needs the Chromium binary and its Linux shared
libraries, neither of which `pip install -r requirements.txt` provides on
its own -- the `playwright` package is just a driver/CLI. Locally:

```bash
.venv/bin/python -m playwright install --with-deps chromium
```

Deployed, this runs from `crassus/Dockerfile` (Railway's `railway.toml`
sets `builder = "DOCKERFILE"`), built from the official
`mcr.microsoft.com/playwright/python` image rather than a Railpack
`buildCommand` -- a build command running `playwright install` is not
evidence the browser cache survives into the deploy image Railpack
actually ships, since it separates build-layer contents (apt packages,
`~/.cache/ms-playwright`) from the final image. The official image bakes a
version-matched Chromium directly into the runtime image instead, sidestepping
that split. The image tag and `requirements.txt`'s `playwright` pin must be
bumped together (exact-pinned, not `>=`) -- a mismatch leaves the pip
package importable but unable to find a working Chromium. The Dockerfile
build-tests this itself (`RUN python scripts/smoke_browser_launch.py`,
also run as its own CI job in `crassus-ci.yml`), so that specific mismatch
fails the build, not a live deploy days later. Without a working browser,
the fallback raises `RedditFetchError` (declines, doesn't crash) rather
than failing to import -- the JSON layer alone still works wherever Reddit
isn't blocking it.

A 429 from the JSON layer is treated separately from a JS-challenge/HTTP
failure: it means Reddit itself is asking for backoff, so
`RedditSentimentReader` starts a cooldown (from the response's
`Retry-After` header when present, else a conservative default) shared
across every subreddit and every future cycle until it expires, and
declines outright rather than falling through to the browser -- retrying
the same rate limit through a different transport would only compound it,
unlike the JS-challenge case the fallback exists for. The browser fallback
also recovers if the shared Chromium process itself crashes or disconnects
mid-run (checked via `browser.is_connected()`): the dead context is torn
down and relaunched exactly once, rather than staying cached as a
permanently-failing fallback until the whole bot process restarts. Every
Playwright call in `_fetch_listing_browser` -- including opening the page
and the cleanup `finally` block, not just navigation -- is wrapped so a raw
Playwright exception can never bypass `_fetch_listing`'s
`except RedditFetchError` and skip that recovery; cleanup failures are
swallowed rather than allowed to override whatever real error triggered
them.

The strategy declines every cycle (`no_trade`, reason cites the fetch
error) rather than raising if a scrape fails -- consistent with the
runtime's rule that a strategy proposes and never crashes the loop for a
condition it can anticipate. It also declines outside regular market
hours, before spending a request on a decision that would be `no_trade`
regardless, and below a minimum sample size (`min_sample_size`, default 5)
rather than trading on a couple of off-topic comments. `bullish_threshold`
/ `bearish_threshold`
(`mean_compound`, default ±0.15) are configurable per account via an
optional `"params"` object in `accounts.json`, which the runner passes
straight through to `StrategyContext.params`:

```json
{
  "alias": "Jesus",
  "username": "crassus_jesus",
  "password_env": "CRASSUS_PW_JESUS",
  "strategy_id": "reddit_sentiment_qqq",
  "params": { "min_sample_size": 8, "bullish_threshold": 0.2, "bearish_threshold": -0.2 }
}
```

`"params"` is optional and defaults to `{}` for every account, including
existing `smoke_atm_roundtrip` entries -- `accounts.example.json` is
unchanged.

Verify the decision logic and aggregation math hermetically (no network
access):

```bash
.venv/bin/python scripts/verify_reddit_sentiment.py
```

And the ingestion layer itself -- JSON fetch/pagination, browser DOM
parsing, layer-selection, the 429 cooldown, and browser crash recovery --
against fake `requests`/Playwright-shaped objects, also with no network
access or real browser process:

```bash
.venv/bin/python scripts/verify_reddit_ingestion.py
```

And, non-hermetically, that the deployed image itself can launch Chromium
(needs Docker):

```bash
docker build -t crassus-smoke .  # fails if Chromium can't launch inside it
```

**Known gaps**, in the same spirit as the section below: it only reads
submissions, not comments, so it can undercount chatter relative to the
reference pipeline, which scores comments. `_reader` in
`crassus/strategies/reddit_sentiment.py` is a module-level singleton, so
every account configured with this strategy in one runner process already
shares one `min_interval_s` cache and one 429 cooldown -- but there is
still no cross-*process* coordination if this ever ran outside a single
runner, and the `min_interval_s` default (300s) is tuned by assumption, not
measured against Reddit's actual unauthenticated budget. The browser
fallback shares one Chromium instance and one browser context across every
subreddit and every poll cycle for the life of the process (launching
fresh per subreddit or per cycle would multiply an already-expensive
fallback for no benefit), so one account running this strategy holds one
background Chromium process open for as long as it's configured this way
-- fine for a handful of accounts, worth revisiting if this strategy is
ever assigned to many. The stealth measures in `_default_browser_factory`
(masking `navigator.webdriver`, a realistic UA/viewport/locale) are the
minimum verified to work as of this writing; Reddit is free to tighten its
challenge in a way that defeats them without notice, same as it could
tighten or remove the plain `new.json` endpoint this exists to fall back
from.

## `trump_whisperer_qqq`

A third strategy, same shape as `reddit_sentiment_qqq` pointed at a
different sentiment source: buys one ATM QQQ call when aggregate sentiment
across Trump's recent Truth Social posts is bullish, one ATM put when it's
bearish, and closes whichever side it's holding the moment sentiment stops
supporting it -- including going neutral, not only flipping outright --
otherwise it declines. At most one contract at a time, same reasoning as
`reddit_sentiment_qqq`'s own section above (sizing/hysteresis/cool-downs
are `ctx.params` / future-strategy concerns, not platform invariants); "what's
currently held" is likewise read from `ctx.book.positions`, not re-derived
from "the current ATM strike."

Its signal comes from `crassus/trump_sentiment.py`, which polls
`https://trumpstruth.org/feed` -- an unauthenticated RSS mirror of Trump's
actual Truth Social posts, no app to register and no credentials to
provision -- and scores each post published within `max_post_age_minutes`
(default 180) with VADER. Two existing projects were the prior art
considered before writing this: `maxbbraun/trump2cash` (Twitter-era,
per-company entity detection + sentiment, buy-on-positive /
short-on-negative -- the trading-direction shape reused here, though the
entity-resolution step doesn't apply since this only ever trades QQQ) and
`TheNeuroDeveloper/TrumpTruthsMarketAnalysis` (the actual ingestion source,
`trumpstruth.org/feed`, verified live and working -- but its per-post LLM
market-impact analysis is a different cost/complexity tier than a VADER
score, same "one lightweight process reading one number" reasoning
`reddit_sentiment_qqq`'s section above gives for not adopting its reference
project's heavier stack). See `crassus/trump_sentiment.py`'s module
docstring for the full comparison.

To enable it for an account, set `strategy_id` to `trump_whisperer_qqq` in
`accounts.json`. No credentials are required; optionally set a distinctive
User-Agent as a courtesy:

```bash
export TRUMP_FEED_USER_AGENT="crassus-trump-whisperer/1.0 by u/yourname"
```

Unlike Reddit's `new` listing (already just recent posts), the RSS feed is
an append-only archive going back years, so `aggregate()` only scores posts
published within `max_post_age_minutes` of the read -- a quiet stretch with
no fresh posts reads as "no signal," not a re-scoring of old archived
content on every cycle. `min_sample_size` defaults lower than Reddit's (2
vs. 5) and the sentiment thresholds default wider (±0.2 vs. ±0.15) since
there is exactly one Trump and he doesn't post constantly -- a 3-hour
window commonly holds only 0-3 fresh posts, and individual posts tend to
read as more strongly worded than an averaged batch of Reddit comments.
Both remain configurable per account via `"params"`, passed through to
`StrategyContext.params` exactly like `reddit_sentiment_qqq`:

```json
{
  "alias": "TrumpWhisperer",
  "username": "crassus_trumpwhisp",
  "password_env": "CRASSUS_PW_TRUMPWHISPERER",
  "strategy_id": "trump_whisperer_qqq",
  "params": { "min_sample_size": 3, "bullish_threshold": 0.25, "bearish_threshold": -0.25 }
}
```

Verify the decision logic and aggregation math hermetically (no network
access):

```bash
.venv/bin/python scripts/verify_trump_whisperer.py
```

And the ingestion layer -- RSS parsing, freshness filtering, fetch error
handling, and reader caching -- against a fake `requests`-shaped session,
also with no network access:

```bash
.venv/bin/python scripts/verify_trump_ingestion.py
```

`aggregate()` also filters reposts: Truth Social supports verbatim reposts
("ReTruths"), and a real statement is sometimes restated near-verbatim
minutes later. Counting either as independent signal would silently double
the sentiment of one opinion. A similarity ratio over normalized text
(`difflib.SequenceMatcher`, stdlib) drops the older of two similar-enough
posts in favor of the fresher one already counted; `duplicate_count` on the
snapshot (surfaced in the strategy's decision metadata) records how many
were dropped this way. This is the same problem RavenPack's "novelty"
score exists to solve in real news-analytics feeds -- see the module
docstring in `crassus/trump_sentiment.py`.

An ingestion failure (HTTP error, timeout, malformed feed) is treated as
"we don't know," not "sentiment is neutral/unsupported": while flat it
declines the same way an empty result would, but while holding a position
it explicitly retains it rather than closing, since a transient failure of
a single unauthenticated third-party mirror is not evidence the position
should be closed. This distinction matters and is regression-tested
(`scenario_fetch_error_while_positioned_retains_not_sells` in
`verify_trump_whisperer.py`) precisely because it's easy to get backwards --
an earlier version of this strategy did, until review caught it.

**Known gaps**, in the same spirit as the section below: it has never
observed the feed misbehave beyond what a live spot-check turned up
(a post with an empty `<title>` -- an image/media-only post -- which scores
as neutral rather than erroring, since VADER's `compound` on empty text is
`0.0`). There is no fallback ingestion path if `trumpstruth.org` goes down
or changes its feed shape -- unlike `reddit_sentiment_qqq`'s browser
fallback, this has exactly one ingestion layer, because there is no known
JS-challenge-style obstacle to work around; if `trumpstruth.org` ever adds
one, this strategy would decline every cycle while flat (and retain,
rather than close, any held position -- see above) until an equivalent
fallback were added. There is also no maximum-staleness fallback to the
reader's last good cached snapshot on a fetch failure -- a failure simply
propagates as "unavailable this cycle" rather than serving slightly-stale
data, which would let the strategy ride out a brief outage on a still-valid
recent read instead of just holding pat. `_reader` in
`crassus/strategies/trump_whisperer.py` is a module-level singleton like
`reddit_sentiment_qqq`'s, so every account running this strategy in one
runner process already shares one `min_interval_s` cache -- appropriate
here in particular, since there is only one Trump regardless of how many
accounts are watching for him.

## Known gaps

- Trade-record field names in `Book._normalize` and `ExecutionClient._trade_matches` are **unverified** — no real `/api/me` payload has ever been observable. Both normalize defensively across plausible spellings; tighten them once P0 passes.
- `clock.session_phase` does not know about exchange holidays or early closes.
- Liquidation auto-reprovisioning (register a replacement account after a 410) is **detected and recorded but not yet implemented** — it lands with P2.
- **HTTP 410 liquidation detection is unverified against the real Worker, and that's a real gap, not a hedge.** `scripts/mock_worker.py` fabricates a 410 from `/api/me` and `/api/paper-trade` on demand, matching the documented contract. But `handleSettle` in `worker.js` deletes both the user and session record outright on insolvency (see `worker.js`, around the `insolvent` branch); the *next* request against that same session resolves through `requireSession()` returning `null`, and `/api/me` / `/api/paper-trade` report a generic 401 there, never a 410. The client's `AccountLiquidated` detector is exercised only against the mock's contract-accurate behavior -- it has never seen the real Worker do this, because the real Worker doesn't yet preserve enough state to do it. Deferred until either (a) `worker.js` writes a liquidation tombstone instead of deleting the record, so a resolved session can report 410 explicitly, or (b) the client additionally treats a 401 on a session it previously authenticated as ambiguous (worth reconciling) rather than assuming it's a benign logout. Do not read the passing `scenario_liquidation` check as evidence this works in production.
