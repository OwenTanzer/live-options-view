# TimeAndSale ordering follow-up — 2026-11-10

Status: **prepared and tested on an isolated branch, not merged, not deployed.**
Base: current `master` (`a855bdef5d3776d357087806b24ab30c51091b6d` at branch
creation; #203 released as `94301e08`).
Branch: `steve/e1-timesale-ordering`.

## Defect

Release close-out found that a NEW `TimeAndSale` print was ordered by provider
time alone. dxFeed orders time and sales by the unique per-symbol index composed
of time and sequence, so a later print in the same millisecond must still win.
Instead the equal-time print was rejected as `out_of_order` and the earlier
print stayed selected — reproduced at price 100 with sequence 1 surviving over
price 101 with sequence 2. A second symptom was that a duplicate identity NEW
was stored as a "correction", overwriting the selected price with a replay of
the same event.

## Change

`market_data/live_market/feed.py`:

- NEW prints are ordered by `(time, sequence)`; an equal-time higher sequence
  replaces the print, an equal or lower sequence is rejected, and a duplicate
  identity is counted `duplicate` and ignored.
- An equal-time event with a missing sequence on either side fails closed
  (`out_of_order`) instead of being guessed later; a later time without a
  sequence is still accepted.
- Corrections keep the stored index/sequence/trade identity when the correction
  omits those fields, so a correction cannot degrade later ordering by writing
  nulls over a known identity.
- Exact integer semantics are unchanged: identity and sequence fields still use
  the exact parser, reject fractional values, and never round-trip through
  float.

No subscription, channel, selection, consumer or configuration change. The
`out_of_order` behaviour for older snapshot prints is unchanged.

## Tests

`tests/test_price_observations.py` adds:

- equal-time higher sequence accepted; equal-time lower sequence rejected and
  equal identity counted `duplicate`;
- duplicate NEW identity rejected before ordering;
- equal-time without a sequence fails closed; later time without a sequence
  accepted;
- large integer sequences (above 2**53) order exactly, then match a correction
  and a cancellation by identity;
- out-of-order snapshot prints do not rewind the latest.

Four of the seven fail on the released parser; the three that pass cover
behaviour that was already correct (fail-closed equal time, later time without
sequence, older snapshot rejection).

## Verification

```text
python -m pytest tests/test_price_lane.py tests/test_price_observations.py -q
python -m unittest discover -s tests -p test_price_observations.py
```

## Release and rollback

No configuration; ships with the collector image on the normal path. Rollback
is a revert of this branch; the released `94301e08` behaviour then returns.
Chej owns review and release under tonight's authority.
