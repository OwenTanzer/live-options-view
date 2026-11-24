"""NASDAQ-100 constituent universe (101 listings, 100 companies).

The same 101 listings as ``market_data/nasdaq_collector/symbols.py``
(PR #391, unmerged at the time of writing) — this collector is
self-contained on master and does not import that unmerged package, so
the list is embedded here as well. Re-verified live against Nasdaq's
official list-type API on 2026-11-23 (``totalrecords: 101``, API date
"Nov 23, 2026"): the live symbol set is identical to this embedded set,
and GOOGL/GOOG are both present. Three live company names carry a
trailing space ("CEG", "GEHC", "HONA"); the embedded names strip it.

Source: Nasdaq official list-type API
    https://api.nasdaq.com/api/quote/list-type/nasdaq100
Re-verify against the same endpoint after each annual reconstitution
(December) or announced change, and update SOURCE_AS_OF below.

The list is embedded so the collector has a last-known universe to
fall back on; the collector re-fetches the same endpoint on every
run and diffs the live list against this embedded one (see
:func:`diff_universe` and ``collector.resolve_universe``), so
entrants are picked up and leavers dropped without a code change.
The embedded list is the diff baseline, so keeping it current
keeps the recorded changes accurate.

Dual-class pair: Alphabet Inc. is included twice, as GOOGL
(Class A) and GOOG (Class C) — hence 101 listings for 100
companies. No other constituent has two listings in the index.
"""

from __future__ import annotations

SOURCE_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
SOURCE_AS_OF = "2026-11-23"
SOURCE_LABEL = "Nasdaq official list-type/nasdaq100 API"
EXPECTED_LISTINGS = 101
EXPECTED_COMPANIES = 100

# (symbol, company name as published by Nasdaq). Sorted by symbol.
SYMBOLS = (
    ("AAPL", "Apple Inc. Common Stock"),
    ("ABNB", "Airbnb, Inc. Class A Common Stock"),
    ("ADBE", "Adobe Inc. Common Stock"),
    ("ADI", "Analog Devices, Inc. Common Stock"),
    ("ADP", "Automatic Data Processing, Inc. Common Stock"),
    ("ADSK", "Autodesk, Inc. Common Stock"),
    ("AEP", "American Electric Power Company, Inc. Common Stock"),
    ("ALAB", "Astera Labs, Inc. Common Stock"),
    ("ALNY", "Alnylam Pharmaceuticals, Inc. Common Stock"),
    ("AMAT", "Applied Materials, Inc. Common Stock"),
    ("AMD", "Advanced Micro Devices, Inc. Common Stock"),
    ("AMGN", "Amgen Inc. Common Stock"),
    ("AMZN", "Amazon.com, Inc. Common Stock"),
    ("APP", "Applovin Corporation Class A Common Stock"),
    ("ARM", "Arm Holdings plc American Depositary Shares"),
    ("ASML", "ASML Holding N.V. New York Registry Shares"),
    ("AVGO", "Broadcom Inc. Common Stock"),
    ("AXON", "Axon Enterprise, Inc. Common Stock"),
    ("BKNG", "Booking Holdings Inc. Common Stock"),
    ("BKR", "Baker Hughes Company Class A Common Stock"),
    ("CCEP", "Coca-Cola Europacific Partners plc Ordinary Shares"),
    ("CDNS", "Cadence Design Systems, Inc. Common Stock"),
    ("CEG", "Constellation Energy Corporation Common Stock"),
    ("CMCSA", "Comcast Corporation Class A Common Stock"),
    ("COST", "Costco Wholesale Corporation Common Stock"),
    ("CPRT", "Copart, Inc. (DE) Common Stock"),
    ("CRWD", "CrowdStrike Holdings, Inc. Class A Common Stock"),
    ("CRWV", "CoreWeave, Inc. Class A Common Stock"),
    ("CSCO", "Cisco Systems, Inc. Common Stock (DE)"),
    ("CSX", "CSX Corporation Common Stock"),
    ("CTAS", "Cintas Corporation Common Stock"),
    ("DASH", "DoorDash, Inc. Class A Common Stock"),
    ("DDOG", "Datadog, Inc. Class A Common Stock"),
    ("DXCM", "DexCom, Inc. Common Stock"),
    ("EXC", "Exelon Corporation Common Stock"),
    ("FANG", "Diamondback Energy, Inc. Common Stock"),
    ("FAST", "Fastenal Company Common Stock"),
    ("FER", "Ferrovial N.V. Ordinary Shares"),
    ("FTNT", "Fortinet, Inc. Common Stock"),
    ("GEHC", "GE HealthCare Technologies Inc. Common Stock"),
    ("GILD", "Gilead Sciences, Inc. Common Stock"),
    ("GOOG", "Alphabet Inc. Class C Capital Stock"),
    ("GOOGL", "Alphabet Inc. Class A Common Stock"),
    ("HON", "Honeywell International Inc. Common Stock"),
    ("HONA", "Honeywell Aerospace Inc. Common Stock"),
    ("IDXX", "IDEXX Laboratories, Inc. Common Stock"),
    ("INTC", "Intel Corporation Common Stock"),
    ("INTU", "Intuit Inc. Common Stock"),
    ("ISRG", "Intuitive Surgical, Inc. Common Stock"),
    ("KDP", "Keurig Dr Pepper Inc. Common Stock"),
    ("KLAC", "KLA Corporation Common Stock"),
    ("LIN", "Linde plc Ordinary Shares"),
    ("LITE", "Lumentum Holdings Inc. Common Stock"),
    ("LRCX", "Lam Research Corporation Common Stock"),
    ("MAR", "Marriott International Class A Common Stock"),
    ("MCHP", "Microchip Technology Incorporated Common Stock"),
    ("MDLZ", "Mondelez International, Inc. Class A Common Stock"),
    ("MELI", "MercadoLibre, Inc. Common Stock"),
    ("META", "Meta Platforms, Inc. Class A Common Stock"),
    ("MNST", "Monster Beverage Corporation"),
    ("MPWR", "Monolithic Power Systems, Inc. Common Stock"),
    ("MRNA", "Moderna, Inc. Common Stock"),
    ("MRVL", "Marvell Technology, Inc. Common Stock"),
    ("MSFT", "Microsoft Corporation Common Stock"),
    ("MSTR", "Strategy Inc Common Stock Class A"),
    ("MU", "Micron Technology, Inc. Common Stock"),
    ("NBIS", "Nebius Group N.V. Class A Ordinary Shares"),
    ("NFLX", "Netflix, Inc. Common Stock"),
    ("NVDA", "NVIDIA Corporation Common Stock"),
    ("NXPI", "NXP Semiconductors N.V. Common Stock"),
    ("ODFL", "Old Dominion Freight Line, Inc. Common Stock"),
    ("ORLY", "O'Reilly Automotive, Inc. Common Stock"),
    ("PANW", "Palo Alto Networks, Inc. Common Stock"),
    ("PAYX", "Paychex, Inc. Common Stock"),
    ("PCAR", "PACCAR Inc. Common Stock"),
    ("PDD", "PDD Holdings Inc. American Depositary Shares"),
    ("PEP", "PepsiCo, Inc. Common Stock"),
    ("PLTR", "Palantir Technologies Inc. Class A Common Stock"),
    ("PYPL", "PayPal Holdings, Inc. Common Stock"),
    ("QCOM", "QUALCOMM Incorporated Common Stock"),
    ("REGN", "Regeneron Pharmaceuticals, Inc. Common Stock"),
    ("RKLB", "Rocket Lab Corporation Common Stock"),
    ("ROP", "Roper Technologies, Inc. Common Stock"),
    ("ROST", "Ross Stores, Inc. Common Stock"),
    ("SBUX", "Starbucks Corporation Common Stock"),
    ("SHOP", "Shopify Inc. Class A Subordinate Voting Shares"),
    ("SNDK", "Sandisk Corporation Common Stock"),
    ("SNPS", "Synopsys, Inc. Common Stock"),
    ("SPCX", "Space Exploration Technologies Corp. Class A Common Stock"),
    ("STX", "Seagate Technology Holdings PLC Ordinary Shares (Ireland)"),
    ("TER", "Teradyne, Inc. Common Stock"),
    ("TMUS", "T-Mobile US, Inc. Common Stock"),
    ("TRI", "Thomson Reuters Corporation Common Shares"),
    ("TSLA", "Tesla, Inc. Common Stock"),
    ("TTWO", "Take-Two Interactive Software, Inc. Common Stock"),
    ("TXN", "Texas Instruments Incorporated Common Stock"),
    ("VRTX", "Vertex Pharmaceuticals Incorporated Common Stock"),
    ("WDAY", "Workday, Inc. Class A Common Stock"),
    ("WDC", "Western Digital Corporation Common Stock"),
    ("WMT", "Walmart Inc. Common Stock"),
    ("XEL", "Xcel Energy Inc. Common Stock"),
)

DUAL_CLASS_PAIRS = (("GOOGL", "GOOG"),)  # Alphabet Inc., Class A / Class C


def symbols() -> list[str]:
    """Ticker symbols in the embedded universe, sorted."""
    return [symbol for symbol, _ in SYMBOLS]


def names() -> dict[str, str]:
    """Symbol -> company name as published by Nasdaq."""
    return dict(SYMBOLS)


def validate(universe: object = SYMBOLS) -> None:
    """Fail closed unless the universe is 101 unique listings.

    Raises ValueError on a duplicate symbol or a wrong count, so a
    hand-edited universe cannot silently drop or add a listing.
    """
    seen_symbols = [symbol for symbol, _ in universe]  # type: ignore[union-attr]
    if len(seen_symbols) != len(set(seen_symbols)):
        raise ValueError("NASDAQ-100 universe contains a duplicate symbol")
    if len(seen_symbols) != EXPECTED_LISTINGS:
        raise ValueError(
            f"NASDAQ-100 universe has {len(seen_symbols)} listings, expected {EXPECTED_LISTINGS}"
        )


def validate_unique(universe: object) -> None:
    """Fail closed unless the universe has no duplicate symbol.

    Unlike :func:`validate`, no count is asserted: a live constituent
    list legitimately differs from 101 after a reconstitution, so the
    collector validates uniqueness only and records the size.
    """
    seen_symbols = [symbol for symbol, _ in universe]  # type: ignore[union-attr]
    if len(seen_symbols) != len(set(seen_symbols)):
        raise ValueError("NASDAQ-100 universe contains a duplicate symbol")


def parse_list_response(payload) -> list[tuple[str, str]]:
    """Parse Nasdaq's list-type/nasdaq100 response into ``(symbol, name)``.

    Rows live at ``data.data.rows`` with ``symbol``, ``companyName`` and
    ``marketCap`` per row (verified live 2026-11-23). Returns the members
    sorted by symbol. Raises ValueError when the response is empty or
    garbled — no ``data.data.rows`` list, no row with a usable symbol —
    so the caller can fall back to the embedded list instead of ever
    publishing an emptied universe. A row without a company name keeps
    its symbol as the name; a duplicate symbol keeps its first row.
    """
    if not isinstance(payload, dict):
        raise ValueError("Nasdaq list response is not an object")
    rows = ((payload.get("data") or {}).get("data") or {}).get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Nasdaq list response carries no constituent rows")
    members: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        symbol = symbol.strip()
        name = (row.get("companyName") or row.get("name")
                or row.get("company_name"))
        if not isinstance(name, str) or not name.strip():
            name = symbol
        members.setdefault(symbol, name.strip())
    if not members:
        raise ValueError("Nasdaq list response carries no usable symbols")
    return sorted(members.items())


def diff_universe(old, new) -> dict[str, list[tuple[str, str]]]:
    """Diff two universes (iterables of ``(symbol, name)``).

    Returns ``{"added": [...], "removed": [...]}``, each a
    symbol-sorted list of ``(symbol, name)`` — ``added`` takes names
    from ``new``, ``removed`` from ``old``. The collector diffs the
    live list against the embedded list, so ``added`` is what entered
    the index and ``removed`` is what left it since the embedded list
    was last verified.
    """
    old_map = dict(old)
    new_map = dict(new)
    added = sorted((s, new_map[s]) for s in new_map if s not in old_map)
    removed = sorted((s, old_map[s]) for s in old_map if s not in new_map)
    return {"added": added, "removed": removed}
