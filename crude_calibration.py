"""Pure EIA STEO / crude-volatility calibration math, shared by collector.py
and its Python tests. Same separation-of-concerns as market_signals.py: no
I/O, no global state, no R2/HTTP dependencies here -- every function takes
already-fetched data in and returns a normalized record out, so
tests/verify_crude_calibration.py can exercise it with hand-built fixtures.

This exists to answer one recurring question mechanically instead of by hand:
"has this month's EIA Short-Term Energy Outlook revised its crude balance/
price forecast from last month's, and how does that compare to where
crude-oil implied volatility (OVX) actually sits right now?" That's the same
comparison a debater would otherwise do by pulling two STEO PDFs and reading
off numbers -- see docs/plans/2026-08-crude-calibration.md for the motivating
example (a transcript that manually diffed EIA's June-2026 vs July-2026 STEO
crude-balance assumptions against the realized Brent-WTI spread).

EIA's STEO API (https://api.eia.gov/v2/steo/data/) returns one row per
(seriesId, period). A "vintage" here is one release of that forecast -- the
STEO published in a given month -- identified by its own `period` values
covering forecast months, not by when EIA published it. Two consecutive
monthly STEO releases typically overlap on most forecast periods but diverge
on the assumptions baked into those periods (see compare_vintages).
"""

from __future__ import annotations

from dataclasses import dataclass

# EIA v2 STEO series IDs used here. Verified 2026-08 with a live
# `GET https://api.eia.gov/v2/steo/data/?api_key=...&facets[seriesId][]=...`
# call -- all three resolve and the balance series's sign convention was
# cross-checked against api.eia.gov/v2/steo/facet/seriesId and against a real
# figure (Apr-Jun 2026 averages ~5.08 million bbl/d on T3_STCHANGE_WORLD,
# matching the "realized Q2 draw was 5.1 million barrels per day" figure from
# the debate transcript this module was built for -- see the module
# docstring above). EIA does not version series IDs the way a typical API
# versions endpoints, and STEO occasionally renames or retires a series
# across annual outlook revisions, so re-verify if this ever starts
# returning empty rows.
BRENT_SERIES_ID = "BREPUUS"   # Brent spot price, $/barrel, monthly
WTI_SERIES_ID   = "WTIPUUS"   # WTI spot price, $/barrel, monthly
# "Net Inventory Withdrawals, Total World Crude Oil and Other Liquids,"
# million barrels/day. Positive = withdrawal (a draw -- stocks are being
# consumed, i.e. tighter/bullish); negative = a build (looser/bearish). Not
# named "balance" or "PATC_WORLD" (which is *consumption*, not the
# balance) in EIA's own facet listing -- easy to mis-guess, hence the live
# check above. This is the series whose vintage-over-vintage swing the
# debate's Contention Three hinged on: prior (June-vintage, closure-assumed)
# forecast a ~7.6 million bbl/d Q3 draw; current (July-vintage,
# normalization-assumed) revised that down to ~2.2 million bbl/d -- a ~5.4
# million bbl/d swing toward a smaller draw (looser, more bearish).
BALANCE_SERIES_ID = "T3_STCHANGE_WORLD"


@dataclass(frozen=True)
class SteoPricePoint:
    period: str    # "YYYY-MM"
    brent: float | None
    wti: float | None
    balance: float | None  # net world inventory withdrawal, million bbl/d;
                            # positive = draw, negative = build -- see BALANCE_SERIES_ID


@dataclass(frozen=True)
class SteoVintage:
    release_period: str | None  # the STEO release's own vintage, e.g. "2026-07"
    points: tuple[SteoPricePoint, ...]  # forecast months contained in this release


def parse_steo_rows(rows: list[dict], release_period: str | None) -> SteoVintage:
    """Normalize raw EIA v2 STEO API rows (one dict per seriesId+period) into
    one SteoVintage keyed by forecast period.

    Each row is expected to look like
    `{"period": "2026-07", "seriesId": "BREPUUS", "value": "76.2", ...}`
    (EIA's v2 API returns `value` as a string; unparseable/missing values
    become None rather than raising, since a single bad row from a live API
    should degrade one field, not the whole vintage).
    """
    by_period: dict[str, dict[str, float | None]] = {}
    for row in rows:
        period = row.get("period")
        series = row.get("seriesId")
        if not period or series not in (BRENT_SERIES_ID, WTI_SERIES_ID, BALANCE_SERIES_ID):
            continue
        value = row.get("value")
        try:
            parsed = float(value) if value is not None else None
        except (TypeError, ValueError):
            parsed = None
        by_period.setdefault(period, {"brent": None, "wti": None, "balance": None})
        if series == BRENT_SERIES_ID:
            by_period[period]["brent"] = parsed
        elif series == WTI_SERIES_ID:
            by_period[period]["wti"] = parsed
        elif series == BALANCE_SERIES_ID:
            by_period[period]["balance"] = parsed

    points = tuple(
        SteoPricePoint(period=p, brent=v["brent"], wti=v["wti"], balance=v["balance"])
        for p, v in sorted(by_period.items())
    )
    return SteoVintage(release_period=release_period, points=points)


def point_for_period(vintage: SteoVintage, period: str) -> SteoPricePoint | None:
    for p in vintage.points:
        if p.period == period:
            return p
    return None


@dataclass(frozen=True)
class SteoRevision:
    """The vintage-over-vintage delta for one forecast period -- the
    mechanical version of "did this month's STEO revise last month's
    assumption for the same future month, and by how much."
    """
    period: str
    prior_release: str | None
    current_release: str | None
    brent_delta: float | None
    wti_delta: float | None
    balance_delta: float | None  # million bbl/d; positive = revised toward a larger
                                  # withdrawal/draw (tighter, bullish); negative = revised
                                  # toward a build (looser, bearish) -- see BALANCE_SERIES_ID


def compare_vintages(prior: SteoVintage, current: SteoVintage, period: str) -> SteoRevision | None:
    """Compare how `period`'s forecast changed between two STEO releases.

    Returns None if `period` isn't present in both vintages -- a period only
    the newer release forecasts (e.g. a month added as the outlook window
    rolls forward) has no prior value to diff against, and isn't a revision.
    """
    prior_point = point_for_period(prior, period)
    current_point = point_for_period(current, period)
    if prior_point is None or current_point is None:
        return None

    def _delta(a: float | None, b: float | None) -> float | None:
        return round(b - a, 4) if a is not None and b is not None else None

    return SteoRevision(
        period=period,
        prior_release=prior.release_period,
        current_release=current.release_period,
        brent_delta=_delta(prior_point.brent, current_point.brent),
        wti_delta=_delta(prior_point.wti, current_point.wti),
        balance_delta=_delta(prior_point.balance, current_point.balance),
    )


# -- OVX regime classification (display-only, see module docstring) ---------

# Thresholds are round numbers, not a fitted model -- OVX has historically
# traded roughly 25-40 in calm-but-uncertain crude markets and spiked past 55
# during acute physical-disruption events (2020 negative-WTI, 2022 invasion
# shock). These bands exist to give a reader a quick "is this elevated"
# read next to the raw number, not to drive any trading decision -- same
# display/observability framing as market_signals.py's momentum/RVOL status
# fields.
OVX_CALM_MAX     = 35.0  # below this: "calm"
OVX_ELEVATED_MAX = 55.0  # [OVX_CALM_MAX, this): "elevated"; at/above: "high"


def classify_ovx_regime(value: float | None) -> str:
    """"no_data" | "calm" | "elevated" | "high", per the OVX_*_MAX bands."""
    if value is None:
        return "no_data"
    if value < OVX_CALM_MAX:
        return "calm"
    if value < OVX_ELEVATED_MAX:
        return "elevated"
    return "high"
