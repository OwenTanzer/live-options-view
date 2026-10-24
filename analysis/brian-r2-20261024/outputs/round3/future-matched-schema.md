# Receipt-stamped options/prints: exact lead and research requirements

Existing source lead, not a request to create infrastructure: `scripts/moo144_tradier_collector.py` in OwenTanzer/live-options-view, inspected at `26332bb0eba41618f4a8b941e74ff202c66b0fe0`. The current implementation describes the MOO-169 daily collector. QQQ archive root is `moo144/tradier`, and a daily lane uses `moo144/tradier/YYYY-MM-DD/`; optional underlyings use sibling `moo144/tradier-<lowercase symbol>/YYYY-MM-DD/`. This differs from the older one-shot probe's nested run prefix.

Existing per-day leads are `universe.json`, `health.json`, `run-started-<owner_id>.json`, `summary-<owner_id>.json`, `manifest-<owner_id>.json` and archived event segments named by the manifest. The immutable manifest/segment list should establish actual coverage; a health snapshot or successful cron declaration alone cannot. Source code persists the session's selected universe and describes restart/reconciliation handling. Code capability does not prove deployment, completion or data retention on any particular date. No collector was changed or launched for this research.

The already inspected older prefix is `moo144/tradier/2026-09-03/2026-09-03-ca9d268541/`. Its summary reports a 30-minute sample. Only part000's approximately three minutes were independently inspected: 13,270 prints, 7,959 quotes, one heartbeat, QQQ plus 16 same-day-expiry options. This establishes normalized trade/quote fields, actual event and receipt timestamps, a narrow universe and measurable lag. It cannot establish multiple independent sessions, full-market order flow, aggressor truth, complete book depth, reliable multi-leg identification, out-of-sample predictability or tradability. The summary's full-run totals are not a substitute for inspecting those objects.

Minimum preserved fields for future matched research:

| Record | Required evidence and purpose |
|---|---|
| Identity/provenance | Schema version, provider/feed, symbol, immutable source key/hash, segment/run/connection identifiers, original normalized payload; distinguish decompressed-body and stored-object hashes. |
| Print | Provider event timestamp with unit/time zone, collector receipt UTC, price, size, exchange, sequence scope, condition flags, cancel/correction and original-event reference where available. No inferred trade-side label presented as ground truth. |
| Quote | Bid/ask prices and sizes, separate biddate and askdate, collector receipt UTC, exchange/venue semantics, crossed/locked/zero handling. Both side ages must be computed; the sample's newest-side-derived age is insufficient to bound the oldest side. |
| Availability | Original receipt ordering plus first publication/archive-availability time if research simulates that consumer; clock-offset diagnostics. Upload time is not substituted for original event time. Late arrivals/corrections retain their arrival history. |
| Universe | Session-start selection timestamp/rule, expiration, call/put, strike, underlying, multipliers, all selected and omitted contracts with reasons; retain expired/delisted contracts and changes across restarts. |
| Session quality | Expected calendar open/close and time zone/DST, first/last event by symbol/type, reconnect gaps, missing segments, sequence anomalies, restart dedup scope, retention and full/partial status. Audit independently rather than trusting a single complete flag. |
| Matched targets | QQQ/other-asset quote or bar provenance and receipt-time availability on the same sessions; exact feature cutoff, backward-as-of tolerance justified by both-side age, target interval and maturity. Match ablations on common eligible observations. |

## Read-only evidence added this increment

Ten public objects were retrieved with certificate verification: health/universe/lease/manifest and the lexically first declared event segment for each of October 22 and 23. Exact keys and stored/decoded hashes are in source-lead.json and verified-source-segments.json. Both compressed segment hashes match their manifests, unlike the older sample where only the decoded body was independently hashed.

The manifests are `moo144/tradier/2026-10-22/manifest-ee7939514b95.json` and `moo144/tradier/2026-10-23/manifest-5c58abce22a8.json`. Each declares 131 event segments and complete status; that declaration is not independently verified full-day completeness. The actual sampled segments are `ee7939514b95-part-0000.ndjson.gz` and `5c58abce22a8-part-0000.ndjson.gz` in the respective daily prefixes.

| Directly inspected opening segment | October 22 | October 23 |
|---|---:|---:|
| Rows | 22,852 | 23,235 |
| Timesales | 18,482 | 18,642 |
| Top-of-book quotes | 4,308 | 4,413 |
| Excluded timesales | 62 | 180 |
| Receipt span, UTC | 13:30:00.048–13:32:08.827 | 13:30:00.039–13:32:10.123 |
| Print delivery median / p95, ms | 132 / 7,003 | 322 / 10,137.85 |
| Older quote-side age median / p95, ms | 820 / 1,405.65 | 833 / 1,436 |
| Crossed quotes | 5 | 0 |

Both segments contain QQQ and 16 options with same-day expiry. Strikes are 710–717 on October 22 and 699–706 on October 23; the different selected universes are preserved. Every sampled quote has both provider-side timestamps and receipt timestamp, with no invalid or negative side ages. Older-side age at receipt exceeds one second for 1,381/4,308 and 1,376/4,413 quotes. Quote ages would grow further by a later feature cutoff. These are real contemporaneous QQQ/options event samples across two sessions, not merely date-manifest overlap. There is still zero overlap with the frozen price-history test ending August 5, and no other cross-asset stream was audited.

Health metadata reports 1,280,103/1,022,356 timesales and 998,104/1,013,907 quotes over regular-session receipt spans, with no reconnect gaps or remaining spool backlog. However, the same metadata reports 8,276/8,120 trades delayed more than five seconds and maximum trade delivery lags 3,327,575/1,118,986ms. Receipt-time joining and late-arrival handling are essential even when a manifest says complete. Those full-day counts and maxima are metadata evidence, not calculations from all 262 event segments.

Research gate: audit representative middle/end segments, discontinuities and more sessions, then freeze a separate chronological plan before searching. Count independent sessions/episodes rather than treating prints as independent trials. For quote-classified prints, preserve unclassified/ambiguous cases and compare classification rules without calling them full order flow. Executable-return claims would additionally require defensible bid/ask fills, liquidity and latency assumptions; otherwise report predictive associations only. No event-sample model was fitted and no collector changed.

—Brian 🦡
