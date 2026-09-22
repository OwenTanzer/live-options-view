"""finviz screener fetch + the raw-row-to-FactorInputs translation.

Uses the finvizfinance package (pypi: finvizfinance) rather than scraping
finviz.com directly. Two finvizfinance-specific quirks drove real bugs here
before (PR #99 review) and are documented inline rather than left implicit:

1. `Overview`'s `_parse_columns` is a no-op -- passing `columns=` to it does
   nothing, and its fixed default table doesn't include any short-interest
   fields at all. Short float / short ratio / float size require the
   `Custom` screener with explicit finviz column IDs (see
   `_CUSTOM_SCREENER_COLUMN_IDS` below; IDs come from
   `finvizfinance.constants.CUSTOM_SCREENER_COLUMNS`).
2. finvizfinance's own number-parsing (`number_convert` in its `util`
   module) applies to a column only when the *live HTML header text*
   matches a name in `finvizfinance.constants.NUMBER_COL`. The short-float
   column's live header is "Short Float" but NUMBER_COL lists it as
   "Float Short" -- a mismatch inside finvizfinance itself -- so that one
   column comes back as a raw "70.68%" string (handled fine by our own
   `_to_float`'s "%" stripping) while "Perf Week" *is* auto-converted, to a
   **fraction** (0.1672, not 16.72) and "Float" comes back as an absolute
   share count already (34,860,000.0, not "in millions"). Verified live
   against finvizfinance 1.5.0 on 2026-09-22 -- if a future finvizfinance
   release fixes either quirk, `_PARSE_CANDIDATE_ROW`'s conversions below
   will silently double-convert; the parse tests
   (tests/test_squeeze_scanner_finviz_client.py) exist to catch that by
   pinning the *expected shape* of a row, not just the code path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from squeeze_scanner.scoring import FactorInputs

# finviz's own screener filter values -- picking "Over 10%" short float
# rather than a higher floor to avoid excluding legitimate candidates before
# compute_factor_score has a chance to rank them; the score itself, not the
# filter, does the discriminating. Keys/values are finvizfinance's filter
# vocabulary (finvizfinance.constants.filter_dict), NOT the live column
# header text -- e.g. the filter key is "Float Short" even though the
# resulting column header is "Short Float" (see module docstring).
DEFAULT_FILTERS = {
    "Float Short": "Over 10%",
    "Average Volume": "Over 500K",  # excludes illiquid names Tradier likely can't chain-price usefully
}
DEFAULT_ORDER = "Short Interest Share"  # finvizfinance order_dict key, sorts by the Float Short column

# finviz's own numeric column IDs (finvizfinance.constants.CUSTOM_SCREENER_COLUMNS),
# in the order we want them to come back: Ticker, Company, Short Float,
# Short Ratio, Float, Rel Volume, Perf Week, Price.
_CUSTOM_SCREENER_COLUMN_IDS = [1, 2, 30, 31, 25, 64, 42, 65]


@dataclass(frozen=True)
class Candidate:
    ticker: str
    company: str
    price: float
    factor_inputs: FactorInputs


def _to_float(value) -> float | None:
    """None for anything that isn't a real, finite number -- NaN and inf
    included. A missing/garbage field must fail closed (candidate dropped
    or field treated as absent), never silently become 0.0 or, worse,
    survive into scoring.py's _clamp01 as a false 1.0 (see PR #99 review,
    finding 3: an all-NaN row previously scored a perfect 1.0)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    text = str(value).strip().replace("%", "").replace(",", "")
    if text in ("", "-", "nan", "NaN", "None"):
        return None
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def parse_candidate_row(row: dict) -> Candidate | None:
    """Translate one finviz Custom-screener row (as returned by
    `fetch_candidates`'s DataFrame.iterrows(), or a hand-built dict in
    tests) into a Candidate, or None if the row is unusable.

    Pure and network-free on purpose -- see
    tests/test_squeeze_scanner_finviz_client.py, which exercises this
    directly against representative parsed rows (including the actual
    finvizfinance 1.5.0 unit quirks) rather than only against
    already-correct fixtures, per the PR #99 review's finding that the
    original tests never covered real provider output shapes.
    """
    short_float_pct = _to_float(row.get("Short Float"))  # e.g. "70.68%" -> 70.68 (already percentage-scale)
    days_to_cover = _to_float(row.get("Short Ratio"))  # already plain, e.g. 6.24
    float_shares = _to_float(row.get("Float"))  # already an absolute share count, e.g. 34_860_000.0
    relative_volume = _to_float(row.get("Rel Volume"))  # already a ratio, e.g. 0.47
    perf_week_raw = _to_float(row.get("Perf Week"))  # a FRACTION from finvizfinance, e.g. 0.1672 for +16.72%
    price = _to_float(row.get("Price"))

    # A candidate missing the core short-interest fields is meaningless to
    # this scanner regardless of the rest -- drop it rather than feed
    # scoring.py a None/garbage factor.
    if short_float_pct is None or days_to_cover is None or float_shares is None:
        return None

    perf_week_pct = perf_week_raw * 100 if perf_week_raw is not None else 0.0

    return Candidate(
        ticker=str(row.get("Ticker", "")),
        company=str(row.get("Company", "")),
        price=price or 0.0,
        factor_inputs=FactorInputs(
            short_float_pct=short_float_pct,
            days_to_cover=days_to_cover,
            float_shares=float_shares,
            relative_volume=relative_volume if relative_volume is not None else 1.0,
            price_change_5d_pct=perf_week_pct,
        ),
    )


def fetch_candidates(filters: dict[str, str] | None = None, limit: int = 50) -> list[Candidate]:
    """Fetch finviz's short-interest screener (via finvizfinance's `Custom`
    screener, not `Overview` -- see module docstring) and translate each row
    with `parse_candidate_row`.

    Raises ImportError with an install hint if finvizfinance isn't present
    yet -- kept as a soft/lazy import so importing squeeze_scanner.finviz_client
    for its Candidate type (e.g. in tests) doesn't require the network
    dependency to be installed.
    """
    try:
        from finvizfinance.screener.custom import Custom
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "finvizfinance is required: pip install finvizfinance (see requirements.txt)"
        ) from exc

    screener = Custom()
    screener.set_filter(filters_dict=filters or DEFAULT_FILTERS)
    df = screener.screener_view(
        order=DEFAULT_ORDER, ascend=False, limit=limit, columns=list(_CUSTOM_SCREENER_COLUMN_IDS), verbose=0
    )
    if df is None or df.empty:
        return []

    candidates = []
    for _, row in df.iterrows():
        candidate = parse_candidate_row(row.to_dict())
        if candidate is not None:
            candidates.append(candidate)
    return candidates
