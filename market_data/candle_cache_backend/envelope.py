"""One Clock observation envelope for cached candle bars.

Verified against the repository's One Clock core (master, read
2026-11-24):

- JS core ``site/shared/one-clock.js`` exports ``instant``,
  ``observation``, ``observationKey``, ``selectAsOf``, ``readingAsOf``,
  ``createSourceRegistry`` and ``createClock``.
- Python binding ``research/options_replay/one_clock.py`` exports
  ``instant``, ``observation``, ``identity``, ``select_as_of``,
  ``reading_as_of`` and ``TrainerSource``.
- Fixture contract ``tests/fixtures/one_clock_contract.json`` shows
  the base row.

The envelope fields, copied verbatim in meaning from
``site/shared/one-clock.js`` ``observation()``:

=================  =======================================================
Field              Meaning
=================  =======================================================
``source``         Stable source identity. Here ``"candle-cache"``.
``id``             Immutable record identity: ``"<SYM>:<interval>:<t>"``.
``revision``       Positive integer; a correction is a higher revision
                   of the same identity, never an edit in place.
``session``        Source-qualified session: the bar's New York date
                   (``YYYY-MM-DD``), never inferred from the open page.
``instrument``     The ticker, e.g. ``"AAPL"``.
``origin``         Exactly one of ``recorded`` / ``reconstructed`` /
                   ``synthetic``. One explicit cohort per selection.
``evidence``       Stable source artifact identity: the R2 key
                   ``nasdaq/candles/<TICKER>/<interval>.json``.
``eventAt``        Observation (source) time, epoch milliseconds: the
                   bar's own start time.
``availableAt``    Earliest proven availability in this cache's
                   declared view, epoch milliseconds, or ``None``.
                   Unknown is null — never substituted with a download
                   time, a filename, or the event time. A row with a
                   null ``availableAt`` is ineligible at every cutoff.
``value``          The observed bar: ``{open, high, low, close}`` and
                   ``volume`` / ``adj_close`` / ``adj_volume`` only
                   where the base carries them. ``None`` is a
                   retraction, never a missing price.
``valueAt``        Optional; when present, the time the value itself
                   was observed, and it must not follow ``eventAt``.
``details``        Optional allowlisted provenance/coverage for this
                   record, without future summaries.
=================  =======================================================

What "carries" means here: the on-disk interval files keep the exact
PR #393 shape (plain OHLC bars, so the PR #391 panel keeps reading
them unchanged). The envelope is carried (a) in the ticker's
provenance sidecar, which records per file its ``origin``,
``revision``, ``available_at`` and ``evidence`` (see
``provenance.py``), and (b) as a read-time projection: ``cache.py``
stamps every bar it returns with ``eventAt`` / ``availableAt`` /
``origin`` / ``source`` / ``revision`` / ``evidence`` / ``session``
without mutating the stored file. A bar with no provenance is stamped
``origin="recorded"``, ``revision=1`` and ``availableAt`` from the
file's ``generated_at``; the stamp is labelled as cache-derived in
``details`` so a derived availability is never mistaken for a
proven publication time.

State chips (the ``origin`` values, shown to the reader as chips):
``recorded`` (a bar Tiingo returned), ``reconstructed`` (a bar rebuilt
from another source, e.g. a derived interval re-aggregated after a
gap was repaired — never a silently filled price), ``synthetic``
(seed/demo bars that are not market observations at all). A fake zero
is never stored: a missing bar is a gap, not a ``0`` close.

Gap language (One Clock's wording, ``docs/ONE_CLOCK.md``): a missing
bar inside otherwise-present coverage is labelled
``"reaction unmeasurable · price gap"``; a file whose first/last bars
do not span the requested window is ``"coverage partial"``. See
``selector.gap_report``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

SOURCE = "candle-cache"
ORIGINS = ("recorded", "reconstructed", "synthetic")

#: Gap inside otherwise-present coverage (One Clock's wording).
GAP_LABEL = "reaction unmeasurable \u00b7 price gap"
#: File does not span the requested window.
PARTIAL_LABEL = "coverage partial"
#: No eligible row at the cutoff at all.
UNAVAILABLE_LABEL = "unavailable"

#: Step, in minutes, for the intraday intervals (for gap detection).
INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "9m": 9, "15m": 15,
                    "30m": 30, "1h": 60, "2h": 120, "4h": 240}


def parse_time(value):
    """Parse an ISO-8601 timestamp (or epoch-ms int) to epoch ms.

    Mirrors One Clock ``instant()``: an int must be epoch milliseconds
    (>= 1e12); a string must be offset-qualified (``Z`` or ``±HH:MM``).
    Naive dates and epoch seconds are rejected.
    """
    if isinstance(value, bool):
        raise ValueError("time must be epoch milliseconds or offset-qualified ISO")
    if isinstance(value, int):
        if value < 10 ** 12:
            raise ValueError("time must be epoch milliseconds, not seconds")
        return value
    if isinstance(value, str):
        text = value.strip()
        if "T" not in text or not (text.endswith("Z") or
                                   (len(text) >= 6 and text[-3] == ":" and
                                    text[-6] in "+-")):
            raise ValueError("time must be epoch milliseconds or offset-qualified ISO")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("time must be offset-qualified")
        return int(parsed.timestamp() * 1000)
    raise ValueError("time must be epoch milliseconds or offset-qualified ISO")


def format_time(epoch_ms):
    """Format epoch milliseconds as ISO-8601 UTC (``...Z``)."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_for(epoch_ms):
    """The NYSE session date (``YYYY-MM-DD``) of a bar's event time."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=ET).date().isoformat()


def evidence_key(symbol, interval):
    """The R2 key that is this bar's stable evidence identity."""
    return f"nasdaq/candles/{symbol}/{interval}.json"


def observation_id(symbol, interval, bar_time):
    """Immutable record identity for one bar."""
    return f"{symbol}:{interval}:{bar_time}"


def stamp_bar(bar, *, symbol, interval, origin="recorded", revision=1,
              available_at=None, evidence=None, details=None):
    """Stamp one stored bar with its One Clock observation envelope.

    ``bar`` is a stored bar dict (``t``/``open``/``high``/``low``/
    ``close``, plus ``volume``/``adj_*`` where the base carries them).
    Returns a new dict: the bar's fields plus the envelope fields
    ``source``/``id``/``revision``/``session``/``instrument``/
    ``origin``/``evidence``/``eventAt``/``availableAt``/``value``.
    The stored bar itself is never mutated. ``available_at`` may be
    ``None`` (unknown availability — the row is then ineligible at
    every cutoff, exactly as One Clock requires); it is never filled
    from the bar time or the read time.
    """
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}, got {origin!r}")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("revision must be a positive integer")
    event_at = parse_time(bar["t"])
    available_ms = None if available_at is None else parse_time(available_at)
    if available_ms is not None and available_ms < event_at:
        raise ValueError("availability precedes observation")
    value = {k: bar[k] for k in ("open", "high", "low", "close")
             if k in bar}
    for key in ("volume", "adj_close", "adj_volume", "bars"):
        if key in bar:
            value[key] = bar[key]
    row = {
        "source": SOURCE,
        "id": observation_id(symbol, interval, bar["t"]),
        "revision": revision,
        "session": session_for(event_at),
        "instrument": symbol,
        "origin": origin,
        "evidence": evidence or evidence_key(symbol, interval),
        "eventAt": event_at,
        "availableAt": available_ms,
        "value": value,
    }
    if details is not None:
        row["details"] = details
    return row


def observations_for_bars(bars, *, symbol, interval, origin="recorded",
                          revision=1, available_at=None, evidence=None,
                          details=None):
    """Stamp every bar in ``bars``; returns observations sorted by time."""
    rows = [stamp_bar(bar, symbol=symbol, interval=interval, origin=origin,
                      revision=revision, available_at=available_at,
                      evidence=evidence, details=details)
            for bar in bars]
    return sorted(rows, key=lambda row: row["eventAt"])


def chip_for(origin):
    """The state chip shown for an origin. Never a fake zero."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin: {origin!r}")
    return origin
