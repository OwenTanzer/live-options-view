"""As-of selector for cached candle observations.

This is a **compatible mirror** of One Clock's selector, not a reuse
of its code — stated explicitly, as the spec requires:

- The JS core (``site/shared/one-clock.js``: ``selectAsOf``,
  ``readingAsOf``) has no Python import path a producer package can
  use; it is an ESM browser/Node module.
- The Python binding (``research/options_replay/one_clock.py``:
  ``select_as_of``, ``reading_as_of``) lives in the Trainer slice of
  ``research/``; a producer under ``market_data/`` importing it would
  invert the dependency (producer -> research) and pull in
  Trainer-only machinery (``TrainerSource``).
- The JS panel side *does* reuse the real core: see
  ``site/js/candle-cache-one-clock.js``, which imports ``selectAsOf``
  / ``readingAsOf`` from ``../shared/one-clock.js`` directly.

Semantics are copied field-for-field from ``selectAsOf`` as verified
on master 2026-11-24, and the conformance tests in
``tests/test_candle_cache_backend.py`` exercise the same cases as
``tests/fixtures/one_clock_contract.json`` (before/at/after
publication, unknown availability, duplicate delivery, correction,
rewind, missing/partial data, session/instrument isolation):

- Selection requires ``eventAt`` **and** ``availableAt`` at or before
  the cutoff, a single explicit ``origin`` cohort, and the requested
  ``session``/``instrument``. A row with ``availableAt is None`` is
  ineligible at every cutoff.
- For each identity (``source``, ``session``, ``instrument``,
  ``origin``, ``id``) the highest eligible ``revision`` wins; a
  correction is a higher revision, never an edit. Two deliveries of
  the same identity/revision with different contents raise
  ``ValueError`` ("conflicting immutable observation revision").
- Rows sort by ``eventAt``, then ``availableAt``, then identity key
  in UTF-16 code-unit order (the same ordering the Python binding
  uses in ``_key``).
- ``reading_as_of`` returns ``state`` ``fresh`` / ``stale`` /
  ``unavailable``, ``ageMs`` (cutoff minus ``valueAt`` or ``eventAt``)
  and ``publicationAgeMs`` (cutoff minus ``availableAt``).
  ``max_age_ms`` is explicit and required; a retraction (``value is
  None``) is ``unavailable`` and never resurrects a prior value.

No change is made to One Clock's own QQQ/SPY/IBIT fields: this
selector only ever sees ``source == "candle-cache"`` rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .envelope import (
    GAP_LABEL,
    INTERVAL_MINUTES,
    PARTIAL_LABEL,
    UNAVAILABLE_LABEL,
    format_time,
    parse_time,
    session_for,
)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

SESSION_OPEN_MINUTES = 9 * 60 + 30   # 09:30 ET
SESSION_CLOSE_MINUTES = 16 * 60      # 16:00 ET
MINUTES_PER_DAY = 390                # regular-session 1-minute bars


def _identity_key(row):
    """Identity key in UTF-16 code-unit order (as in one_clock.py)."""
    identity = (row["source"], row["session"], row["instrument"],
                row["origin"], row["id"])
    return json.dumps(identity, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-16-be")


def _canonical(value):
    """Deterministic canonical form for conflict detection."""
    if isinstance(value, dict):
        return ("object", tuple((k, _canonical(value[k]))
                                for k in sorted(value)))
    if isinstance(value, list):
        return ("array", tuple(_canonical(item) for item in value))
    return (type(value).__name__, value)


def _validate(row):
    """Validate one observation row (mirrors one-clock.js observation())."""
    for key in ("source", "id", "instrument", "session", "evidence"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f"{key} is required")
    if row.get("origin") not in ("recorded", "reconstructed", "synthetic"):
        raise ValueError("origin must be explicit")
    revision = row.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("revision must be a positive integer")
    row["eventAt"] = parse_time(row["eventAt"])
    if "valueAt" in row and row["valueAt"] is not None:
        row["valueAt"] = parse_time(row["valueAt"])
        if row["valueAt"] > row["eventAt"]:
            raise ValueError("value observation follows state change")
    available = row.get("availableAt")
    row["availableAt"] = None if available is None else parse_time(available)
    if row["availableAt"] is not None and row["availableAt"] < row["eventAt"]:
        raise ValueError("availability precedes observation")
    if "value" not in row:
        raise ValueError("value is required (null may represent a retraction)")
    return row


def select_as_of(rows, *, session, instrument, origin="recorded", cutoff):
    """Return the eligible observations as of ``cutoff`` (epoch ms/ISO).

    Mirrors ``site/shared/one-clock.js`` ``selectAsOf`` exactly: see
    the module docstring for the rules. ``rows`` are observation dicts
    (as produced by ``envelope.stamp_bar``); the returned rows are
    sorted by ``(eventAt, availableAt, identity)``.
    """
    cutoff_ms = parse_time(cutoff)
    if origin not in ("recorded", "reconstructed", "synthetic"):
        raise ValueError("invalid origin")
    if not session or not instrument:
        raise ValueError("session and instrument are required")
    identities = {}
    seen = {}
    for value in rows:
        row = _validate(dict(value))
        if (row["session"] != session or row["instrument"] != instrument
                or row["origin"] != origin):
            continue
        if (row["eventAt"] > cutoff_ms or row["availableAt"] is None
                or row["availableAt"] > cutoff_ms):
            continue
        key = (row["source"], row["session"], row["instrument"],
               row["origin"], row["id"])
        revision_key = (key, row["revision"])
        if (revision_key in seen
                and _canonical(seen[revision_key]) != _canonical(row)):
            raise ValueError("conflicting immutable observation revision")
        seen[revision_key] = row
        previous = identities.get(key)
        if previous is None or row["revision"] > previous["revision"]:
            identities[key] = row
    return sorted(identities.values(),
                  key=lambda r: (r["eventAt"], r["availableAt"], _identity_key(r)))


def reading_as_of(rows, *, session, instrument, origin="recorded",
                  cutoff, max_age_ms):
    """Latest eligible reading plus its freshness state.

    Mirrors ``readingAsOf``: ``state`` is ``fresh`` / ``stale`` /
    ``unavailable``; ``ageMs`` is the observation age, ``publicationAgeMs``
    the publication age. ``max_age_ms`` is explicit and required.
    """
    if (not isinstance(max_age_ms, int) or isinstance(max_age_ms, bool)
            or max_age_ms < 0):
        raise ValueError("max_age_ms must be an explicit non-negative integer")
    cutoff_ms = parse_time(cutoff)
    eligible = select_as_of(rows, session=session, instrument=instrument,
                            origin=origin, cutoff=cutoff_ms)
    if not eligible:
        return {"state": "unavailable", "row": None, "ageMs": None,
                "publicationAgeMs": None}
    row = eligible[-1]
    age_ms = cutoff_ms - row.get("valueAt", row["eventAt"])
    state = ("unavailable" if row["value"] is None
             else "stale" if age_ms > max_age_ms else "fresh")
    return {"state": state, "row": row, "ageMs": age_ms,
            "publicationAgeMs": cutoff_ms - row["availableAt"]}


def as_of_bars(rows, *, session, instrument, origin="recorded", cutoff,
               max_age_ms):
    """Selected bars at ``cutoff`` plus freshness and coverage labels.

    Returns ``{"bars", "reading", "gaps", "labels"}`` where ``bars``
    are the selected observations' ``value`` payloads in event order
    (each paired with its ``eventAt``), ``reading`` is the
    ``reading_as_of`` result, ``gaps`` is the ``gap_report`` output
    for the selected bars, and ``labels`` is the sorted set of gap /
    coverage labels that apply (``"reaction unmeasurable · price
    gap"``, ``"coverage partial"``, ``"unavailable"``) — empty when
    the selection is complete and fresh.
    """
    eligible = select_as_of(rows, session=session, instrument=instrument,
                            origin=origin, cutoff=cutoff)
    reading = reading_as_of(rows, session=session, instrument=instrument,
                            origin=origin, cutoff=cutoff,
                            max_age_ms=max_age_ms)
    bars = [{"t": format_time(row["eventAt"]), "eventAt": row["eventAt"],
             **(row["value"] or {})} for row in eligible]
    gaps = gap_report(eligible, session=session)
    labels = sorted({gap["label"] for gap in gaps})
    if reading["state"] == "unavailable":
        labels.append(UNAVAILABLE_LABEL)
        labels = sorted(set(labels))
    return {"bars": bars, "reading": reading, "gaps": gaps, "labels": labels}


def gap_report(rows, *, session):
    """Label the holes in a selected observation set, honestly.

    ``rows`` are already-selected observations for one session and
    instrument (one interval implied by their ids). For intraday
    intervals with a known step, every missing expected bar between
    the first and last selected bar is reported as
    ``"reaction unmeasurable · price gap"`` with its expected
    ``eventAt`` range — the gap is a fact about the cache, never
    filled with a carried-forward or zero price. When the selected
    rows do not cover the full regular session (first bar after
    09:30 ET or last bar before 16:00 ET on a weekday session), the
    report also carries ``"coverage partial"``. Daily/weekly rows
    have no fixed intraday step, so only the partial-coverage label
    applies to them (a missing trading day is not inferred without a
    calendar). Returns a list of ``{"label", "from", "to", "bars"}``
    entries; an empty list means no gap was found inside the covered
    range (it does not claim the session is complete — see the
    partial-coverage entry).
    """
    if not rows:
        return [{"label": UNAVAILABLE_LABEL, "from": None, "to": None,
                 "bars": 0}]
    ordered = sorted(rows, key=lambda r: r["eventAt"])
    report = []
    interval = None
    for row in ordered:
        parts = row["id"].split(":")
        if len(parts) >= 2:  # id is "<SYM>:<interval>:<ISO time>"
            interval = parts[1]
            break
    step = INTERVAL_MINUTES.get(interval)
    if step is not None:
        expected = ordered[0]["eventAt"]
        for row in ordered:
            while expected < row["eventAt"]:
                nxt = expected + step * 60_000
                report.append({"label": GAP_LABEL,
                               "from": format_time(expected),
                               "to": format_time(min(nxt, row["eventAt"])),
                               "bars": 1})
                expected = nxt
            expected = max(expected, row["eventAt"] + step * 60_000)
    first_et = datetime.fromtimestamp(ordered[0]["eventAt"] / 1000, tz=ET)
    last_et = datetime.fromtimestamp(ordered[-1]["eventAt"] / 1000, tz=ET)
    # Intraday only: daily/weekly bars are labelled by midnight UTC, so
    # their ET clock time says nothing about session coverage.
    if step is not None:
        first_min = first_et.hour * 60 + first_et.minute
        last_min = last_et.hour * 60 + last_et.minute
        weekday = first_et.weekday() < 5
        if weekday and (first_min > SESSION_OPEN_MINUTES
                        or last_min < SESSION_CLOSE_MINUTES - step):
            report.append({"label": PARTIAL_LABEL,
                           "from": format_time(ordered[0]["eventAt"]),
                           "to": format_time(ordered[-1]["eventAt"]),
                           "bars": len(ordered)})
    return report
