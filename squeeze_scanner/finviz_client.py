"""finviz screener fetch + the raw-row-to-FactorInputs translation.

Uses the finvizfinance package (pypi: finvizfinance) rather than scraping
finviz.com directly, matching this repo's convention of vendoring the
network-touching bits behind a small typed function set (see
tradier_options.py). finvizfinance's screener column names come from
finviz's own site and have shifted before across versions; if a run of
scan.py logs missing-column warnings, check the installed finvizfinance
version's actual DataFrame columns (`Overview().screener_view().columns`)
against FINVIZ_COLUMNS below before assuming the scanner itself is broken.
"""

from __future__ import annotations

from dataclasses import dataclass

from squeeze_scanner.scoring import FactorInputs

# finviz's own screener filter values for "Short Float" -- picking "Over 10%"
# rather than a higher floor to avoid excluding legitimate candidates before
# compute_factor_score has a chance to rank them; the score itself, not the
# filter, does the discriminating.
DEFAULT_FILTERS = {
    "Short Float": "Over 10%",
    "Average Volume": "Over 500K",  # excludes illiquid names Tradier likely can't chain-price usefully
}

FINVIZ_COLUMNS = [
    "Ticker",
    "Company",
    "Short Float",
    "Short Ratio",
    "Shs Float",
    "Rel Volume",
    "Perf Week",
    "Price",
]


@dataclass(frozen=True)
class Candidate:
    ticker: str
    company: str
    price: float
    factor_inputs: FactorInputs


def _to_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", "")
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_candidates(filters: dict[str, str] | None = None, limit: int = 50) -> list[Candidate]:
    """Fetch and translate finviz's short-interest screener into Candidates.

    Raises ImportError with an install hint if finvizfinance isn't present
    yet -- kept as a soft/lazy import so importing squeeze_scanner.finviz_client
    for its Candidate type (e.g. in tests) doesn't require the network
    dependency to be installed.
    """
    try:
        from finvizfinance.screener.overview import Overview
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "finvizfinance is required: pip install finvizfinance (see requirements.txt)"
        ) from exc

    screener = Overview()
    screener.set_filter(filters_dict=filters or DEFAULT_FILTERS)
    df = screener.screener_view(order="Short Float", ascend=False, limit=limit, columns=FINVIZ_COLUMNS)
    if df is None or df.empty:
        return []

    candidates = []
    for _, row in df.iterrows():
        short_float_pct = _to_float(row.get("Short Float"))
        days_to_cover = _to_float(row.get("Short Ratio"))
        float_shares_millions = _to_float(row.get("Shs Float"))
        relative_volume = _to_float(row.get("Rel Volume"))
        perf_week_pct = _to_float(row.get("Perf Week"))
        price = _to_float(row.get("Price"))

        # A candidate missing the core short-interest fields is meaningless
        # to this scanner regardless of the rest -- skip rather than feed
        # scoring.py None/garbage.
        if short_float_pct is None or days_to_cover is None or float_shares_millions is None:
            continue

        candidates.append(
            Candidate(
                ticker=str(row.get("Ticker")),
                company=str(row.get("Company", "")),
                price=price or 0.0,
                factor_inputs=FactorInputs(
                    short_float_pct=short_float_pct,
                    days_to_cover=days_to_cover,
                    float_shares=float_shares_millions * 1_000_000,
                    relative_volume=relative_volume if relative_volume is not None else 1.0,
                    price_change_5d_pct=perf_week_pct if perf_week_pct is not None else 0.0,
                ),
            )
        )
    return candidates
