"""The coin universe for the crypto collector: dynamic top 50.

The universe is resolved live on every run (see
:mod:`market_data.crypto_collector.collector.resolve_universe`):
the CoinGecko markets list (top 100, ordered by market cap) is
re-fetched, tokenized assets are excluded under the tracker's
standing rule (see ``TOKENIZED_EXCLUDED_IDS``), and the first 50
are published, sorted by live market-cap rank. Each entrant's
source is probed live — a Coinbase Exchange USD pair that answers
means ``coinbase``, otherwise ``coingecko``.

``COINS`` below is the embedded fallback universe: the tracker's
39 coins as verified on 2026-11-23 (getLivePrices, 22:06 UTC) —
the 30 it prices from Coinbase plus the 9 it prices from CoinGecko
(USDT, USDC, TRX, XMR, LEO, WBT, DAI, CC, GRAM). It is not the
top 39 by market cap: the tracker's Coinbase set reaches down to
rank 85 (ATOM), while the top 39 by market cap includes coins the
tracker does not carry (e.g. RAIN, M, OKB, USDD) and excludes
tracker coins ranked below 39. The fallback is used only when the
live CoinGecko list is missing or partial — a bad fetch never
shrinks or empties the snapshot — and the fallback is recorded in
the snapshot's ``universe`` block, so a reader can tell a live
universe from a fallback one.

The tracker's first live reconciliation (2026-11-24, CoinGecko
top 100 read the same day) had USDD (rank 51), BFUSD (55) and
HTX (57) entering the top 50 after tokenized exclusion, and
ASTER, DOT and ATOM leaving it; that reconciliation is the
ground truth the dynamic rule reproduces, not a hard-coded list —
the live list is re-read on every run and the embedded list stays
the diff baseline.

Source labels are fixed for the embedded coins, not derived at run
time:

- ``coinbase`` -- priced from the public Coinbase Exchange API
  (``/products/<SYMBOL>-USD/ticker`` and ``/stats``). No account, no
  auth, no Coinbase MCP credential.
- ``coingecko`` -- the 9 top-ranked coins the tracker prices from
  CoinGecko (USDT, USDC, TRX, XMR, LEO, WBT, DAI, CC, GRAM). A
  CoinGecko row is never priced from Coinbase and a Coinbase row is
  never priced from CoinGecko, even if the other source could supply a
  price. See :mod:`market_data.crypto_collector.collector`.

``rank`` is the CoinGecko market-cap rank observed at verification
time (2026-11-23 22:06 UTC). Live ranks are re-read from CoinGecko
on every run and the published snapshot is sorted by live rank; the
embedded order here is the fallback order and the order the tests
assert the fallback universe in.

All 30 ``coinbase``-labelled coins have a live Coinbase Exchange USD
pair (verified 2026-11-23 22:06 UTC). A row whose source cannot be
read at run time is published as ``source_unavailable`` with an
explicit reason; it is not substituted from the other source and
never silently dropped. Do not silently drop a coin: dropping a coin
changes the live 50-coin contract (or the 39-coin fallback contract).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Coin:
    """One coin in the universe (live or embedded fallback)."""

    symbol: str
    name: str
    coingecko_id: str
    # "coinbase" or "coingecko": the only source this coin's price
    # may come from.
    source: str
    # Coinbase Exchange product id, e.g. "BTC-USD". None when the
    # coin is CoinGecko-sourced.
    coinbase_product: str | None
    # CoinGecko market-cap rank at verification time (2026-11-23).
    rank: int


COINS: tuple[Coin, ...] = (
    Coin("BTC", "Bitcoin", "bitcoin", "coinbase", "BTC-USD", 1),
    Coin("ETH", "Ethereum", "ethereum", "coinbase", "ETH-USD", 2),
    Coin("USDT", "Tether", "tether", "coingecko", None, 3),
    Coin("BNB", "BNB", "binancecoin", "coinbase", "BNB-USD", 4),
    Coin("USDC", "USD Coin", "usd-coin", "coingecko", None, 5),
    Coin("XRP", "XRP", "ripple", "coinbase", "XRP-USD", 6),
    Coin("SOL", "Solana", "solana", "coinbase", "SOL-USD", 7),
    Coin("TRX", "TRON", "tron", "coingecko", None, 8),
    Coin("HYPE", "Hyperliquid", "hyperliquid", "coinbase", "HYPE-USD", 10),
    Coin("ZEC", "Zcash", "zcash", "coinbase", "ZEC-USD", 11),
    Coin("XMR", "Monero", "monero", "coingecko", None, 12),
    Coin("DOGE", "Dogecoin", "dogecoin", "coinbase", "DOGE-USD", 13),
    Coin("LEO", "LEO Token", "leo-token", "coingecko", None, 14),
    Coin("WBT", "WhiteBIT Coin", "whitebit", "coingecko", None, 15),
    Coin("LINK", "Chainlink", "chainlink", "coinbase", "LINK-USD", 16),
    Coin("ADA", "Cardano", "cardano", "coinbase", "ADA-USD", 18),
    Coin("XLM", "Stellar", "stellar", "coinbase", "XLM-USD", 20),
    Coin("DAI", "Dai", "dai", "coingecko", None, 21),
    Coin("BCH", "Bitcoin Cash", "bitcoin-cash", "coinbase", "BCH-USD", 22),
    Coin("NEAR", "NEAR Protocol", "near", "coinbase", "NEAR-USD", 23),
    Coin("LTC", "Litecoin", "litecoin", "coinbase", "LTC-USD", 24),
    Coin("CC", "Canton", "canton-network", "coingecko", None, 25),
    Coin("AVAX", "Avalanche", "avalanche-2", "coinbase", "AVAX-USD", 26),
    Coin("UNI", "Uniswap", "uniswap", "coinbase", "UNI-USD", 28),
    # CoinGecko lists this asset under the symbol GRAM (Gram/Toncoin);
    # Coinbase lists TON-USD for the same network token. The tracker
    # prices it from CoinGecko, so this row stays CoinGecko-sourced
    # and is never priced from TON-USD.
    Coin("GRAM", "Gram / Toncoin", "the-open-network", "coingecko", None, 29),
    Coin("SUI", "Sui", "sui", "coinbase", "SUI-USD", 32),
    Coin("HBAR", "Hedera", "hedera-hashgraph", "coinbase", "HBAR-USD", 35),
    Coin("QNT", "Quant", "quant-network", "coinbase", "QNT-USD", 38),
    Coin("TAO", "Bittensor", "bittensor", "coinbase", "TAO-USD", 39),
    Coin("SHIB", "Shiba Inu", "shiba-inu", "coinbase", "SHIB-USD", 40),
    Coin("CRO", "Cronos", "crypto-com-chain", "coinbase", "CRO-USD", 41),
    Coin("AAVE", "Aave", "aave", "coinbase", "AAVE-USD", 50),
    Coin("PEPE", "Pepe", "pepe", "coinbase", "PEPE-USD", 52),
    Coin("ONDO", "Ondo", "ondo-finance", "coinbase", "ONDO-USD", 53),
    Coin("ENA", "Ethena", "ethena", "coinbase", "ENA-USD", 54),
    Coin("WLFI", "World Liberty Financial", "world-liberty-financial", "coinbase", "WLFI-USD", 55),
    Coin("ASTER", "Aster", "aster-2", "coinbase", "ASTER-USD", 59),
    Coin("DOT", "Polkadot", "polkadot", "coinbase", "DOT-USD", 62),
    Coin("ATOM", "Cosmos", "cosmos", "coinbase", "ATOM-USD", 85),
)

# Coins whose fixed source label cannot currently resolve against
# that source. Empty at verification (2026-11-23 22:06 UTC): every
# coin in the universe resolves against its labelled source. Kept as
# data, not dropped, so a reader can tell "known-unavailable" from
# "unknown".
UNRESOLVABLE_AT_VERIFICATION: tuple[str, ...] = ()

COINBASE_COINS: tuple[Coin, ...] = tuple(c for c in COINS if c.source == "coinbase")
COINGECKO_COINS: tuple[Coin, ...] = tuple(c for c in COINS if c.source == "coingecko")

assert len(COINS) == 39, len(COINS)
assert len(COINBASE_COINS) == 30, len(COINBASE_COINS)
assert len(COINGECKO_COINS) == 9, len(COINGECKO_COINS)
assert [c.rank for c in COINS] == sorted(c.rank for c in COINS)


TARGET_COINS = 50

# CoinGecko ids excluded from the live top 50 under the tracker's
# standing rule (tokenized assets: Figure Heloc, Tether Gold,
# tokenized funds / yield coins). Verified against the raw
# CoinGecko top 100 on 2026-11-24: figure-heloc (rank 9),
# tether-gold (28), ondo-us-dollar-yield (34),
# spiko-amundi-overnight-swap-fund-eur (42), hashnote-usyc (43),
# pax-gold (47), blackrock-usd-institutional-digital-liquidity-fund
# (49). New tokenized assets that appear in the top 100 later are
# NOT excluded automatically — this list is the documented rule as
# verified, and it must be extended deliberately when a new one
# appears, so an exclusion is never silent.
TOKENIZED_EXCLUDED_IDS: tuple[str, ...] = (
    "figure-heloc",
    "tether-gold",
    "ondo-us-dollar-yield",
    "spiko-amundi-overnight-swap-fund-eur",
    "hashnote-usyc",
    "pax-gold",
    "blackrock-usd-institutional-digital-liquidity-fund",
)


def parse_top_markets(rows):
    """Parse a CoinGecko ``/coins/markets`` top-100 response.

    Returns the first ``TARGET_COINS`` usable coins after tokenized
    exclusion, as ``[{"symbol", "name", "coingecko_id", "rank"}]``
    in live market-cap rank order. A row is usable only with a
    CoinGecko id, a symbol and a market-cap rank; a duplicate symbol
    keeps its first (highest-ranked) row. Raises ValueError when the
    payload is not a list, or when fewer than ``TARGET_COINS`` usable
    coins survive — a partial list must never shrink the universe,
    so the caller falls back to the embedded list instead.
    """
    if not isinstance(rows, list):
        raise ValueError("CoinGecko top-markets payload is not a list")
    parsed = []
    seen_symbols = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        coin_id = row.get("id")
        symbol = row.get("symbol")
        rank = row.get("market_cap_rank")
        if not isinstance(coin_id, str) or not coin_id.strip():
            continue
        if coin_id in TOKENIZED_EXCLUDED_IDS:
            continue
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            continue
        symbol = symbol.strip().upper()
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            name = symbol
        parsed.append({"symbol": symbol, "name": name.strip(),
                       "coingecko_id": coin_id, "rank": rank})
    parsed.sort(key=lambda entry: entry["rank"])
    if len(parsed) < TARGET_COINS:
        raise ValueError(
            f"CoinGecko top-markets list carries only {len(parsed)} "
            f"usable coins after tokenized exclusion, expected "
            f"{TARGET_COINS}")
    return parsed[:TARGET_COINS]


def coin_from_market_row(entry, source):
    """Build a :class:`Coin` for a live top-50 entry.

    ``entry`` is one :func:`parse_top_markets` dict; ``source`` is
    the probed source (``"coinbase"`` or ``"coingecko"``). The
    tracker symbol for ``the-open-network`` is ``GRAM`` (CoinGecko
    reports the symbol ``gram``; Coinbase lists TON-USD for the same
    network token, which this package never prices from).
    """
    symbol = entry["symbol"]
    name = entry["name"]
    if entry["coingecko_id"] == "the-open-network":
        symbol = "GRAM"
        name = "Gram / Toncoin"
    return Coin(symbol, name, entry["coingecko_id"], source,
                f"{symbol}-USD" if source == "coinbase" else None,
                entry["rank"])


def _change_record(coin):
    return {"symbol": coin.symbol, "name": coin.name,
            "coingecko_id": coin.coingecko_id, "rank": coin.rank,
            "source": coin.source}


def diff_universe(old, new):
    """Diff two universes (iterables of :class:`Coin`).

    Returns ``{"added": [...], "removed": [...]}`` of change records
    (symbol, name, CoinGecko id, live rank, source) — ``added`` from
    ``new``, ``removed`` from ``old`` — each sorted by rank. The
    caller diffs the live list against the embedded list, so
    ``added`` is what entered the top 50 and ``removed`` is what
    left it since the embedded list was last verified.
    """
    old_by_symbol = {coin.symbol: coin for coin in old}
    new_by_symbol = {coin.symbol: coin for coin in new}
    added = sorted((_change_record(new_by_symbol[s]) for s in new_by_symbol
                    if s not in old_by_symbol),
                   key=lambda record: record["rank"])
    removed = sorted((_change_record(old_by_symbol[s]) for s in old_by_symbol
                      if s not in new_by_symbol),
                     key=lambda record: record["rank"])
    return {"added": added, "removed": removed}


def universe_change_records(old, new, as_of):
    """The membership-change block recorded in a snapshot/provenance.

    ``as_of`` is the resolution date (``YYYY-MM-DD``). Empty
    ``added``/``removed`` means the live list matched the embedded
    list exactly.
    """
    changes = diff_universe(old, new)
    return {"as_of": as_of, "added": changes["added"],
            "removed": changes["removed"]}
