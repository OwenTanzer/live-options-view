"""Volume-profile (Point of Control / Value Area) QQQ options strategy.

Market-profile analysis buckets traded volume by price rather than by time:
plot volume on the y-axis against price on the x-axis over some session and
you get a histogram shaped like a distribution, not a bar chart. The bin
with the most accumulated volume is the **Point of Control (POC)** -- the
price the market spent the most volume agreeing on. Expanding outward from
the POC, bin by bin, until some target share of total volume is captured
(conventionally ~68%, one standard deviation's worth if the distribution
were normal) gives the **Value Area** -- `value_area_low`/`value_area_high`
here -- the range where "most of the trading" happened. Price outside that
range is, by construction, where the market spent comparatively little time
agreeing on value.

This is a **research-grade proxy**, not a real market-profile implementation,
and that distinction matters enough to be explicit about it. True
market/volume profile tooling is typically built from tick-level trade prints
or at least exchange-supplied TPO (time-price-opportunity) letters; what this
module has is free, unauthenticated `yfinance` 1-minute OHLCV bars for QQQ.
Each bar contributes its *entire* volume to a single bucket keyed off its
typical price `(high + low + close) / 3` -- a bar that actually traded across
a wide range within that minute still deposits all of its volume at one
point estimate of where "most" of it happened. With 1-minute bars instead of
tick prints, and `bin_width` defaulting to a coarse $0.25, the resulting
POC/value-area are a reasonable approximation of the shape of intraday value,
not a precise reconstruction of it. Good enough to reason about "is price
inside or outside where most trading has happened," not good enough to be
mistaken for a professional market-profile terminal.

Bars are fetched from `yfinance` at most once every `refresh_interval_s`
(default 300s = 5 minutes) via `VolumeProfileBarReader`, a cache in the same
shape as `market.py`'s `SnapshotReader`: re-fetching on every polling cycle
would just repeatedly ask Yahoo for a dataset that hasn't meaningfully
changed within a few minutes, and 1-minute bars don't need faster-than-that
refresh to stay useful. Any fetch failure, empty response, or an
insufficient number of returned bars is treated exactly like
`trump_whisperer`'s `fetch_error` path: a missing observation, not a neutral
one -- a held position is retained rather than closed, and no new position is
opened on nothing.

Two trading regimes, both intentionally scoped as heuristics without any
claimed edge:

  * **Breakout / momentum continuation**: price is currently outside the
    value area, and the bar series shows it was still inside the value area
    within the last `breakout_lookback_minutes` (default 10) -- i.e. the
    move out is recent, not a long-settled excursion. The bet is that a
    fresh break away from the POC continues: buy a call on a break above,
    a put on a break below.
  * **Re-entry / mean-reversion**: price is currently back inside the value
    area, but the bar series shows it was outside within the same lookback
    window -- a recent excursion that has since reverted. The bet is that
    price continues drifting back toward the POC: buy a call if the POC
    sits above the current price, a put if it sits below.

Neither regime is backed by a walk-forward study in this repo; they encode
the standard textbook market-profile intuitions ("continuation on a fresh
breakout," "reversion on a failed excursion") and nothing more. Whichever
regime does not apply -- price settled inside the value area with no recent
crossing either way -- is `"none"`: no_trade, and any held position that
regime no longer supports gets closed, same "close on loss of support, not
only on a flip" shape every other strategy in this repo uses.

`_decide_core` takes an already-fetched bar list (or a `fetch_error` string)
and contains all the actual decision logic, including the profile/regime
math (`build_profile`, `_detect_regime`); it has no network dependency and is
what `scripts/verify_volume_profile.py` exercises directly with hand-built
bar series. `_decide` is the thin I/O wrapper that pulls bars from the shared
`VolumeProfileBarReader` and calls `_decide_core`. Position management
(one contract at a time, OCC-symbol parsing, close-if-unsupported, ATM/quote
handling) is deliberately identical in shape to `momentum_qqq` and
`trump_whisperer` -- see those modules' docstrings for why this is the right
level of complexity for now; the duplication is intentional isolation, not
an oversight.
"""

from __future__ import annotations

import math
import re
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ..client import Position
from ..market import EXECUTION_QUOTE_MAX_AGE_S
from ..strategy import Decision, StrategyContext, register

STRATEGY_ID = "volume_profile_qqq"
STRATEGY_VERSION = "1.0.0"

DEFAULT_SYMBOL = "QQQ"
DEFAULT_REFRESH_INTERVAL_S = 300.0  # 5 minutes -- see module docstring
DEFAULT_BIN_WIDTH = 0.25
DEFAULT_VALUE_AREA_PCT = 0.68
DEFAULT_BREAKOUT_LOOKBACK_MINUTES = 10.0

# Fewer bars than this and a histogram/value-area is more noise than signal
# (e.g. the first minute of a fresh cache, or a half-populated fetch) --
# treated the same as a fetch error, not as "no signal."
MIN_BARS_REQUIRED = 5

# The bars only cover "recent" relative to *themselves* -- `_detect_regime`
# windows off `bars[-1].timestamp`, not off `ctx.now_et`. If yfinance lags,
# or `period="1d"` hands back the previous session's bars (which happens
# early in a session, or during an outage), nothing here would otherwise
# notice: a stale value area would be built and compared against today's
# live price with no flag to say the bars were old. Same reasoning and
# similar magnitude as momentum_qqq.DEFAULT_MAX_SNAPSHOT_AGE_MINUTES.
DEFAULT_MAX_BAR_AGE_MINUTES = 5.0

# yf.download of a full day of 1-minute bars is a heavier call than a
# fast_info lookup, and can hang or degrade for the same Yahoo-side reasons
# documented in collector.py's fetch_yf_prices_bounded. runner.py runs every
# account's strategy sequentially in a single thread, so an unbounded call
# here stalls every other bot's cycle behind it, not just this one.
DEFAULT_FETCH_TIMEOUT_S = 20.0
DEFAULT_COOLDOWN_S = 300.0

# OCC option symbol: root + YYMMDD + C/P + 8-digit strike. Parsed from the
# symbol itself, not looked up in the current market snapshot -- same
# reasoning as momentum_qqq._OCC_TYPE_RE and trump_whisperer._OCC_TYPE_RE: a
# held position can roll off the front of the chain while still needing to
# be recognized and closed.
_OCC_TYPE_RE = re.compile(r"\d{6}([CP])\d{8}$")


@dataclass(frozen=True)
class Bar:
    """One fetched 1-minute OHLCV bar."""

    timestamp: datetime
    high: float
    low: float
    close: float
    volume: float

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(frozen=True)
class VolumeProfile:
    """POC and value area computed from a volume-weighted price histogram."""

    poc: float
    value_area_low: float
    value_area_high: float
    total_volume: float
    bins: dict[int, float]


def build_profile(
    bars: list[Bar],
    bin_width: float = DEFAULT_BIN_WIDTH,
    value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
) -> VolumeProfile | None:
    """Bucket each bar's typical price into a volume-weighted histogram.

    POC is the bin with the most accumulated volume. The value area expands
    outward from the POC bin one bin at a time, each step adding whichever
    adjacent bin (low side or high side) currently carries more volume, until
    the accumulated share of total volume reaches `value_area_pct`. Ties on
    which side to add go to the high side, arbitrarily but deterministically.
    Returns `None` if there is nothing to build a profile from.
    """

    if not bars or bin_width <= 0:
        return None

    histogram: dict[int, float] = {}
    for bar in bars:
        if bar.volume <= 0:
            continue
        bin_index = math.floor(bar.typical_price / bin_width)
        histogram[bin_index] = histogram.get(bin_index, 0.0) + bar.volume

    if not histogram:
        return None

    total_volume = sum(histogram.values())
    # Tie-break deterministically: highest volume, then lowest price.
    poc_index = max(histogram, key=lambda i: (histogram[i], -i))

    target = total_volume * value_area_pct
    lo = hi = poc_index
    accumulated = histogram[poc_index]

    while accumulated < target:
        lo_vol = histogram.get(lo - 1, 0.0)
        hi_vol = histogram.get(hi + 1, 0.0)
        if lo_vol <= 0.0 and hi_vol <= 0.0:
            break  # no more volume in either direction to add
        if hi_vol >= lo_vol:
            hi += 1
            accumulated += hi_vol
        else:
            lo -= 1
            accumulated += lo_vol

    def bin_center(index: int) -> float:
        return (index + 0.5) * bin_width

    return VolumeProfile(
        poc=bin_center(poc_index),
        value_area_low=bin_center(lo),
        value_area_high=bin_center(hi),
        total_volume=total_volume,
        bins=histogram,
    )


def _classify(price: float, profile: VolumeProfile) -> str:
    if price > profile.value_area_high:
        return "above"
    if price < profile.value_area_low:
        return "below"
    return "inside"


def _detect_regime(
    bars: list[Bar],
    profile: VolumeProfile,
    current_price: float,
    lookback_minutes: float,
) -> tuple[str, str]:
    """Classify the current price against the value area and check freshness.

    Returns `(regime, current_state)` where `current_state` is
    `"above"`/`"below"`/`"inside"` and `regime` is:

      * `"breakout"` -- currently outside the value area, and the bar series
        shows it was inside within the last `lookback_minutes` (a fresh
        crossing out).
      * `"reentry"` -- currently inside the value area, and the bar series
        shows it was outside within the last `lookback_minutes` (a fresh
        crossing back in).
      * `"none"` -- otherwise: settled outside for longer than the lookback
        window, or settled inside with no recent excursion.
    """

    current_state = _classify(current_price, profile)
    if not bars:
        return "none", current_state

    last_ts = bars[-1].timestamp
    window_start = last_ts - timedelta(minutes=lookback_minutes)
    window_states = {_classify(b.typical_price, profile) for b in bars if b.timestamp >= window_start}

    if current_state != "inside":
        if "inside" in window_states:
            return "breakout", current_state
        return "none", current_state

    if "above" in window_states or "below" in window_states:
        return "reentry", current_state
    return "none", current_state


def _fetch_bars_yfinance(symbol: str) -> list[Bar]:
    """Fetch today's 1-minute OHLCV bars for `symbol` via `yfinance`.

    Imported lazily so importing this module (or running its hermetic
    verify script, which never calls this function) never requires the
    `yfinance` package to be importable, let alone to reach the network.
    """

    import yfinance as yf  # noqa: PLC0415

    df = yf.download(symbol, period="1d", interval="1m", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return []

    # Recent yfinance versions return MultiIndex columns (ticker, field)
    # even for a single symbol; flatten down to the field level.
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        try:
            df = df.xs(symbol, axis=1, level=-1)
        except KeyError:
            df.columns = df.columns.get_level_values(0)

    bars: list[Bar] = []
    for ts, row in df.iterrows():
        try:
            high = float(row["High"])
            low = float(row["Low"])
            close = float(row["Close"])
            volume = float(row["Volume"])
        except (KeyError, TypeError, ValueError):
            continue
        timestamp = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        bars.append(Bar(timestamp=timestamp, high=high, low=low, close=close, volume=volume))
    return bars


class VolumeProfileBarReader:
    """Caches fetched 1-minute bars, refreshing at most every `refresh_interval_s`.

    Same cache shape as `market.py`'s `SnapshotReader`: a poll faster than the
    refresh interval just re-serves the cached bars instead of re-hitting
    `yfinance` for a dataset that has not meaningfully changed. `fetch_fn` is
    injectable so `scripts/verify_volume_profile.py` can exercise the cache
    behavior itself without ever invoking `yfinance`.
    """

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        refresh_interval_s: float = DEFAULT_REFRESH_INTERVAL_S,
        fetch_fn: Callable[[str], list[Bar]] | None = None,
        fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
    ):
        self.symbol = symbol
        self.refresh_interval_s = refresh_interval_s
        self._fetch_fn = fetch_fn or _fetch_bars_yfinance
        self.fetch_timeout_s = fetch_timeout_s
        self.cooldown_s = cooldown_s
        self._cached: list[Bar] | None = None
        self._cached_at: float = 0.0
        self._cooldown_until: float = 0.0

    def read(self, force: bool = False) -> list[Bar]:
        age = _time.monotonic() - self._cached_at
        if self._cached is not None and not force and age < self.refresh_interval_s:
            return self._cached

        now = _time.monotonic()
        if now < self._cooldown_until:
            raise RuntimeError(
                f"bar fetch is in cooldown after a prior timeout/failure "
                f"({self._cooldown_until - now:.0f}s remaining)"
            )

        # Bound the fetch with a daemon thread + join(timeout): a
        # hung/slow yf.download() otherwise blocks this call -- and
        # everything the runner schedules after it -- for however long
        # yfinance takes, with no ceiling.
        box: dict[str, Any] = {}

        def _worker() -> None:
            try:
                box["result"] = self._fetch_fn(self.symbol)
            except Exception as exc:  # noqa: BLE001 -- surfaced via box, not re-raised across threads
                box["error"] = exc

        thread = threading.Thread(target=_worker, name="volume-profile-fetch", daemon=True)
        thread.start()
        thread.join(timeout=self.fetch_timeout_s)

        if thread.is_alive():
            self._cooldown_until = _time.monotonic() + self.cooldown_s
            raise RuntimeError(
                f"bar fetch exceeded {self.fetch_timeout_s:.0f}s timeout; "
                f"cooling down for {self.cooldown_s:.0f}s"
            )
        if "error" in box:
            raise box["error"]

        bars = box.get("result", [])
        if not bars:
            self._cooldown_until = _time.monotonic() + self.cooldown_s

        self._cached, self._cached_at = bars, _time.monotonic()
        return self._cached


# Shared across every account running this strategy, deliberately -- there is
# one QQQ bar series, not one per account, exactly like trump_whisperer's
# module-level `_reader` and momentum_qqq's module-level `_tracker`.
_reader = VolumeProfileBarReader()


def _option_type_from_symbol(symbol: str) -> str | None:
    match = _OCC_TYPE_RE.search(symbol)
    if not match:
        return None
    return "call" if match.group(1) == "C" else "put"


def _open_positions(ctx: StrategyContext) -> dict[str, Position]:
    return {symbol: position for symbol, position in ctx.book.positions.items() if position.quantity != 0}


def _decide_core(
    ctx: StrategyContext,
    bars: list[Bar] | None,
    fetch_error: str | None,
) -> Decision:
    def no(reason: str, **meta: Any) -> Decision:
        return Decision.no_trade(
            reason=reason,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            metadata=meta or None,
        )

    def base_meta(
        profile: VolumeProfile | None = None,
        current_price: float | None = None,
        regime: str = "none",
    ) -> dict[str, Any]:
        return dict(
            poc=profile.poc if profile else None,
            value_area_high=profile.value_area_high if profile else None,
            value_area_low=profile.value_area_low if profile else None,
            current_price=current_price,
            regime=regime,
        )

    if ctx.session_phase != "open":
        return no(
            f"Market is {ctx.session_phase}; execution quotes will be stale.",
            session_phase=ctx.session_phase,
            **base_meta(),
        )

    open_positions = _open_positions(ctx)
    if len(open_positions) > 1:
        return no(
            "Holding more than one open option position; standing down "
            "rather than guessing which one this strategy owns.",
            open_positions={s: p.quantity for s, p in open_positions.items()},
            **base_meta(),
        )

    held_symbol: str | None = None
    held_quantity = 0
    held_type: str | None = None
    if open_positions:
        held_symbol, held_position = next(iter(open_positions.items()))
        held_quantity = held_position.quantity
        if held_quantity < 0:
            return no(
                f"Unexpected short position in {held_symbol}; standing down "
                f"rather than compounding it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **base_meta(),
            )
        held_type = _option_type_from_symbol(held_symbol)
        if held_type is None:
            return no(
                f"Held symbol {held_symbol} has an unrecognized option "
                f"type; standing down rather than guessing whether to "
                f"close it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **base_meta(),
            )

    if fetch_error is not None:
        # A fetch failure, empty response, or insufficient bar count is not
        # evidence the profile has changed -- it's an absence of a fresh
        # observation, not an observation of "neutral" or "unsupported."
        # Same reasoning as trump_whisperer's fetch_error branch: retain a
        # held position rather than closing on a missing read.
        if held_symbol is not None:
            return no(
                f"1-minute bar data unavailable ({fetch_error}); retaining "
                f"the held {held_type} position rather than closing on a "
                f"missing observation.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **base_meta(),
            )
        return no(f"1-minute bar data unavailable: {fetch_error}", **base_meta())

    params = ctx.params or {}
    bin_width = params.get("bin_width", DEFAULT_BIN_WIDTH)
    value_area_pct = params.get("value_area_pct", DEFAULT_VALUE_AREA_PCT)
    breakout_lookback_minutes = params.get("breakout_lookback_minutes", DEFAULT_BREAKOUT_LOOKBACK_MINUTES)

    profile = build_profile(bars or [], bin_width=bin_width, value_area_pct=value_area_pct)
    if profile is None:
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            "Could not build a volume profile from the fetched bars.",
            base_meta(),
        )

    current_price = ctx.snapshot.underlying_price
    regime, current_state = _detect_regime(bars or [], profile, current_price, breakout_lookback_minutes)
    meta_base = base_meta(profile, current_price, regime)

    if regime == "none":
        return _maybe_close_unsupported(
            ctx, held_symbol, held_quantity, held_type, no,
            f"Price ({current_price:.2f}) is {current_state} the value area "
            f"[{profile.value_area_low:.2f}, {profile.value_area_high:.2f}] "
            f"with no fresh crossing in the last "
            f"{breakout_lookback_minutes:.0f}m; no signal.",
            meta_base,
        )

    if regime == "breakout":
        supported_type = "call" if current_state == "above" else "put"
        direction_reason = (
            f"Price broke {current_state} the value area "
            f"[{profile.value_area_low:.2f}, {profile.value_area_high:.2f}] "
            f"(POC={profile.poc:.2f}) within the last "
            f"{breakout_lookback_minutes:.0f}m; momentum continuation away "
            f"from POC."
        )
    else:  # regime == "reentry"
        if current_price == profile.poc:
            return _maybe_close_unsupported(
                ctx, held_symbol, held_quantity, held_type, no,
                "Price has re-entered the value area exactly at POC; no "
                "directional edge.",
                meta_base,
            )
        supported_type = "call" if profile.poc > current_price else "put"
        direction_reason = (
            f"Price re-entered the value area "
            f"[{profile.value_area_low:.2f}, {profile.value_area_high:.2f}] "
            f"after a recent excursion; mean-reversion toward "
            f"POC={profile.poc:.2f}."
        )

    if held_symbol is not None:
        if held_type == supported_type:
            return no(
                f"Already holding {held_quantity} {held_symbol}; {regime} "
                f"regime still supports it.",
                symbol=held_symbol,
                held_quantity=held_quantity,
                **meta_base,
            )
        return _close(
            ctx, held_symbol, held_quantity,
            reason=(
                f"{direction_reason} Closing stale {held_type} position "
                f"before considering a new one."
            ),
            meta=meta_base,
            no=no,
        )

    row = ctx.snapshot.atm(supported_type)
    if not row:
        return no(f"No quoted {supported_type} in the snapshot to trade.", **meta_base)
    symbol = row["OptionSymbol"]

    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None:
        return no(f"No live quote returned for {symbol}.", symbol=symbol, **meta_base)
    if not quote.is_executable:
        return no(
            f"Live quote for {symbol} is not executable "
            f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s).",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            age_seconds=quote.age_seconds,
            **meta_base,
        )

    return Decision(
        action="buy",
        symbol=symbol,
        quantity=1,
        reason=f"{direction_reason} Opening one {supported_type}.",
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={
            **meta_base,
            "strike": row["Strike"],
            "bid": quote.bid,
            "ask": quote.ask,
            "quote_age_seconds": quote.age_seconds,
        },
    )


def _maybe_close_unsupported(
    ctx: StrategyContext,
    held_symbol: str | None,
    held_quantity: int,
    held_type: str | None,
    no: Any,
    reason: str,
    meta: dict[str, Any],
) -> Decision:
    if held_symbol is None:
        return no(reason, **meta)
    return _close(
        ctx, held_symbol, held_quantity,
        reason=f"{reason} No longer supports the held {held_type} position; closing it.",
        meta=meta,
        no=no,
    )


def _close(
    ctx: StrategyContext,
    symbol: str,
    quantity: int,
    *,
    reason: str,
    meta: dict[str, Any],
    no: Any,
) -> Decision:
    quote = ctx.quotes([symbol]).get(symbol)
    if quote is None:
        return no(f"No live quote returned for {symbol}; cannot close it this cycle.", symbol=symbol, **meta)
    if not quote.is_executable:
        return no(
            f"Live quote for {symbol} is not executable "
            f"(age={quote.age_seconds}s, limit={EXECUTION_QUOTE_MAX_AGE_S}s); "
            f"cannot close it this cycle.",
            symbol=symbol,
            bid=quote.bid,
            ask=quote.ask,
            age_seconds=quote.age_seconds,
            **meta,
        )
    return Decision(
        action="sell",
        symbol=symbol,
        quantity=quantity,
        reason=reason,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        metadata={**meta, "closing_symbol": symbol},
    )


def _decide(ctx: StrategyContext) -> Decision:
    if ctx.session_phase != "open":
        return _decide_core(ctx, None, None)

    try:
        bars = _reader.read()
    except Exception as exc:  # network failure, yfinance error, etc.
        return _decide_core(ctx, None, str(exc))

    if len(bars) < MIN_BARS_REQUIRED:
        return _decide_core(
            ctx, None,
            f"insufficient bar data ({len(bars)} bars, need {MIN_BARS_REQUIRED})",
        )

    params = ctx.params or {}
    max_bar_age = params.get("max_bar_age_minutes", DEFAULT_MAX_BAR_AGE_MINUTES)
    latest_bar_ts = max(b.timestamp for b in bars)
    bar_age_minutes = (ctx.now_et - latest_bar_ts).total_seconds() / 60.0
    if bar_age_minutes > max_bar_age:
        return _decide_core(
            ctx, None,
            f"latest bar is {bar_age_minutes:.1f} minutes old (limit={max_bar_age}m) "
            f"-- yfinance looks stalled or is serving a prior session",
        )

    return _decide_core(ctx, bars, None)


_decide.strategy_id = STRATEGY_ID
_decide.strategy_version = STRATEGY_VERSION
volume_profile_qqq = register(_decide)
