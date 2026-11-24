# Media performance pipeline — news -> stock performance

Status: PR prep, fixture-only, inert by default. No live feeds, no bucket
writes, no trading signal, no sentiment model.
Repo path (intended): `market_data/news/MEDIA_PERFORMANCE.md`
Branch (intended): `jay/media-news-stock-performance`, base master
`a250f6f2e82fab0bc257ba7edd88712be2e0ffe2`
Schema: `news-performance/v1`. Mapping: `companies-v1`.

> Sequencing note (Jay, 2026-11-23): the pipeline design in sections
> 1-6 was settled FIRST. Critical Design Theory (sections 7-12) was
> applied only after that, to the visual models and the interface for
> the settled pipeline — not before, and not in parallel.

## 1. Goal

Take media/news about a company's performance and connect it to stock
performance, inside the existing news pipeline, so a reader can see —
for one story, one mapped instrument — how much that instrument moved
after the story became available, at fixed horizons, with the honesty
of each number in sight.

One question, answered per event: "After this story, what did the
mapped stock do?" Not "was the story good?", not "should I trade?".
The pipeline records moves. It does not judge stories and it does not
recommend trades.

## 2. What exists today

| Piece | File | What it does | What it does NOT do |
|---|---|---|---|
| Collect -> normalize -> classify -> archive | `market_data/news/` (`archive.py`, `normalize.py`, `classify.py`, `reader.py`) | Historical news collector (MediaFlow M2/M3/M4a). Fixture feeds: OilPrice.com, EIA Today in Energy, TechCrunch, Ars Technica. Streams: `all`, `ai_buildout`, `oil`. | No entity / instrument extraction. `classify.py` sets `entities=[]`, `instruments=[]`, `instrument_mapping="unknown"` by design. |
| Backward join | `market_data/news/join.py` (M3) | One row per news revision, timed by when it became available (`collector` = `observed_at`, `archive` = later of `observed_at` / `archived_at`, `publication` = `published_at`, labelled hindsight). Matches BACKWARD to the latest price available by `event_at`, with tolerance / stale / missing. Event-known history; later context kept separate. | No forward performance. A row tells you what the price was *before* the story, never what it did *after*. |
| Price records | `market_data/news/prices.py` | Validated read-only records: `price_time` + `available_at`, kinds `underlying_quote` / `option_quote` / `bar`, NaN rejected, a bar is never available before it closes. | No exchange calendar. A closed market shows as a stale / missing interval, never as a calendar fact. |
| Views + panel | `market_data/news/views.py`, `site/js/news-panel.js` (M4a/M4b) | Sanitized, bounded JSON view pages (`news-view/v1`); the panel renders them with `textContent` only, strict view-key checks, lease / deadline, truthful absence / failure states. | No performance fields. The News tab currently shows 7-10 lines of provenance per headline before the next headline (see the Options View critique) — provenance is at full volume, the move is not shown at all. |

## 3. Gaps this PR closes

1. No company -> ticker link. A story about NVIDIA and a story about
   "a startup raises a round" look identical to the join: both are
   `unknown`.
2. No forward look. The join answers "what was the price then?", not
   "what did it do after?".
3. No honest missing states for performance. Without a defined
   vocabulary, a missing forward price is easily rendered as 0.0% —
   a fake zero — or a stale price is presented as fresh.
4. No display contract. The panel has no place to put a move, and no
   rule for what must be legible at a glance vs. what can wait one
   interaction away.

## 4. Data flow

```mermaid
flowchart LR
    News[News archive\nobservations + classifications] --> Map[companies.py\nallowlist mapping]
    Map --> Event[join.news_events\ncollector / archive mode\n event_at]
    Event --> Base[join.match_price\nbackward: latest price\navailable by event_at]
    Base --> Fwd[performance.py\nforward_after(target, cutoff)\nearliest price_time >= target\nAND available_at <= cutoff]
    Fwd --> Rows[news-performance/v1\none row per event + instrument + horizon]
    Rows --> View[performance_views.attach_performance\nsummary inside the same view page]
    View --> Panel[media-performance-panel.js\nrender-only, textContent only]
```

```text
news (title + excerpt + categories)
  -> companies.map_company()            [mapped | unknown + reason]
  -> join.news_events(mode)             [event_at, never publication for a claim]
  -> join.match_price()                 [base price, matched / stale / missing]
  -> performance.forward_after()        [5m / 30m / 1h / 1d, earliest >= target]
  -> news-performance/v1 rows           [return_pct, return_bps, provenance]
  -> performance_views.attach_performance()
  -> media-performance-panel.js         [lead with the move; honesty label in sight]
```

No new top-level folder: this is a new feature under its owning
component, `market_data/news/`, per AGENTS.md. No KV, no bucket writes
by default, no live feeds, no producer restarts. The pipeline runs on
local fixtures; `build-views` / `publish` wiring is out of scope for
this PR and is listed as an open question.

## 5. Company / ticker mapping — `companies.py` (`companies-v1`)

Explicit allowlist only (16 entries, named in code and here):

- Market: QQQ (`etf.qqq`), SPY (`etf.spy`).
- Tech: NVDA (NVIDIA), MSFT (Microsoft), AAPL (Apple), GOOGL
  (Alphabet / Google), AMZN (Amazon), META (Meta Platforms), AMD
  (Advanced Micro Devices), TSM (Taiwan Semiconductor / TSMC), ORCL
  (Oracle).
- Oil majors: XOM (Exxon Mobil), CVX (Chevron), COP (ConocoPhillips),
  BP (BP), SHEL (Shell).

Rules:

- Match on title + excerpt + categories, case-insensitive, whole-word
  / word-boundary. `$NVDA` matches; `ANVDA` does not.
- Ticker and company-name phrases are both checked. If they agree on
  one instrument, it wins; `mapping_basis` records `ticker`,
  `company_name`, or `ticker_and_name`, plus the exact `matched_text`.
- Distinctive-phrase guard: "Apple" alone is not enough (fruit /
  generic word) — it needs `AAPL` or a distinctive phrase (Apple Inc,
  iPhone, Tim Cook). Same for "Meta" (needs `META` or Meta Platforms /
  Facebook; "meta-analysis" is excluded outright) and "Shell" (needs
  `SHEL` or Shell plc / Royal Dutch Shell).
- Ambiguous (>= 2 distinct instruments), conflicting (ticker and name
  disagree), or absent (0 hits) -> `instrument_mapping="unknown"`,
  `instrument_id=None`, `reason` in
  (`no_match`, `ambiguous`, `conflict`), `candidates` listed. Never
  guess, never pick the first hit.
- Private companies (OpenAI, Anthropic) have no ticker here and stay
  unknown. Adding a company is an explicit allowlist edit with a test,
  not a data change.
- Mapping alone never invents prices. If no price series exists for
  the mapped instrument, performance reports `no_series`.

`classify.py` is NOT changed in this PR. Its `instrument_mapping:
"unknown"` default stays; the mapper is a separate, testable step
that proves a match before anything claims one. Wiring the mapper
into `classify_rules` is a follow-up, listed as an open question.

## 6. Forward performance — `performance.py` (`news-performance/v1`)

### 6.1 Event time

`event_at` comes from `join.news_events` in `collector` or `archive`
mode only. `publication` mode is hindsight: its rows are labelled
`hindsight` and carry NO return (`return_pct` / `return_bps` are
`None`). A performance claim is never built on publication time.

### 6.2 Base price

The base is the backward-matched price from `join.match_price` for
that event — the newest price with `available_at <= event_at`.
This module never recomputes it.

- Base `missing` -> no return; row status `missing`,
  `missing_reason="no_base_price"`.
- Base `stale` -> return is still computed from that base, but the row
  is flagged `base_stale=True` and the honesty label is "stale base".
  A stale base is never silently presented as fresh.
- Base `matched` -> normal case.

### 6.3 Forward price

Horizons: `5m` (300 s), `30m` (1800 s), `1h` (3600 s), `1d` (86400 s).
For a horizon with target `event_at + horizon`, the forward price is
the EARLIEST record satisfying BOTH:

```
price_time >= target  AND  available_at <= cutoff
```

- It is never the latest price before the target, never an
  interpolation, never a carried-forward stale price.
- `forward_after()` scans the `PriceSeries` for the minimum
  `price_time` meeting both conditions.
- For `1d`, the preferred series is `bar_1d` when supplied (earliest
  daily bar with `bar_end >= target` and `available_at <= cutoff`);
  otherwise the same rule on the supplied quote / 1m-bar series, with
  the series recorded on the row. The rule does not change; only the
  series does.
- The forward lag (`forward_price_time - target`) is recorded on the
  row as `coverage.forward_lag_seconds`. A gap makes the lag large
  (e.g. NVDA fixture: 5m target 10:10, no quotes 10:10-10:19, forward
  10:20, lag 600 s). The panel shows the forward price_time, never
  just "5m".

### 6.4 Missing states (never a fake zero)

| Status | `missing_reason` | Meaning |
|---|---|---|
| `matched` | — | Return computed; see honesty label (`fresh` / `stale base`). |
| `missing` | `not_yet_available` | Cutoff is before the target. The move has not happened yet in the data. |
| `missing` | `no_forward_price` | Cutoff is past the target but no record qualifies (gap / thin series). Market-closed is NOT asserted — there is no exchange calendar in this repo. |
| `missing` | `no_series` | No price records for this instrument / series at all. No fallback to QQQ. |
| `missing` | `no_base_price` / `unknown mapping` | No base, or no instrument to price. |
| `hindsight` | `publication mode is hindsight` | No return, ever. |

A missing horizon has `return_pct=None`, `return_bps=None`,
`forward=None`. A renderer that prints `0.0%` for a missing row is
wrong by construction; the tests assert `None`, not zero.

### 6.5 Returns and schema

Simple return `(forward - base) / base`, as `return_pct` (percent)
and `return_bps` (basis points). Zero / non-finite base is rejected.
NaN / infinite prices are already rejected by `prices.parse_record`;
this module rejects them again defensively.

One row per (event, instrument, horizon). Every row carries:
`event_id`, `item_id`, `revision_id`, `title`, `event_at`, `mode`,
`hindsight`, `instrument_id`, `mapping_basis`, `matched_text`,
`horizon`, `horizon_seconds`, `target_at`, `cutoff`, `base`
(status, record summary, `age_seconds`), `forward` (summary or
`None`), `return_pct`, `return_bps`, `status`, `missing_reason`,
`base_stale`, and `coverage` (series, `records_in_series`,
`forward_age_seconds`, `forward_lag_seconds`).

`performance_for_join(join_result, series_by_instrument)` runs the
above for every row of a `join.join()` result and returns
`{schema_version, mode, hindsight, cutoff, horizons, rows, summary}`.
Rows for unmapped events are still emitted (as `missing`), so the
output never silently drops an event.

### 6.6 Display hook — `performance_views.py`

`attach_performance(view_page, performance_result)` returns a copy
of a sanitized view page with a `performance` summary on each row,
matched by `(item_id, revision_id)`. A row with no performance rows
gets `performance: None` — the panel renders "performance not yet
available", never a zero.

The summary is one entry per horizon: `status`, `honesty_label`
(see section 8, Gate 1), `return_pct` / `return_bps` (only when
matched, else `None`), `forward_price`, `forward_price_time`,
`base_price`. Full provenance stays in the performance row and in
the view row's existing fields; the panel shows it one interaction
away (collapsed "Methodology & provenance"), never as a wall of
provenance before the headline.

This hook does NOT change `views.py` validation, limits, or key
rules, and does NOT change `site/js/news-panel.js` key validation,
lease, or deadline logic. The performance summary travels inside the
same view page the panel already validated; the new panel is
render-only (see section 9).

### 6.7 What this does NOT do

- No trading signal, no recommendation, no "buy / sell".
- No sentiment model. Direction in the panel (section 10, Gate 4)
  is the sign of the return, not a sentiment score.
- No live feeds, no bucket writes, no KV. Fixture-only by default.
- No 1d close-to-close corporate-action adjustment. A `1d` return is
  a raw price return on the stated series, with the series named.
- No backtest, no aggregate statistics. One event, one instrument,
  one horizon at a time; aggregation is a later PR with its own
  lookahead rules.
- No change to `join.py`'s backward contract, `classify.py`'s
  default, or `news-panel.js` validation.

### 6.8 How to run it on fixtures

```sh
# After the execution-risk blocker is cleared (see bundle README):
python -m unittest discover -s tests -p "test_news_media_performance.py" -v
python -m unittest discover -s tests -p "test_news_*.py" -v
node --check site/js/media-performance-panel.js
node tests/news_panel/media_performance.test.js
```

Fixtures: `tests/fixtures/news/prices/media_performance.synthetic.jsonl`
(NVDA + XOM, invented prices, deliberate NVDA gap 10:10-10:19) and
`tests/fixtures/news/media_performance_events.json` (four events:
NVDA mapped, XOM mapped, unmapped, NVDA stale-base). All synthetic.
`tests/test_news_fixtures.py` pins SHA-256 of every file under
`tests/fixtures/news/` — add the two new fixture files to its pin
list when applying the bundle.

## 7. Critical Design Theory — applied after the pipeline (Gate 0)

Sources (used, not invented): "Critical Design Theory — Skill R&D"
(https://app.notion.com/p/4213c519147b8188a27cfe64285fe277) and
"Options View — a critique of every interface page"
(https://app.notion.com/p/41e3c519147b81acbb06cf2e67541a27).
Skill source: `~/.claude/skills/critical-design-theory/SKILL.md`;
draft PR #279 on live-options-view was closed by Musef Nov 20
without merging. The critique's thesis is the starting point:
the site solved "don't mislead" better than "what should I see
first", and honesty has been implemented as volume instead of depth.

### Gate 0 — Mission (concrete, at product / page / view / component level)

- Product: Options View connects a live options chain to the news
  that may have moved it, without misleading a reader about what was
  known when.
- Page (News): show, for each story, what the mapped stock did after
  it — and make the absence of a move as legible as a move.
- View (Media performance, inspection mode — see Gate 3): one story,
  one instrument, four horizons. The reader's question is "after this
  story, what did it do?"
- Component (performance row): the move first, at a glance; the
  honesty label in the same line of sight; methodology and full
  provenance one interaction away.
- What must be legible at a glance: the instrument, the largest
  available horizon return (or "not yet available"), and whether
  that number is fresh, stale-base, pending, or hindsight.
  What can wait: the exact forward price_time, the base record id,
  the classification arc, the mapping basis, and the full
  event/published/observed/archived chain.
- Mission -> design link, kept through every gate below: if a design
  choice hides the move, buries the honesty label, or makes a missing
  number look like zero, it fails Gate 0 even if it is honest in the
  fine print.

## 8. Gate 1 — Honesty Contract

Every forward return carries its honesty label in the line of sight.
The vocabulary is fixed (see `performance_views.honesty_label`):
`fresh`, `stale base`, `pending`, `not yet available`, `no coverage`,
`unknown mapping`, `hindsight`. Never a fake zero, never a stale
price presented as fresh.

Smoothing / interpolation / fabrication — list, and how the screen
admits each:

| Fabrication | In this pipeline? | How the screen admits it |
|---|---|---|
| Interpolation between prices | No. `forward_after` picks the earliest real record >= target; it never interpolates. | The forward `price_time` is shown, so a 5m label with a 10:20 forward price is visibly 15 minutes late (lag 600 s in the fixture). |
| Carrying a stale price forward | No. A price before the target is never used as the forward price. | If no record qualifies, the horizon shows "not yet available", not the last price. |
| Base price staleness | Yes, possible: base may be `stale` (older than tolerance at the event). | Label "stale base" sits next to the return; the base age is in the collapsed provenance. |
| 1d close | The `1d` return is a raw price return on the named series (`bar_1d` or quotes), with no corporate-action adjustment. | The series is named on the row; methodology says so in one line. |
| Hindsight | Publication mode only, and it carries no return. | Label "hindsight — not a performance claim". |
| Missing data | Reported, never filled. | "not yet available" / "no coverage" / "unknown mapping", with the reason in the disclosure. |

Methodology is one interaction away, not inline at full volume: a
single "Methodology & provenance" disclosure per row (or per view)
holds the event / published / observed / archived times, revision,
classification, mapping basis and matched text, base and forward
record ids and times, horizon definitions, and the no-lookahead rule.
The short labels above stay in sight because they change how you
read the number right now; the methodology does not.

## 9. Gate 2 — Divergent mockups (three structurally different models)

Each alternative sets the reader's sense of time differently, and
answers a different question first. All three use the same data
(`news-performance/v1` rows) and the same honesty labels.

### A. Timeline strip (temporal)

```
Story ─────●───────────────────────● 5m  ● 30m  ● 1h  ● 1d
10:05      10:10 (+0.83%)        10:35 (+1.66%)  11:05 (+3.32%)
```
- Time is horizontal; the story is a point on a line, the moves are
  points after it. Answers first: "what happened after, and when?"
- Strength: the gap is visible (10:10-10:19 with no NVDA quotes is
  empty space, not a hidden jump). Weakness: the move itself is small
  — the eye reads time before magnitude, and four horizons compete
  for the same line. A newcomer must learn to read a timeline before
  they can answer "did it go up?"

### B. Lead-with-move card (inspection) — CHOSEN

```
NVDA  +3.32%  in 1h  ·  fresh
  5m +0.83% · 30m +1.66% · 1h +3.32% · 1d not yet available
  "NVIDIA beats on data-center revenue…" — TechCrunch, 10:05 UTC
  [Methodology & provenance ▸]
```
- The object (the move) leads; the story follows. Answers first:
  "how much did it move?" Time is a set of labelled chips, not a
  line. Provenance is one interaction away.
- Strength: matches Gate 3 (inspection) and the critique's "lead
  with the object"; a newcomer reads the biggest number first and
  the honesty label with it. Weakness: the sequence story -> price
  is less explicit than in A — the card must keep the event time
  next to the return, or the move floats free of its cause.
  Mitigation: the event time and instrument are in the same block
  as the return, and the disclosure shows the full sequence.
- Why chosen: it fixes the critique's central problem (honesty as
  volume, provenance wall before the headline) without hiding the
  honesty labels, and it degrades most visibly — a missing horizon
  is an empty chip that says "not yet available", not a gap in a
  line a reader might miss.

### C. Ambient table (ambient)

```
Story                          5m      30m     1h      1d
NVIDIA beats… (NVDA)          +0.83%  +1.66%  +3.32%  —
OPEC+ cut… (XOM)              +0.45%  +0.91%  +1.36%  —
Startup raises… (—)           —       —       —       —
```
- Time is columns; the reader scans rows to compare stories.
  Answers first: "which stories moved?" Strength: comparison across
  stories is immediate, and "—" for missing is unambiguous.
  Weakness: no single story is inspected; the story title is
  truncated to fit the table, and the honesty label per cell is
  cramped. Best as an ambient overview, not as the inspection view.

Decision: ship B as the inspection view (the panel in this PR),
keep C as a named follow-up for an ambient "which stories moved
today?" view, and keep A as a documented alternative (the timeline
is the right model if a later PR adds an intra-day price chart —
see D1-D2 below). The mockup in `site/media-performance-mockup.html`
implements B with fixture data; A and C are specified in this doc
so a reviewer can compare without building them.

## 10. Gate 3 — One question per view (ambient / temporal / inspection)

- Ambient (C, follow-up): "Which stories moved today?" One row per
  story, horizons as columns, no chart. The reader monitors.
- Temporal (A, follow-up): "What happened after this story, and
  when?" One story, time horizontal. The reader follows a sequence.
- Inspection (B, this PR): "After this story, what did it do?"
  One story, one instrument, the move first. The reader inspects
  one claim.

Each view answers one question. This PR ships only the inspection
view. It leads with the object — the price move after the story —
not the provenance. Provenance (event, published, observed, archived,
revision, classification, mapping) goes one layer deeper, in the
collapsed disclosure. That is the direct fix for the critique's
News-tab finding: 7-10 lines of provenance per headline before the
next headline. No 9px grey provenance wall before the headline.

A surface may move between modes (Ash, via the skill R&D notes:
technicity phases between modes), but it must say which mode it is
in. The inspection card says "Inspection — one story" in its header;
it does not pretend to be an ambient table.

## 11. Gate 4 — One texture / one meaning per colour + depiction gates

### Gate 4 — Colour and type

- Red / green are reserved for gain / loss only. A positive return
  is green, a negative return is red, zero is neutral grey. Nothing
  else on the card uses red or green.
- News direction / instrument direction gets its own pair:
  blue / orange (the critique's suggestion; it survives
  colour-blindness). In this view, blue = the story / instrument
  identity, orange = the horizon marker — never red/green. Colour is
  always paired with a word or glyph (`+` / `−`, "up" / "down"),
  so the distinction does not depend on colour alone.
- Intensity is single-hue with monotonic lightness: a larger move is
  a darker (or more saturated) shade of the same hue, never a
  different hue, and lightness rises monotonically with magnitude.
  The CSS tokens in `media-performance-panel.css` encode this; do
  not add a second hue for "bigger".
- Type: 3-4 deliberate steps, with a 12px floor for anything meant
  to be read. Tokens: 20px return (reading number), 14px instrument
  + horizon labels, 12px story title and honesty labels, 12px
  fine print in the disclosure (never 9px). If text needs to be 9px
  to fit, it belongs one layer deeper.
- Stale looks stale: a `stale base` card is desaturated (reduced
  saturation, dated header "Base 10:35, 20 min old at event") —
  not merely preceded by a warning banner. A `pending` / `not yet
  available` horizon is a muted, empty chip, not a zero. This is
  the critique's principle 6, applied to performance.

### Depiction gates D1-D3

- D1 — Recordable or rhetorical? The performance display is
  RHETORICAL, not recordable. It does not reproduce a price series;
  it argues a sequence (story, then move) by selecting four horizons
  and leading with the largest. Say so in the methodology: "This
  view selects 5m / 30m / 1h / 1d and leads with the 1h move; it is
  an argument about timing, not a reproduction of the tape."
  No chart in this PR is neutral, and none claims to be.
- D2 — Sequence and enclosure. The sequence is story -> base price
  -> forward prices -> move, enclosed in one card per story, with
  the event time as the left edge and the horizons as the right edge.
  The card's enclosure is the claim: everything inside the card
  happened after the story, or is labelled as not having happened
  yet. A later revision does not move the event (join rule, kept).
- D3 — Modalisation and the human check. Observed vs estimated vs
  modelled, stated on the card:
  - Observed: the base and forward prices (record ids + times in
    the disclosure), the mapping basis and matched text.
  - Estimated: the return itself (a calculation from observed
    prices, with the formula in the methodology).
  - Modelled: nothing in this view. No sentiment score, no
    predicted move, no interpolated price. If a later PR adds a
    model, it must be labelled as modelled, not as observed.
  - Human check: the disclosure ends with "Check: open the story
    (link) and the forward price record (id + time) — do they match
    the claim above?" A reviewer can verify one card in under a
    minute with no coaching (see Gate 5, quick test).

### Immersion

Push for immersion only where every immersive effect is driven by a
real datum and degrades visibly when that datum is stale, missing,
or modelled. In this view that means:

- The return number's size / saturation is driven by the return
  magnitude (real datum). When the return is missing, there is no
  big number — the chip is empty and muted.
- A live "pending" horizon does not pulse or animate as if it were
  updating; it sits still and says "pending". No fake liveliness.
- No ambient animation, no synoptic field, no reading game in this
  PR. Those are future levers, each needing a truth hook first.

### Ergonomics (Owen's feedback — no extra gates)

At each gate, ask how it shows up as visual ease and operational
accessibility for a user new to the platform — decluttering,
condensing, rearranging. Working heuristics (draft — verify the
exact 7 wordings against `SKILL-v3.md` / `ergonomics.md`, emailed
to Owen 2026-11-22, before merging; the Notion excerpt fetched
for this PR lists the feedback but not the verbatim 7):

1. Declutter: remove one thing per view before adding one.
2. Condense: group by the task, then test what the user has to hold
   in mind (Cowan is a reason to look for overload, not a rule for
   counting panels — a fifth visible control can be easier than one
   hidden in a menu).
3. Rearrange: put the object first, the provenance one layer deeper.
4. One thing to hold: the reader should be able to repeat the
   claim ("NVDA +3.32% in 1h, fresh") after one glance.
5. Find it again: a returning reader, given one timestamp and no
   coaching, can get to the same card, tell what was observed vs
   estimated vs stale, open the evidence, and return to the same
   instant.
6. Visual ease: 12px floor, 3-4 type steps, one meaning per colour
   (Gate 4) — so a newcomer does not need the legend to read the
   card.
7. Operational accessibility: every action (open story, open
   methodology, change horizon) is a visible control with a word
   label, not a hover-only affordance.

Per-gate ergonomics check (applied in the mockup): Gate 0 — is the
mission readable from the card alone? Gate 1 — can a newcomer tell
fresh from stale without opening the disclosure? Gate 2 — does the
chosen layout (B) ask the newcomer to learn the least? Gate 3 — does
the view answer one question, or three at once? Gate 4 — can the
card be read if colour is removed? Gate 5 — see below.

## 12. Gate 5 — Fidelity checklist

Ship code is checked against the mockup's claims. A reviewer can
tick each line against the mockup (`site/media-performance-mockup.html`)
and the fixture data:

- [ ] Lead: the return (or "not yet available") is the largest text
  on the card; the story title is below it, not above it.
- [ ] Honesty label: every horizon shows its label in the line of
  sight (`fresh` / `stale base` / `pending` / `not yet available` /
  `no coverage` / `unknown mapping` / `hindsight`); no horizon shows
  a number without a label.
- [ ] No fake zero: a missing horizon shows no number and no `0.0%`.
  (Tests: `test_news_media_performance.py::ViewHookTests`,
  `tests/news_panel/media_performance.test.js`.)
- [ ] Horizons: 5m / 30m / 1h / 1d are labelled with their forward
  price_time when matched; the 5m chip for the NVDA fixture shows
  forward 10:20, not 10:10.
- [ ] Colour: red/green only for gain/loss; blue/orange only for
  story/horizon identity; intensity is single-hue, monotonic.
  Colour is paired with `+` / `−` and a word.
- [ ] Type: no text meant to be read is below 12px; there are 3-4
  type steps, not a collapsed 9-10px scale.
- [ ] Stale looks stale: a `stale base` card is desaturated and
  dated in its header, not just bannered.
- [ ] Methodology one interaction away: one disclosure per card
  holds event / published / observed / archived, revision,
  classification, mapping basis + matched text, base + forward
  record ids/times, horizon definitions, and the no-lookahead rule.
- [ ] D1-D3: the methodology says the view is rhetorical, names
  the sequence and its enclosure, and separates observed /
  estimated / modelled with a human check.
- [ ] Quick test (Owen's Edit 3): give a returning reader one
  timestamp (e.g. 2026-11-09 10:05 UTC, NVDA) and no coaching.
  Can they get to the card, tell what was observed vs estimated
  vs stale, open the evidence, and return to the same instant?
- [ ] No regression: `news-panel.js` key validation, lease, and
  deadline logic are unchanged; the new panel is render-only and
  never fetches on its own (see section 6.6).
- [ ] Fixture-only: no live feeds, no bucket writes, no KV, no
  secrets in the PR; all numbers in the mockup are synthetic.

## 13. Open questions (for Jay / Owen)

1. Allowlist: is the 16-entry list right? Which tickers / companies
   should be added or removed first — e.g. add SPY series, add
   CoreWeave (CRWV), add a semiconductor ETF (SMH), or keep it to
   the 16 until the mapping is proven?
2. Horizons: do 5m / 30m / 1h / 1d matter most, or should 1h be
   replaced by close-to-close, or should a 15m horizon be added?
3. View placement: should performance live inside the News view
   (this PR's choice — summary inside the same view page) or as a
   separate performance view artifact? What does the News tab
   gain/lose each way?
4. Wiring the mapper into `classify_rules` vs. keeping it as a
   separate post-classification step — which is the right home?
5. `build-views` / `publish` wiring for performance summaries:
   is the summary part of the published view, or a separate
   generation?
6. Exact wording of the 7 ergonomics heuristics in skill v3 — confirm
   against `SKILL-v3.md` / `ergonomics.md` before merging.
7. Should the ambient table (Gate 2, alternative C) be the next
   view, or the timeline (A)?

