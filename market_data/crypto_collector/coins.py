"""The 39-coin universe for the crypto collector.

Fixed list, in market-cap rank order, matching the live Coinbase
Price Tracker as verified on 2026-11-23 (getLivePrices, 22:06 UTC):
the tracker's 39 coins -- the 30 it prices from Coinbase plus the 9
it prices from CoinGecko (USDT, USDC, TRX, XMR, LEO, WBT, DAI, CC,
GRAM). It is not the top 39 by market cap: the tracker's Coinbase
set reaches down to rank 85 (ATOM), while the top 39 by market cap
includes coins the tracker does not carry (e.g. RAIN, M, OKB, USDD)
and excludes tracker coins ranked below 39.

Source labels are fixed, not derived at run time:

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
fixed order here is the fallback order and the order the tests assert
the universe in.

All 30 ``coinbase``-labelled coins have a live Coinbase Exchange USD
pair (verified 2026-11-23 22:06 UTC). A row whose source cannot be
read at run time is published as ``source_unavailable`` with an
explicit reason; it is not substituted from the other source and
never silently dropped. Do not silently drop a coin: dropping a coin
changes the 39-coin contract.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Coin:
    """One coin in the fixed universe."""

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
