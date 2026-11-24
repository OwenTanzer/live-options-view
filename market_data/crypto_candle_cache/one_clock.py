"""One Clock synchronisation for cached crypto candles.

A faithful Python port of the selection rules in
``site/shared/one-clock.js`` (master ``901f1fec``), which this
module mirrors name for name so a reader can check the two
against each other:

- ``observation`` — a bar becomes an observation with ``source``,
  ``id``, ``instrument``, ``session``, ``evidence``, ``origin`` in
  ``recorded`` / ``reconstructed`` / ``synthetic``, a positive
  integer ``revision``, ``eventAt`` / ``valueAt`` / ``availableAt``
  in epoch milliseconds, and a ``value`` (``null`` is a retraction,
  never a fake zero).
- ``select_as_of`` — :func:`selectAsOf`: same session / instrument /
  origin filter, same ``eventAt <= cutoff`` and ``availableAt``
  non-null and ``<= cutoff`` rule, same highest-revision-wins
  dedupe, same conflict error on two different encodings of one
  revision, same sort.
- ``reading_as_of`` — :func:`readingAsOf`: same ``fresh`` / ``stale``
  / ``unavailable`` states, same age from ``valueAt`` (else
  ``eventAt``), same publication age from ``availableAt``.

The mapping from a cached bar to an observation is the contract
in ``docs/specs/CRYPTO_CANDLE_CACHE.md``:

- ``instrument`` is the coin symbol (``"BTC"``); ``session`` is
  ``"continuous"`` — crypto trades 24/7, there is no equity
  session to name, and the session string is what keeps a crypto
  bar out of an equity cohort (and vice versa).
- ``source`` is the cache adapter id ``"crypto_candle_cache"``;
  the provider (Coinbase / Tiingo / CoinGecko) travels in the
  ``provider`` field and in ``evidence`` (the R2 key), never by
  relabelling the source.
- ``eventAt`` is the bar's **close** (start + interval width): the
  moment the bar's value became final. ``valueAt`` is the bar's
  start. That ordering is forced by One Clock itself — ``valueAt``
  must not follow ``eventAt`` — and it has a real consequence: a
  bar is only selectable once it has closed.
- ``availableAt`` is ``observed_at``: when this collector fetched
  the bar. A bar fetched before its close (the still-forming latest
  bar) has ``availableAt < eventAt``, which ``observation()``
  rejects — so forming bars are **not** observations. They are
  reported separately as ``forming`` and never served as a price.
  A bar observed early stays forming at later cutoffs too: it can
  only become an observation when a later fetch observes it after
  its close.
- ``origin`` is the bar's state chip: ``"recorded"`` for a base bar
  fetched from a provider, ``"reconstructed"`` for a bar derived
  locally by :mod:`resample`. ``"synthetic"`` is never emitted by
  this collector: a price is never invented, carried forward, or
  zero-filled. The chip exists in the vocabulary for a source that
  explicitly marks a bar synthetic; this collector has no such
  source.
- ``id`` is ``"<COIN>|<interval>|<bar start ISO>"``; ``revision``
  is 1 for a stored bar (a re-fetch that changes a stored bar is a
  new revision of the same id, never a silent overwrite — see
  :mod:`cache`).

Gaps are labelled, not smoothed over. :func:`detect_gaps` reports
every place where consecutive bar starts are further apart than
1.5x the interval width as a ``price gap`` (``missing_bars`` is the
count of whole bars absent), and :func:`coverage_label` reports a
requested window as ``"coverage partial"`` when bars do not span
it. Both labels are the reader's honest language, in One Clock's
terms: the selection still returns what exists, and the gap is
stated next to it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .resample import INTERVAL_MINUTES, _parse_time

SOURCE = "crypto_candle_cache"
SESSION = "continuous"
ORIGINS = ("recorded", "reconstructed", "synthetic")


def instant_ms(value, name="time"):
    """Normalise an epoch-ms int or offset-qualified ISO string to ms.

    Mirrors ``instant()``: naive dates and epoch seconds are
    rejected — callers must normalise units explicitly.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 10**12:
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise TypeError(f"{name} must be epoch milliseconds or an offset-qualified timestamp")
        if parsed.tzinfo is None:
            raise TypeError(f"{name} must be epoch milliseconds or an offset-qualified timestamp")
        return int(parsed.timestamp() * 1000)
    raise TypeError(f"{name} must be epoch milliseconds or an offset-qualified timestamp")


def _require(value, name):
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} is required")
    return value


def bar_end_ms(bar, interval):
    """Epoch ms of a bar's close (start + interval width)."""
    start = _parse_time(bar["t"])
    if interval == "1w":
        # Weekly bars start Monday 00:00 UTC; the week ends the
        # following Monday 00:00 UTC.
        return int((start + timedelta(days=7)).timestamp() * 1000)
    width = INTERVAL_MINUTES[interval]
    return int((start + timedelta(minutes=width)).timestamp() * 1000)


def observation_key(row):
    """Identity of an observation: source, session, instrument, origin, id."""
    return json.dumps([row["source"], row["session"], row["instrument"],
                       row["origin"], row["id"]])


def observation(row):
    """Validate one observation row, mirroring ``observation()``.

    Returns a copy of the row. Raises ``TypeError`` on a missing
    required field, an unknown origin, a non-positive revision, a
    ``valueAt`` after ``eventAt``, or an ``availableAt`` before
    ``eventAt``. ``availableAt`` may be null (a row that can never
    be selected — the same rule as the JS original).
    """
    row = dict(row)
    for key in ("source", "id", "instrument", "session", "evidence"):
        _require(row.get(key), key)
    if row.get("origin") not in ORIGINS:
        raise TypeError("origin must be explicit")
    revision = row.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise TypeError("revision must be a positive integer")
    row["eventAt"] = instant_ms(row["eventAt"], "eventAt")
    if row.get("valueAt") is not None:
        row["valueAt"] = instant_ms(row["valueAt"], "valueAt")
        if row["valueAt"] > row["eventAt"]:
            raise TypeError("value observation follows state change")
    available = row.get("availableAt")
    row["availableAt"] = None if available is None else instant_ms(available, "availableAt")
    if row["availableAt"] is not None and row["availableAt"] < row["eventAt"]:
        raise TypeError("availability precedes observation")
    if "value" not in row:
        raise TypeError("value is required (null may represent a retraction)")
    return row


def bar_to_observation(bar, coin, interval, key, revision=1, provider=None):
    """Map one cached bar to a One Clock observation.

    ``key`` is the R2 key of the interval file the bar came from;
    ``provider`` is the bar's provider (Coinbase / Tiingo /
    CoinGecko), defaulting to the bar's own ``source`` field.
    Raises ``TypeError`` for a forming bar (``availableAt`` before
    the bar's close) — callers must filter forming bars first with
    :func:`split_forming`.
    """
    origin = bar.get("origin", "recorded")
    value = {field: bar[field] for field in ("open", "high", "low", "close")
             if field in bar}
    if bar.get("volume") is not None:
        value["volume"] = bar["volume"]
    if bar.get("bars") is not None:
        value["bars"] = bar["bars"]
    return observation({
        "source": SOURCE,
        "id": f"{coin}|{interval}|{bar['t']}",
        "instrument": coin,
        "session": SESSION,
        "evidence": key,
        "origin": origin,
        "revision": revision,
        "eventAt": bar_end_ms(bar, interval),
        "valueAt": instant_ms(bar["t"], "t"),
        "availableAt": bar.get("observed_at"),
        "value": value,
        "provider": provider if provider is not None else bar.get("source"),
        "interval": interval,
    })


def split_forming(bars, interval, now_ms):
    """Split bars into (completed, forming) at ``now_ms``.

    A bar is forming when its close is after ``now_ms`` — or when it
    was fetched before its close (``observed_at`` before the close).
    The second case matters at later cutoffs: a bar observed early
    can never become a valid observation with that fetch time, since
    One Clock rejects availability before the event; it stays
    forming until a later fetch observes it after its close. Forming
    bars are not observations and must not be passed to
    :func:`select_as_of`; they are reported alongside the selection
    so the reader can say "forming" instead of inventing a price.
    """
    completed, forming = [], []
    for bar in bars:
        close_ms = bar_end_ms(bar, interval)
        observed_ms = (instant_ms(bar["observed_at"], "observed_at")
                       if bar.get("observed_at") else None)
        if close_ms > now_ms or (observed_ms is not None and observed_ms < close_ms):
            forming.append(bar)
        else:
            completed.append(bar)
    return completed, forming


def to_observations(bars, coin, interval, key, now_ms=None, provider=None):
    """Map cached bars to observations, excluding forming bars.

    Returns ``(observations, forming)``. A bar without
    ``observed_at`` has null availability and is therefore never
    selectable — the same rule as ``selectAsOf``; it is still
    returned as an observation so the caller can see it.
    """
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    completed, forming = split_forming(bars, interval, now_ms)
    return [bar_to_observation(bar, coin, interval, key, provider=provider)
            for bar in completed], forming


def select_as_of(inputs, selection):
    """Select observations as of a cutoff, mirroring ``selectAsOf``.

    ``selection`` is ``{"session", "instrument", "cutoff",
    "origin"}`` (``origin`` defaults to ``"recorded"``). Only rows
    matching session/instrument/origin, with ``eventAt <= cutoff``
    and non-null ``availableAt <= cutoff``, survive; per identity
    the highest revision wins; two different encodings of one
    revision raise ``ValueError``. Returns rows sorted by
    ``eventAt``, ``availableAt``, identity.
    """
    session = _require(selection.get("session"), "session")
    instrument = _require(selection.get("instrument"), "instrument")
    cutoff = instant_ms(selection.get("cutoff"), "cutoff")
    origin = selection.get("origin", "recorded")
    if origin not in ORIGINS:
        raise TypeError("invalid origin")
    identities = {}
    seen = {}
    for raw in inputs:
        row = observation(raw)
        if (row["session"] != session or row["instrument"] != instrument
                or row["origin"] != origin):
            continue
        if (row["eventAt"] > cutoff or row["availableAt"] is None
                or row["availableAt"] > cutoff):
            continue
        key = observation_key(row)
        revision_key = json.dumps([key, row["revision"]])
        encoded = json.dumps(row, sort_keys=True, default=str)
        if revision_key in seen and seen[revision_key] != encoded:
            raise ValueError("conflicting immutable observation revision")
        seen[revision_key] = encoded
        previous = identities.get(key)
        if previous is None or row["revision"] > previous["revision"]:
            identities[key] = row
    return sorted(identities.values(),
                  key=lambda r: (r["eventAt"], r["availableAt"], observation_key(r)))


def reading_as_of(inputs, selection, max_age_ms):
    """Latest reading as of a cutoff, mirroring ``readingAsOf``.

    ``max_age_ms`` must be an explicit non-negative int. Returns
    ``{"state", "row", "ageMs", "publicationAgeMs"}`` with state
    ``fresh`` / ``stale`` / ``unavailable``. A retraction (null
    value) never resurrects a prior value.
    """
    if not isinstance(max_age_ms, int) or isinstance(max_age_ms, bool) or max_age_ms < 0:
        raise TypeError("maxAgeMs must be explicit")
    session = _require(selection.get("session"), "session")
    instrument = _require(selection.get("instrument"), "instrument")
    cutoff = instant_ms(selection.get("cutoff"), "cutoff")
    rows = select_as_of(inputs, {"session": session, "instrument": instrument,
                                 "cutoff": cutoff,
                                 "origin": selection.get("origin", "recorded")})
    row = rows[-1] if rows else None
    if row is None:
        return {"state": "unavailable", "row": None, "ageMs": None,
                "publicationAgeMs": None}
    age_ms = cutoff - (row["valueAt"] if row.get("valueAt") is not None else row["eventAt"])
    state = ("unavailable" if row["value"] is None
             else "stale" if age_ms > max_age_ms else "fresh")
    return {"state": state, "row": row, "ageMs": age_ms,
            "publicationAgeMs": cutoff - row["availableAt"]}


def detect_gaps(bars, interval):
    """Find price gaps between consecutive bars of one interval.

    A gap is any place where successive bar starts are further
    apart than 1.5x the interval width. Returns a list of
    ``{"after", "before", "missing_bars"}`` — ``missing_bars`` is
    the count of whole bars absent between the two. An empty list
    means the file is gap-free over its own span; it says nothing
    about coverage outside that span.
    """
    if interval == "1w":
        width_ms = 7 * 24 * 60 * 60 * 1000
    else:
        width_ms = INTERVAL_MINUTES[interval] * 60 * 1000
    ordered = sorted(bars, key=lambda b: b["t"])
    gaps = []
    for previous, current in zip(ordered, ordered[1:]):
        delta = instant_ms(current["t"]) - instant_ms(previous["t"])
        if delta > 1.5 * width_ms:
            gaps.append({"after": previous["t"], "before": current["t"],
                         "missing_bars": round(delta / width_ms) - 1})
    return gaps


def coverage_label(bars, interval, window_start_ms, window_end_ms):
    """Label coverage of a requested window: ``full`` or ``partial``.

    Returns ``{"label", "first_bar", "last_bar", "gaps"}`` where
    ``label`` is ``"coverage partial"`` when the bars do not span
    the whole window or contain a price gap, and ``"coverage full"``
    otherwise. An empty bar list is ``"coverage partial"`` with the
    reason ``"no bars"``.
    """
    if not bars:
        return {"label": "coverage partial", "first_bar": None,
                "last_bar": None, "gaps": [], "reason": "no bars"}
    ordered = sorted(bars, key=lambda b: b["t"])
    gaps = detect_gaps(ordered, interval)
    first_ms = instant_ms(ordered[0]["t"])
    last_end_ms = bar_end_ms(ordered[-1], interval)
    if first_ms > window_start_ms or last_end_ms < window_end_ms or gaps:
        reason = "; ".join(
            ([f"starts {ordered[0]['t']}"] if first_ms > window_start_ms else [])
            + ([f"ends {ordered[-1]['t']}"] if last_end_ms < window_end_ms else [])
            + ([f"{len(gaps)} price gap(s)"] if gaps else []))
        return {"label": "coverage partial", "first_bar": ordered[0]["t"],
                "last_bar": ordered[-1]["t"], "gaps": gaps, "reason": reason}
    return {"label": "coverage full", "first_bar": ordered[0]["t"],
            "last_bar": ordered[-1]["t"], "gaps": []}
