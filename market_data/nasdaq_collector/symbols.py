"""NASDAQ-100 constituent universe (101 listings, 100 companies).

Source: Nasdaq official list-type API
    https://api.nasdaq.com/api/quote/list-type/nasdaq100
fetched 2026-11-23 (API date "Nov 23, 2026", totalrecords 101).
The list is embedded so the collector does not depend on the
constituent endpoint at collection time; re-verify against the
same endpoint after each annual reconstitution (December) or
announced change, and update SOURCE_AS_OF below.

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

