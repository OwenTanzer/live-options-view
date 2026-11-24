# Fed/Treasury headline mapping pilot

The existing News panel applies the headline rules when it renders an eligible article. Matching titles show a “Headline topic” badge with deduplicated tickers. This display-time annotation is separate from archived instrument mappings, prices and publication timestamps. The panel uses the published title, which may be truncated. Python replay operates on the full normalized title.

`classify_rules(..., fed_treasury_pilot=True)` opts in. All existing call sites
omit this keyword. No environment variable or production CLI flag enables it.
The separate local JSONL replay command prints classification records to stdout:

```sh
python -m market_data.news.theme_mapping observations.jsonl --run-id local-review --classified-at 2026-11-22T06:00:00Z --enable-fed-treasury
```

Use the actual replay time, not a headline's publication time. Without the flag,
this command emits the existing rules-v1 output. Input is normalized observations,
one JSON object per line, from a local file; output is not an archive commit.
It does not import the object store, fetch feeds, load credentials or publish.

## Policy and evidence

The fixed `fed-treasury-v4` table in `theme_mapping.py` maps explicit Federal
Reserve/FOMC mentions to QQQ, SPY and TLT; explicit US Treasury financing
language maps to TLT. These are broad topic-relevance candidates, not assertions
that a price moved, causal findings, directional opinions or recommendations.
The Fed label is `fed_topic`, including non-monetary-policy mentions of the same
explicit institutions. Its matching pattern and ticker set are unchanged. Even
a negated or speculative headline identifies a topic, not a confirmed event.
The v4 revision removes all Treasury proximity windows. It requires a directly
owned instrument/financing phrase (such as U.S. Treasury 2-year notes, bond
purchases or quarterly refunding), or an explicit issuer template: Treasury
sells/issues securities with a numeric tenor, auctions securities, announces a
securities auction/refunding, tenor-qualified securities sold/issued/auctioned
by Treasury, securities auction announced by Treasury, or debt issuance by
Treasury. Bare notes/bills/bonds and bare issues/sells or passive issued-by
phrases stay unknown without a numeric tenor. Directly owned security
yield/auction/sale/purchase/issuance noun constructions also qualify. No arbitrary words bridge these
relationships. Verb usages and unrelated city debt/court auctions stay unknown.
This is a deliberately limited phrase grammar, not general syntactic parsing.
No issuer entities are extracted and `conflict` remains unknown.

Only the title is matched, case-insensitively. Bare Fed, Powell, Treasury, rates
and debt remain unknown, as do unrelated headlines and non-approved sources.
Source IDs are frozen to `oilprice_com`, `eia_today_in_energy`, `techcrunch`,
`ars_technica`; later feed enablement cannot expand the pilot implicitly.
Both themes can match, and tickers/themes are deduplicated and sorted.
This deliberately sacrifices recall; Treasury sanctions and bare Fed are
examples of exclusions. Incidental reporting in four existing feeds is not
comprehensive Fed/Treasury coverage. Rules can still match incidental mentions.

The policy digest covers version, patterns, flags, sources, input field and
meaning. Opt-in records have a combined rules/mapping version and digest, and a
separate classification ID tied to the exact input revision. All existing
labels/memberships stay intact. New metadata holds exact matched title spans
and original source IDs/URLs, raw/normalized publication/update times, time
quality, first-seen/observation times and archive time if present. Missing times
stay missing. The original observation is untouched. `classified_at` remains
the enrichment time. No history is rewritten and replay output does not imply
durability or availability in an as-of reader.

`instrument_mapping` is `theme_relevance` only on a match, otherwise `unknown`;
`instruments` contains candidate ticker strings. The browser derives its display from the published source and title. Replay writes its own classification record locally.

## Verification and measurement

```sh
python -m unittest discover -s tests -p test_news_theme_mapping.py -v
python -m unittest discover -s tests -p 'test_news_*.py'
python tests/test_changed_paths.py
node tests/news_panel_view.test.js
node tests/news_generation_contract.test.js
python tests/news_theme_mapping_measure.py
git diff --check
```

Sixteen clearly synthetic headline fixtures cover all four enabled source IDs,
positive, negative, combined and ambiguous cases. They are constructed through
the existing normalizer; they are not quotes from those outlets. Tests also
exercise disallowed sources, evidence spans, missing time, revision identity,
deterministic replay, no mutation, headline-only matching and the default-off CLI.
`off_baseline.json` was generated with the untouched classifier from `9e67e45`,
then frozen. Canonical serialized outputs compare byte for byte, including IDs.
The existing News fixture suite separately pins source-format/archive/view data.
Targeted tests cover the reviewer's verb-usage and unrelated-financing false
positives, six further unrelated-financing cases, nine bare/nonfinancial security-word
regressions and sixteen positive issuer/instrument titles and Fed topic semantics without expanding subject coverage. The frozen v1 off-baseline
file is unchanged. Synthetic coverage is not a measured live precision/recall result.

Local MSI Python 3.14 measurement (five paired trials, 1,600 classifications per
arm/trial), v4: median 303.25 microseconds off and 348.51 on, incremental 45.27
microseconds per revision. The 16-record batch adds 11,682 raw bytes (730.125 per
revision) or 966 gzip bytes. Timing varies by host/load and excludes archive,
network, index generation and publication. The script reproduces the method.

## Workload and the $5/month cap

Actual delivered state: zero extra hosted requests, storage, scheduled jobs or
hosted dollars. Replay uses local CPU and stdout only. No resources were
provisioned and no paid inference is involved.

For a future in-process integration, the November 15 baseline records a $4.40
monthly producer request allowance, $0.30 storage reserve and $0.30 for readers
and probes. Those are allocations, not current billing or measured headroom.
The existing 30-minute cadence is 1,440 cycles per 30-day month; mapping would
run only for classified revisions, not once per ticker or per new feed.

Illustration using the repository's historical assumption of 35 new revisions
per day: 1,050/month adds about 0.048 CPU seconds at the measured delta. Assuming
2 KB extra per revision (a planning allowance above this fixture mean, not a
bound on arbitrary inputs) gives 2.1 MB new raw data/month. At R2 Standard
$0.015/GB-month, holding all of it for the whole month yields $0.0000315/month
unrounded. Twelve such retained cohorts yield $0.000378/month at that time.
This excludes repeated checkpoints/index copies and any historical backfill.
Same existing classification objects would need no extra object requests, but
that integration has not been built or measured. Public-view growth is zero in
this delivered pilot. Long titles/URLs and history scale require remeasurement.

[Cloudflare pricing](https://developers.cloudflare.com/r2/pricing/) checked
November 22 lists $4.50/million Class A and $0.36/million Class B operations.
Free allowance is account-wide and is not deducted here. Billing rounds up to
units: even small extra storage can cross a GB-month boundary and add $0.015;
linear estimates are not billing receipts. Current account usage was not read.
The local pilot adds $0 against the $5 cap; a future hosted enablement must fit
within existing allocations after measuring full-cycle storage/copies and
account rounding, without claiming the entire remaining reserve as free money.
