"""One Clock envelope for hourly crypto predictions.

A prediction is an estimate of the *next* hour's close, published
as a One Clock observation — using the real envelope names from
``site/shared/one-clock.js`` (``source``, ``id``, ``revision``,
``session``, ``instrument``, ``origin``, ``evidence``, ``eventAt``,
``availableAt``, ``value``) and served through the candle cache's
Python port of the selector (:mod:`market_data.crypto_candle_cache.one_clock`).

The mapping (contract in ``docs/specs/CRYPTO_PREDICTIONS.md``):

- ``source`` is the adapter id ``"crypto_predictions"``.
- ``instrument`` is the coin symbol; ``session`` is
  ``"continuous"`` — crypto trades 24/7, the same session string
  the candle cache uses, so a prediction never lands in an equity
  cohort.
- ``origin`` is ``"synthetic"``, always. An estimate is synthetic
  by definition: it is computed, not observed. It therefore lives
  in a different cohort from the candle cache's ``recorded`` /
  ``reconstructed`` bars, and a reader that selects
  ``origin: "recorded"`` never sees a prediction as a price — and
  a reader that selects ``origin: "synthetic"`` never sees a live
  price as an estimate. The two cohorts never mix.
- ``id`` is ``"<COIN>|1h|<target hour start ISO>"`` — one identity
  per coin per target hour.
- ``eventAt`` is the target hour's start (the close of the last
  completed 1h bar: the moment the estimate was made).
  ``valueAt`` is the same instant. ``availableAt`` is the
  producer run time, which must be at or after ``eventAt`` — the
  port's ``observation()`` rejects availability before the event,
  so a prediction can never be selectable before it exists.
- ``value`` is the estimate (``estimated_price``, ``direction``,
  ``pct_move``, ``range_low`` / ``range_high``, ``confidence``,
  ``data_window``, ``basis``, ``signals``, ``last_close``) plus
  ``target_start`` / ``target_end``. ``value`` is always an
  estimate: this adapter never emits a ``null`` value. (One Clock
  itself still allows ``null`` as a retraction for other
  adapters; predictions simply never use it.)

Retention: a prediction stays in One Clock after its target hour
passes, with its value intact, as historical evidence against
which the prediction can be judged. Nothing retracts it. A
reader judges a passed prediction by its ``eventAt`` /
``availableAt`` (and its ``target_start`` / ``target_end`` in
the value), which mark it as a past prediction, not a current
price. ``current_prediction`` still only ever returns the
latest estimate as of a cutoff, and a reader that wants the
live next-hour estimate must check that the returned row's
target hour has not passed — see :func:`current_prediction`.
The file keeps up to 7 days of past predictions, then prunes
the oldest; the retention window is a bound on file size, not
a retraction.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_data.crypto_candle_cache import one_clock as oc

SOURCE = "crypto_predictions"
SESSION = "continuous"
ORIGIN = "synthetic"
HORIZON = "1h"
HORIZON_MS = 60 * 60 * 1000
SCHEMA_VERSION = 1
DATASET = "crypto_predictions"


def artifact_prefix(symbol):
    return f"crypto/predictions/{symbol}"


def prediction_key(symbol):
    return f"{artifact_prefix(symbol)}.json"


def prediction_path(out_dir, symbol):
    """Local path of a prediction file under an output directory."""
    return Path(out_dir) / "crypto" / "predictions" / f"{symbol}.json"


def target_end_ms(target_start_ms):
    return target_start_ms + HORIZON_MS


def is_expired(target_start_ms, now_ms):
    """True once the target hour has passed."""
    return now_ms >= target_end_ms(target_start_ms)


def _iso(ms):
    return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


def prediction_id(symbol, target_start_iso):
    return f"{symbol}|{HORIZON}|{target_start_iso}"


def prediction_to_observation(symbol, estimate, target_start_ms,
                              generated_at, revision=1):
    """Map one estimate to a One Clock observation row.

    ``estimate`` is the :func:`estimator.estimate` result.
    ``generated_at`` is the producer run time (ISO-8601 UTC); it
    must be at or after the target hour's start. Raises
    ``TypeError`` via :func:`one_clock.observation` when the
    envelope is invalid. The estimate is always revision 1:
    a passed prediction is retained as-is, never superseded by
    a revision-2 null row (see the module docstring).
    """
    target_start_iso = _iso(target_start_ms)
    if estimate is None:
        raise ValueError(
            "predictions never emit a null value: a passed prediction "
            "is retained with its estimate, never retracted")
    value = {
        "estimated_price": estimate["estimated_price"],
        "direction": estimate["direction"],
        "pct_move": estimate["pct_move"],
        "range_low": estimate["range_low"],
        "range_high": estimate["range_high"],
        "confidence": estimate["confidence"],
        "last_close": estimate["last_close"],
        "data_window": estimate["data_window"],
        "basis": estimate["basis"],
        "signals": estimate["signals"],
        "target_start": target_start_iso,
        "target_end": _iso(target_end_ms(target_start_ms)),
    }
    return oc.observation({
        "source": SOURCE,
        "id": prediction_id(symbol, target_start_iso),
        "instrument": symbol,
        "session": SESSION,
        "evidence": prediction_key(symbol),
        "origin": ORIGIN,
        "revision": revision,
        "eventAt": target_start_ms,
        "valueAt": target_start_ms,
        "availableAt": generated_at,
        "value": value,
        "provider": SOURCE,
        "horizon": HORIZON,
    })


def build_prediction_file(symbol, observations, generated_at,
                          coverage="current", reason=None):
    """Build one prediction file dict from observation rows.

    ``observations`` are validated One Clock rows (revision-1
    value rows, oldest first — no retraction rows are ever
    published). ``coverage`` is ``"current"`` (the latest row's
    target hour has not passed), ``"historical"`` (every
    target hour has passed, but the value rows are retained as
    historical evidence) or ``"absent"`` (no rows at all — the
    coin cannot be estimated, e.g. no hourly bars).
    """
    rows = [oc.observation(row) for row in observations]
    for row in rows:
        if row["origin"] != ORIGIN or row["session"] != SESSION:
            raise ValueError(
                f"prediction row is not a synthetic continuous observation: {row['id']}")
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "symbol": symbol,
        "horizon": HORIZON,
        "generated_at": generated_at,
        "coverage": coverage,
        "reason": reason,
        "observations_count": len(rows),
        "observations": rows,
    }


def write_prediction_file(out_dir, symbol, content):
    """Write one prediction file under ``out_dir``; return its path."""
    path = prediction_path(out_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8",
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(content, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def read_prediction_file(path):
    """Read and validate one prediction file; return the content dict.

    Raises ``ValueError`` on a schema/dataset mismatch, an
    observation-count mismatch, or a non-synthetic row. A missing
    file raises ``FileNotFoundError`` — the caller labels that
    "absent", it never stands in for an empty file.
    """
    content = json.loads(Path(path).read_text(encoding="utf-8"))
    if content.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version in {path}")
    if content.get("dataset") != DATASET:
        raise ValueError(f"unsupported dataset in {path}")
    rows = content.get("observations")
    if not isinstance(rows, list):
        raise ValueError(f"no observations list in {path}")
    if content.get("observations_count") != len(rows):
        raise ValueError(f"observations_count mismatch in {path}")
    for row in rows:
        checked = oc.observation(row)
        if checked["source"] != SOURCE or checked["instrument"] != content.get("symbol"):
            raise ValueError(f"wrong source or instrument in prediction file {path}")
        if checked["origin"] != ORIGIN:
            raise ValueError(f"non-synthetic row in prediction file {path}: {row['id']}")
        if checked["session"] != SESSION:
            raise ValueError(f"non-continuous row in prediction file {path}: {row['id']}")
    return content


def current_prediction(observations, symbol, cutoff_ms, max_age_ms):
    """The live estimate as of a cutoff, via the One Clock reader.

    Selects with ``origin: "synthetic"`` — the estimate cohort
    only — and returns :func:`one_clock.reading_as_of`'s
    ``{"state", "row", "ageMs", "publicationAgeMs"}``. ``state`` is
    ``"unavailable"`` when no prediction exists or when the
    estimate is older than ``max_age_ms``. Otherwise the state
    is ``"fresh"`` (age within ``max_age_ms``) or ``"stale"``
    (older than ``max_age_ms``) — and in both cases the row's
    value is intact: a passed prediction is retained, never
    retracted, so a reader judging it after its target hour
    gets the estimate itself, with ``eventAt`` /
    ``availableAt`` and the value's ``target_start`` /
    ``target_end`` marking it as a past prediction. Callers
    that need only the *live* next-hour estimate must check
    that the returned row's target hour has not passed at the
    cutoff (``not is_expired(eventAt, cutoff)``) — the panel
    does exactly that.
    """
    return oc.reading_as_of(
        observations,
        {"session": SESSION, "instrument": symbol, "cutoff": cutoff_ms,
         "origin": ORIGIN},
        max_age_ms)
