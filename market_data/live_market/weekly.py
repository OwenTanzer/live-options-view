"""Weekly snapshots: hourly RTH option-chain archives.

WHY this module: the weekly snapshot is the pipeline's archival record --
one CSV per hourly regular-session slot, per weekly expiration, in the
pipeline's 18-column schema, with a manifest entry per key. It is a
separate rhythm from the minute snapshot (hourly, not every minute; it
archives observed contracts instead of the minute row set). Its test
patch point (_weekly_session_bounds, stubbed by
verify_weekly_collection) resolves through the package entry.
"""

import io
import math
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Optional

import pandas as pd

import market_data.live_market as _entry  # entry indirection for _weekly_snapshot_slot's _weekly_session_bounds patch point

from .config import (
    ET,
    PIPELINE_CHAIN_COLUMNS,
    R2_BUCKET,
    STALE_FEED_SECS,
    STRIKE_WINDOW,
    TICKER,
    WEEKLY_R2_PREFIX,
    WEEKLY_SNAPSHOT_SECS,
    log,
)
from .feed import DXLinkFeed
from .manifest import add_weekly_key, empty_manifest_seed, update_manifest
from .market import _resolve_underlying_spot

@lru_cache(maxsize=32)
def _weekly_session_bounds(day: date) -> Optional[tuple[datetime, datetime]]:
    """Exchange hours, including holidays and early closes; fail closed on errors."""
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (row["market_open"].to_pydatetime().astimezone(ET),
            row["market_close"].to_pydatetime().astimezone(ET))


def _weekly_snapshot_slot(ts_et: datetime) -> Optional[datetime]:
    """Return the hourly RTH slot containing ``ts_et``, or None outside RTH."""
    ts_et = ts_et.astimezone(ET)
    bounds = _entry._weekly_session_bounds(ts_et.date())
    if bounds is None:
        return None
    session_open, session_close = bounds
    if not session_open <= ts_et < session_close:
        return None
    elapsed = (ts_et - session_open).total_seconds()
    return session_open + timedelta(
        seconds=int(elapsed // WEEKLY_SNAPSHOT_SECS) * WEEKLY_SNAPSHOT_SECS
    )


def _weekly_day_prefix(exp_date: str, trade_date: date) -> str:
    expiry = date.fromisoformat(exp_date).strftime("%Y%m%d")
    return f"{WEEKLY_R2_PREFIX}/{expiry}/qqq_chain_{trade_date.strftime('%Y%m%d')}_"


def _weekly_existing_snapshot(s3, exp_date: str, trade_date: date,
                              slot: datetime) -> Optional[str]:
    """Find an already-written observation in this slot after a restart."""
    prefix = _weekly_day_prefix(exp_date, trade_date)
    response = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=prefix)
    for item in response.get("Contents", []):
        key = item.get("Key", "")
        match = re.search(r"_(\d{6,12})\.csv$", key)
        if not match:
            continue
        observed_time = datetime.strptime(match.group(1)[:4], "%H%M").time()
        observed = datetime.combine(trade_date, observed_time)
        observed_et = ET.localize(observed)
        if _weekly_snapshot_slot(observed_et) == slot:
            return key
    return None


def _update_weekly_manifest(s3, exp_date: str, snapshot_key: str) -> None:
    """Index one weekly key under its expiry without touching anyone's field.

    Compare-and-swap on the object's ETag: read, merge only `weekly_expirations`,
    write back with `IfMatch`. A concurrent daily-pipeline write therefore costs
    one re-read, not the new date it just added. Exhaustion raises, and the
    already-uploaded CSV stays discoverable through the prefix listing, so the
    next slot re-derives this index entry.
    """
    update_manifest(
        s3,
        lambda document: add_weekly_key(document, exp_date, snapshot_key),
        seed=empty_manifest_seed(),
    )
    log.info(f"manifest.json indexed {snapshot_key} under {exp_date}")


def _weekly_fresh_value(value, observed_at, now: datetime) -> bool:
    """Only finite nonnegative prices with recent feed observations qualify."""
    try:
        age = (now - datetime.fromisoformat(observed_at)).total_seconds()
        return (not isinstance(value, bool) and math.isfinite(float(value))
                and float(value) >= 0 and 0 <= age <= STALE_FEED_SECS)
    except (TypeError, ValueError, OverflowError):
        return False


def take_weekly_snapshot(s3, feed: DXLinkFeed, strikes: list[dict],
                         exp_date: str, today: date,
                         ts_et: Optional[datetime] = None) -> Optional[str]:
    """Archive observed contracts, retaining the pipeline's 18-column schema.

    Success requires a fresh underlying and at least one fresh, non-crossed
    two-sided option quote. Unquoted/stale contracts are omitted, so partial
    coverage is possible; unknown OI/volume remain blank, observed zero stays
    zero. A slot without qualifying data remains retryable. Summary/Greeks
    fields retain the feed's existing semantics, not new freshness guarantees.
    """
    ts_et = ts_et or datetime.now(ET)
    slot = _weekly_snapshot_slot(ts_et)
    if slot is None:
        return None

    existing_key = _weekly_existing_snapshot(s3, exp_date, today, slot)
    if existing_key:
        _update_weekly_manifest(s3, exp_date, existing_key)
        log.info(f"weekly slot already collected -> {existing_key}")
        return existing_key

    state = feed.get_state()
    qqq = state.get(TICKER, {})
    if (_weekly_fresh_value(qqq.get("bid"), qqq.get("bid_ts"), ts_et)
            and _weekly_fresh_value(qqq.get("ask"), qqq.get("ask_ts"), ts_et)
            and qqq["ask"] >= qqq["bid"]):
        underlying, spot_ts = _resolve_underlying_spot(qqq, None, None)
    else:
        underlying, spot_ts = qqq.get("last"), qqq.get("last_ts")
    if not underlying or not _weekly_fresh_value(underlying, spot_ts, ts_et):
        log.warning("weekly snapshot awaiting fresh underlying data -- slot remains retryable")
        return None
    atm = round(underlying) if underlying else None

    rows = []
    dte = (date.fromisoformat(exp_date) - today).days
    for strike_row in strikes:
        strike = strike_row["strike"]
        if atm is not None and abs(strike - atm) > STRIKE_WINDOW:
            continue
        for option_type, sym_key, occ_key in (
            ("call", "call_sym", "call_occ"),
            ("put",  "put_sym",  "put_occ"),
        ):
            sym = strike_row[sym_key]
            data = state.get(sym, {})
            bid = data.get("bid")
            ask = data.get("ask")
            if not (_weekly_fresh_value(bid, data.get("bid_ts"), ts_et)
                    and _weekly_fresh_value(ask, data.get("ask_ts"), ts_et)
                    and ask >= bid):
                continue
            mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
            rows.append({
                "TradeDate":       today.isoformat(),
                "Expiration":      exp_date,
                "Strike":          strike,
                "Type":            option_type,
                "OptionSymbol":    strike_row[occ_key] or sym,
                "DTE":             dte,
                "OpenInterest":    data.get("oi"),
                "Volume":          data.get("volume"),
                "Bid":             bid,
                "Mid":             mid,
                "Ask":             ask,
                "Last":            data.get("last"),
                "IV":              data.get("volatility"),
                "Delta":           data.get("delta"),
                "Gamma":           data.get("gamma"),
                "Theta":           data.get("theta"),
                "Vega":            data.get("vega"),
                "UnderlyingPrice": underlying,
            })

    if not rows:
        log.warning("weekly snapshot awaiting fresh option quotes -- slot remains retryable")
        return None

    key = (
        _weekly_day_prefix(exp_date, today)
        + ts_et.strftime("%H%M%S%f")
        + ".csv"
    )
    csv_buf = io.StringIO()
    pd.DataFrame(rows, columns=PIPELINE_CHAIN_COLUMNS).to_csv(csv_buf, index=False)
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=csv_buf.getvalue().encode(),
        ContentType="text/csv",
    )
    _update_weekly_manifest(s3, exp_date, key)
    bid_count = sum(1 for row in rows if row["Bid"] is not None)
    log.info(
        f"-> {key}  ({len(rows)} weekly rows, expiration={exp_date}, "
        f"underlying={underlying}, bids={bid_count})"
    )
    return key



__all__ = [
    "_weekly_session_bounds",
    "_weekly_snapshot_slot",
    "_weekly_day_prefix",
    "_weekly_existing_snapshot",
    "_update_weekly_manifest",
    "_weekly_fresh_value",
    "take_weekly_snapshot",
]
