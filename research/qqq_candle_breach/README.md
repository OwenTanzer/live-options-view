# QQQ one-minute opposite-color breach, v1

Run with Python 3.9+ and the America/New_York time zone database, from this directory.

    python3 -m unittest -v
    python3 census.py data/*.csv --output results

The three CSVs contain unadjusted Yahoo Finance OHLC at the original quote precision on the fixture dates. The timestamp marks the start of the minute. The inputs were retrieved again from the provider used for the linked analysis. URLs and checksums are in data/provenance.json. The run itself is offline. The command accepts QQQ-only data from known full-length exchange sessions. It filters to 09:30–15:59 Eastern, checks for duplicate minutes and invalid bars, and excludes incomplete or nonstandard sessions. Shortened exchange sessions are outside this CLI's coverage.

## Observer contract

Call classify(first, second) with two finalized, regular-session QQQ minute bars in New York time. It returns one eligible trial, or None for an ineligible pair. A red first bar creates an up trial, a green first bar a down trial. Qualifying opposite color and a strict high or low breach produce an event. Every other eligible pair contributes one non-event, including a second doji. The observer can key each trial by rule version, instrument and second close instant (trial_id), preserving adjacent events and deduplicating replay.

decision_at is the economic bar-close time. Historical CSVs lack receipt or revision times, so available_at is null. An operational observer needs receipt timestamps for both finalized input bars and must wait until both are available. One Clock's research registry validates source/field pairs. Sampled spot snapshots alone cannot recover OHLC. This module is an offline callback with an auditable ledger. Engine registration and live ingestion were outside this run.

The second close strictly beyond the first extreme gives beyond. Equality belongs to at_or_inside. open_beyond compares the second open to the same level. both_extremes independently requires both a strictly higher high and a strictly lower low. The flags contain no intrabar ordering.

Each event's penetration is reported in QQQ points and in units of the prior 14-minute simple average true range. The latter needs 15 consecutive prior bars for the initial previous close. An unavailable or zero scale leaves normalized depth unknown. Scale does not participate in eligibility or event selection.

outcome() settles the signed point change using the exact same-session close five minutes after decision_at. Up uses future close minus second close. Down reverses the sign. The offline ledger joins this later result to the original trial. Missing endpoints remain null, including those beyond session close.

results/trials.jsonl contains one row per eligible trial. results/counts.json contains session counts, including the intersection of both-extremes and at-or-inside. The fixture regression and synthetic boundary cases are in test_census.py.
