# Scheduled Squeeze display

The 20-candidate producer is active and its first scheduled morning publication
passed archive download, offline replay and whole-spool backup verification.
The display now reads latest.json, latest-attempt.json and latest-schedule.json
from squeeze-scanner/v1/scheduled via the existing same-origin R2 proxy.
Opening or polling this panel never starts a scan or calls a market-data provider.

Freshness follows the actual 09:00/noon New York exchange-session slots. A missed,
interrupted, partial or failed slot remains visible beside the last successful
table. Claimed slots report acquisition/publication pending, becoming unconfirmed
after forty minutes (late starts plus service timeout). A missing slot record is
unconfirmed after the ten-minute start grace. A completed slot must agree with the
published run identity, scheduled time, acquisition timestamps and completion status
before the table is labelled the latest scheduled scan. Successful empty scans
remain distinct from missing publications. Prior-session results are labelled and
do not retain New badges. Scores and first-seen history remain producer-owned.

The bounded docs/squeeze-calendar.json asset is exported from the SAME installed
producer calendar package (XNYS, exchange_calendars4.13.2), beginning at the exact
activation timestamp and covering through2027-12-31 New York. It contains explicit
UTC slots, including holidays, half days and daylight-saving shifts. This is
calendar metadata, not a second acquisition scheduler. Out-of-coverage or missing
calendar metadata yields unconfirmed freshness; the UI never invents a weekday
schedule after coverage expires. Regenerate on a special-closure/calendar update,
producer schedule change, or before coverage expires, and deploy it with the page.

Reproduce the artifact with the producer Python environment:

```sh
python scripts/export_squeeze_calendar.py --start 2026-09-23T07:17:47.392121+00:00 --until 2028-01-01T00:00:00-05:00 --out docs/squeeze-calendar.json
```

Every poll stages all four documents before replacing state. HTTP/JSON/shortlist
validation errors retain the last complete table with an unavailable label; an older
poll cannot overwrite a newer one. The existing cache-busted, no-store proxy route
is reused. No Worker, collector, auth, trading or backup behavior changes here.

Validation covers scheduled publication, empty/unavailable results, failed attempts,
missed/interrupted/pending slots, midnight/weekend/holiday/half-day/DST boundaries,
calendar expiry, delayed publication, inconsistent run identity, concurrent polls,
failed JSON and hostile ticker escaping. Legacy manual formatting tests remain
for compatibility; the live panel uses schedule-aware freshness.
