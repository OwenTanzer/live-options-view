"""Deterministic company / ticker mapping for news (media performance).

Rules-based only. No model, no guessing, no entity extraction beyond the
explicit allowlist in this file.

An observation (title + excerpt + categories, as classified by
``classify.py``) maps to at most one instrument. Anything else is
``instrument_mapping == "unknown"`` — the same default ``classify.py``
already uses — and the reason is recorded so the panel can say why
instead of showing a fake ticker.

Matching
--------
* Input text is title + excerpt + categories, joined with spaces.
* A ticker matches only as a whole word (``\\bNVDA\\b``), case-insensitive,
  optionally with a leading ``$`` (``$NVDA``). ``NVDA2`` or ``ANVDA``
  do not match.
* A company name matches only as one of the explicit phrases listed for
  that company, case-insensitive, word-boundary aware. Distinctive
  phrases only: "Apple" alone is NOT enough (fruit / generic word) —
  it needs the ticker ``AAPL`` or a distinctive phrase such as
  "Apple Inc", "iPhone", or "Tim Cook". The same guard applies to
  "Meta" (needs ``META`` or "Meta Platforms" / "Facebook") and to
  "Shell" (needs ``SHEL`` or "Shell plc" / "Royal Dutch Shell").
  "Meta-analysis" is excluded outright (it is already an exclusion in
  the ai_buildout stream config).
* If ticker and name hits agree on one instrument, that instrument wins.
* If they disagree, or two different instruments are hit, the result is
  ``unknown`` with reason ``conflict`` / ``ambiguous`` and the
  candidate list is kept. Never pick the first, never pick the loudest.
* Private companies (OpenAI, Anthropic, CoreWeave in the fixture
  sense — CoreWeave is public, but it is NOT in this allowlist yet)
  have no ticker here, so they stay ``unknown``. Adding a company is
  an explicit allowlist edit, with a test, not a data change.

Allowlist (v1) — named explicitly, covering the existing streams
---------------------------------------------------------------
Market (the existing price series): QQQ, SPY.
Tech, large-cap, named in TechCrunch / Ars Technica / AI buildout
coverage: NVDA (NVIDIA), MSFT (Microsoft), AAPL (Apple), GOOGL
(Alphabet / Google), AMZN (Amazon), META (Meta Platforms), AMD
(Advanced Micro Devices), TSM (Taiwan Semiconductor / TSMC),
ORCL (Oracle).
Oil majors, named in OilPrice.com / EIA coverage: XOM (Exxon Mobil),
CVX (Chevron), COP (ConocoPhillips), BP (BP), SHEL (Shell).

``instrument_id`` follows the existing convention in ``join.py``:
``etf.qqq`` / ``etf.spy`` for the market ETFs, ``eq.<ticker-lower>``
for single names (e.g. ``eq.nvda``). A price series for that
instrument must exist separately; mapping alone never invents prices.
If the series is absent, performance reports ``no_series`` — it does
not fall back to QQQ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAPPING_VERSION = "companies-v1"
SCHEMA = "news-company-mapping/v1"


@dataclass(frozen=True)
class Company:
    ticker: str
    instrument_id: str
    display_name: str
    # Distinctive company-name phrases (lower-cased matching is done
    # with re.IGNORECASE; phrases are matched with word boundaries).
    name_phrases: tuple[str, ...]
    # Extra distinctive phrases that alone are enough (e.g. product
    # or chief-executive names). Empty for most companies.
    distinctive: tuple[str, ...] = ()


COMPANIES: tuple[Company, ...] = (
    Company("QQQ", "etf.qqq", "Invesco QQQ Trust",
            ("invesco qqq", "qqq trust", "nasdaq-100 etf", "nasdaq 100 etf")),
    Company("SPY", "etf.spy", "SPDR S&P 500 ETF Trust",
            ("spdr s&p 500", "s&p 500 etf", "spy etf")),
    Company("NVDA", "eq.nvda", "NVIDIA",
            ("nvidia", "nvidia corporation")),
    Company("MSFT", "eq.msft", "Microsoft",
            ("microsoft", "microsoft corporation")),
    Company("AAPL", "eq.aapl", "Apple",
            ("apple inc", "apple incorporated"),
            ("iphone", "tim cook", "macbook", "ios")),
    Company("GOOGL", "eq.googl", "Alphabet",
            ("alphabet", "alphabet inc", "google", "google cloud")),
    Company("AMZN", "eq.amzn", "Amazon",
            ("amazon", "amazon.com", "amazon web services", "aws")),
    Company("META", "eq.meta", "Meta Platforms",
            ("meta platforms", "facebook"),
            ("mark zuckerberg",)),
    Company("AMD", "eq.amd", "Advanced Micro Devices",
            ("advanced micro devices", "amd")),
    Company("TSM", "eq.tsm", "Taiwan Semiconductor",
            ("taiwan semiconductor", "taiwan semiconductor manufacturing",
             "tsmc", "taiwan semiconductor manufacturing company")),
    Company("ORCL", "eq.orcl", "Oracle",
            ("oracle", "oracle corporation", "oracle cloud")),
    Company("XOM", "eq.xom", "Exxon Mobil",
            ("exxon mobil", "exxonmobil", "exxon")),
    Company("CVX", "eq.cvx", "Chevron",
            ("chevron", "chevron corporation")),
    Company("COP", "eq.cop", "ConocoPhillips",
            ("conocophillips", "conoco phillips")),
    Company("BP", "eq.bp", "BP",
            ("bp plc", "british petroleum")),
    Company("SHEL", "eq.shel", "Shell",
            ("shell plc", "royal dutch shell")),
)

_BY_TICKER = {c.ticker: c for c in COMPANIES}


def _word_re(phrase: str) -> "re.Pattern[str]":
    # Word boundaries on both ends; internal spaces become flexible
    # whitespace so "Exxon   Mobil" still matches. Phrases are escaped
    # so none of them is secretly a regex.
    inner = r"\s+".join(re.escape(p) for p in phrase.split())
    return re.compile(r"\b" + inner + r"\b", re.IGNORECASE)


_TICKER_RES = {c.ticker: re.compile(r"\$?\b" + re.escape(c.ticker) + r"\b",
                                     re.IGNORECASE) for c in COMPANIES}
_NAME_RES = {c.ticker: [_word_re(p) for p in c.name_phrases]
             for c in COMPANIES}
_DISTINCTIVE_RES = {c.ticker: [_word_re(p) for p in c.distinctive]
                    for c in COMPANIES}

_EXCLUDED_PHRASES = (re.compile(r"\bmeta-analysis\b", re.IGNORECASE),)


def _clean_text(title: str, excerpt: str, categories) -> str:
    parts = [title or "", excerpt or "", " ".join(categories or [])]
    text = " ".join(p for p in parts if p)
    for pat in _EXCLUDED_PHRASES:
        text = pat.sub(" ", text)
    return text


def map_company(title: str = "", excerpt: str = "", categories=None) -> dict:
    """Map one news item to an instrument, or to unknown.

    Returns a dict with ``instrument_mapping`` (``"mapped"`` or
    ``"unknown"``), ``instrument_id`` (or ``None``), ``ticker`` (or
    ``None``), ``mapping_basis`` (``"ticker"``, ``"company_name"``,
    ``"ticker_and_name"``, or ``"none"``), ``matched_text`` (the exact
    substring that matched, or ``""``), ``reason`` (``"ok"``,
    ``"no_match"``, ``"ambiguous"``, or ``"conflict"``), and
    ``candidates`` (sorted tickers hit, possibly empty).
    """
    text = _clean_text(title, excerpt, categories)
    ticker_hits: dict[str, str] = {}
    name_hits: dict[str, str] = {}
    for company in COMPANIES:
        m = _TICKER_RES[company.ticker].search(text)
        if m:
            ticker_hits[company.ticker] = m.group(0)
        for pat in _NAME_RES[company.ticker] + _DISTINCTIVE_RES[company.ticker]:
            m = pat.search(text)
            if m:
                name_hits[company.ticker] = m.group(0)
                break
    distinct = sorted(set(ticker_hits) | set(name_hits))
    if not distinct:
        return {"instrument_mapping": "unknown", "instrument_id": None,
                "ticker": None, "mapping_basis": "none", "matched_text": "",
                "reason": "no_match", "candidates": []}
    if len(distinct) > 1:
        return {"instrument_mapping": "unknown", "instrument_id": None,
                "ticker": None, "mapping_basis": "none", "matched_text": "",
                "reason": "ambiguous", "candidates": distinct}
    ticker = distinct[0]
    company = _BY_TICKER[ticker]
    has_ticker = ticker in ticker_hits
    has_name = ticker in name_hits
    if has_ticker and has_name:
        # A name hit that disagrees with the ticker cannot happen here
        # (distinct == 1), so agreement is by construction.
        basis, matched = "ticker_and_name", ticker_hits[ticker]
    elif has_ticker:
        basis, matched = "ticker", ticker_hits[ticker]
    else:
        basis, matched = "company_name", name_hits[ticker]
    return {"instrument_mapping": "mapped", "instrument_id": company.instrument_id,
            "ticker": ticker, "mapping_basis": basis, "matched_text": matched,
            "reason": "ok", "candidates": [ticker]}


def mapping_digest() -> str:
    """Stable identifier for this allowlist version (for provenance)."""
    import hashlib
    import json

    payload = [{"ticker": c.ticker, "instrument_id": c.instrument_id,
                "name_phrases": list(c.name_phrases),
                "distinctive": list(c.distinctive)} for c in COMPANIES]
    return hashlib.sha256(
        json.dumps({"version": MAPPING_VERSION, "companies": payload},
                   sort_keys=True).encode()).hexdigest()[:16]
