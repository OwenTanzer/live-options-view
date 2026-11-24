"""The 50-coin universe for the crypto candle cache.

Fixed list, in market-cap rank order, matching the live Coinbase
Price Tracker as expanded to the top 50 on 2026-11-23: the tracker's
39 coins (30 priced from Coinbase plus 9 from CoinGecko, see
``market_data/crypto_collector/coins.py``, PR #389) plus the 11 the
tracker added when it expanded to the top 50 — USDS and USD1 from
Coinbase; USDE, RLUSD, USDG, PYUSD, RAIN, MemeCore (M), OKB, Falcon
USD (USDF) and United Stables (U) from CoinGecko.

Ranks are the CoinGecko market-cap ranks observed at verification
time. The 39-coin ranks come from the snapshot collector's
verification (2026-11-23 22:06 UTC); the 11 added coins, and the
ranks of the coins whose rank moved overnight, come from the
parallel Crypto Backend job's top-50 read
(``~/workspace/crypto-backend/top50-2026-11-23.json``,
``last_updated`` 2026-11-24T00:13:48Z — e.g. LINK 15 and WBT 16
there, 16 and 15 in the snapshot collector's read). Ranks drift;
the fixed order here is the fallback order and the order the tests
assert the universe in. Live ranks are not re-read by this package:
a cache file is keyed by coin symbol, not by rank.

Source labels are fixed, not derived at run time, and mirror the
snapshot collector's labels for the 39:

- ``coinbase`` — candles from the public Coinbase Exchange API
  (``/products/<SYMBOL>-USD/candles``). No account, no auth, no
  Coinbase MCP credential. USDS-USD and USD1-USD were both verified
  live on 2026-11-24 (ticker reads 00:24 UTC and 00:17 UTC).
- ``coingecko`` — the coins the tracker prices from CoinGecko.
  A CoinGecko-labelled coin is never priced from Coinbase and a
  Coinbase-labelled coin is never priced from CoinGecko, even if
  the other source could supply a price. The one documented
  exception is the Tiingo backstop (see :mod:`collector`): a named,
  provenance-recorded fallback for history depth, never a silent
  same-day price substitution. The snapshot's fixed-label rule
  still governs the panel's spot prices.

Tokenized assets in the raw CoinGecko top 50 (Figure Heloc, Tether
Gold, tokenized funds/yield coins) stay excluded under the
tracker's standing rule, exactly as in the snapshot collector's
expansion — so this universe is the tracker's 50, not the raw
CoinGecko top 50. The parallel Crypto Backend job's raw top-50
list (``~/workspace/crypto-backend/manifest.json``) carries those
assets and uses the symbol ``TON`` for the-open-network; this
package keeps the tracker's ``GRAM`` symbol for the same asset, as
the snapshot collector does.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Coin:
    """One coin in the fixed universe."""

    symbol: str
    name: str
    coingecko_id: str
    # "coinbase" or "coingecko": the only source this coin's bars
    # may come from (Tiingo backstop excepted, see module docstring).
    source: str
    # Coinbase Exchange product id, e.g. "BTC-USD". None when the
    # coin is CoinGecko-sourced.
    coinbase_product: str | None
    # CoinGecko market-cap rank at verification time (see module
    # docstring for the two verification dates).
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
    Coin("LINK", "Chainlink", "chainlink", "coinbase", "LINK-USD", 15),
    Coin("WBT", "WhiteBIT Coin", "whitebit", "coingecko", None, 16),
    Coin("USDS", "USDS", "usds", "coinbase", "USDS-USD", 17),
    Coin("ADA", "Cardano", "cardano", "coinbase", "ADA-USD", 18),
    Coin("USDE", "Ethena USDe", "ethena-usde", "coingecko", None, 19),
    Coin("XLM", "Stellar", "stellar", "coinbase", "XLM-USD", 20),
    Coin("DAI", "Dai", "dai", "coingecko", None, 21),
    Coin("BCH", "Bitcoin Cash", "bitcoin-cash", "coinbase", "BCH-USD", 22),
    Coin("NEAR", "NEAR Protocol", "near", "coinbase", "NEAR-USD", 23),
    Coin("LTC", "Litecoin", "litecoin", "coinbase", "LTC-USD", 24),
    Coin("CC", "Canton", "canton-network", "coingecko", None, 25),
    Coin("AVAX", "Avalanche", "avalanche-2", "coinbase", "AVAX-USD", 26),
    Coin("UNI", "Uniswap", "uniswap", "coinbase", "UNI-USD", 28),
    Coin("USD1", "USD1", "usd1-wlfi", "coinbase", "USD1-USD", 29),
    # CoinGecko lists this asset under the symbol GRAM (Gram/Toncoin);
    # Coinbase lists TON-USD for the same network token. The tracker
    # prices it from CoinGecko, so this row stays CoinGecko-sourced
    # and is never priced from TON-USD.
    Coin("GRAM", "Gram / Toncoin", "the-open-network", "coingecko", None, 30),
    Coin("RLUSD", "Ripple USD", "ripple-usd", "coingecko", None, 31),
    Coin("SUI", "Sui", "sui", "coinbase", "SUI-USD", 32),
    Coin("USDG", "Global Dollar", "global-dollar", "coingecko", None, 33),
    Coin("HBAR", "Hedera", "hedera-hashgraph", "coinbase", "HBAR-USD", 35),
    Coin("PYUSD", "PayPal USD", "paypal-usd", "coingecko", None, 36),
    Coin("RAIN", "Rain", "rain", "coingecko", None, 37),
    Coin("QNT", "Quant", "quant-network", "coinbase", "QNT-USD", 38),
    Coin("TAO", "Bittensor", "bittensor", "coinbase", "TAO-USD", 39),
    Coin("SHIB", "Shiba Inu", "shiba-inu", "coinbase", "SHIB-USD", 40),
    Coin("CRO", "Cronos", "crypto-com-chain", "coinbase", "CRO-USD", 41),
    Coin("M", "MemeCore", "memecore", "coingecko", None, 44),
    Coin("OKB", "OKB", "okb", "coingecko", None, 45),
    Coin("USDF", "Falcon USD", "falcon-finance", "coingecko", None, 46),
    Coin("U", "United Stables", "united-stables", "coingecko", None, 48),
    Coin("AAVE", "Aave", "aave", "coinbase", "AAVE-USD", 50),
    Coin("PEPE", "Pepe", "pepe", "coinbase", "PEPE-USD", 52),
    Coin("ONDO", "Ondo", "ondo-finance", "coinbase", "ONDO-USD", 53),
    Coin("ENA", "Ethena", "ethena", "coinbase", "ENA-USD", 54),
    Coin("WLFI", "World Liberty Financial", "world-liberty-financial",
         "coinbase", "WLFI-USD", 55),
    Coin("ASTER", "Aster", "aster-2", "coinbase", "ASTER-USD", 59),
    Coin("DOT", "Polkadot", "polkadot", "coinbase", "DOT-USD", 62),
    Coin("ATOM", "Cosmos", "cosmos", "coinbase", "ATOM-USD", 85),
)

COINBASE_COINS: tuple[Coin, ...] = tuple(c for c in COINS if c.source == "coinbase")
COINGECKO_COINS: tuple[Coin, ...] = tuple(c for c in COINS if c.source == "coingecko")

BY_SYMBOL = {c.symbol: c for c in COINS}

EXPECTED_COINS = 50


def names():
    """Symbols in rank order."""
    return [c.symbol for c in COINS]


def symbols():
    """Set of symbols."""
    return set(BY_SYMBOL)


def validate():
    """Fail closed on a wrong count, a duplicate symbol, or a rank inversion."""
    assert len(COINS) == EXPECTED_COINS, len(COINS)
    assert len(BY_SYMBOL) == EXPECTED_COINS, "duplicate symbol"
    assert len(COINBASE_COINS) == 32, len(COINBASE_COINS)
    assert len(COINGECKO_COINS) == 18, len(COINGECKO_COINS)
    assert [c.rank for c in COINS] == sorted(c.rank for c in COINS)
    for coin in COINS:
        assert (coin.coinbase_product is None) == (coin.source == "coingecko"), coin.symbol
