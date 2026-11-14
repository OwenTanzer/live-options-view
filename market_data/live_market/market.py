"""Market compute and the minute snapshot.

WHY this module: take_snapshot is the collector's output -- one minute,
one underlying, one CSV row set plus the live latest.json. Around it sit
the per-underlying market computation (VWAP/RVOL/momentum via
collector.market_signals) and the spot resolver. It reads the session
state, the quote helpers, and the session clock, and nothing else.
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import pandas as pd

from ..overview.readings import build_snapshot_readings, finish_publication, readings_digest
from . import market_signals as ms
from .config import (
    ET,
    MOMENTUM_LOOKBACK_MINUTES,
    MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN,
    MOMENTUM_NEUTRAL_BAND_PCT,
    MOMENTUM_RETAIN_MINUTES,
    R2_BUCKET,
    RVOL_BUCKET_MINUTES,
    RVOL_LOOKBACK_DAYS,
    RVOL_MIN_DAYS_REQUIRED,
    STALE_FEED_SECS,
    STRIKE_WINDOW,
    _session_bounds,
    log,
)
from .feed import DXLinkFeed
from .quotes import _fresh_spy_spot, _observed_spy_price_with_basis, _provider_observed_spy_price, _spy_subscription_coverage
from .state import (
    Counters,
    QQQ_SESSION,
    SnapshotTracker,
    _last_prices,
    _log_momentum_reading,
    _persist_vwap_state,
)

# -- snapshot upload ----------------------------------------------------------

def _compute_underlying_market(s3, qqq: dict, underlying: float | None, spot_ts_str: str | None,
                                ts_et: datetime, ts_utc: datetime, today: date,
                                us: "UnderlyingSession | None" = None,
                                selection: UnderlyingSelection | None = None) -> dict:
    """VWAP/RVOL for the `underlying_market` block of `intraday/latest.json`.

    Approximation, not tick-accurate VWAP: weights the snapshot's own spot
    price by the volume delta since the last snapshot, not by each
    individual trade's own price (see market_signals.py's module docstring).

    `spot_ts_str` must already be paired with `underlying` by the caller
    (see take_snapshot's spot-resolution block) -- this function does not
    re-derive it from `qqq`, so a value that fell back to a non-DXLink source
    can't end up stamped with an unrelated DXLink timestamp.
    """
    us = us or QQQ_SESSION
    us.vwap_state = ms.reset_if_new_session(us.vwap_state, today)

    raw_day_volume = qqq.get("volume")
    spot_observed_at = datetime.fromisoformat(spot_ts_str) if spot_ts_str else None

    us.vwap_state = ms.accumulate_vwap(
        us.vwap_state, price=underlying, raw_day_volume=raw_day_volume, observed_at=spot_observed_at,
    )
    _persist_vwap_state(s3, today.strftime("%Y%m%d"), us)
    vs = us.vwap_state

    bucket = ms.bucket_label(ts_et, RVOL_BUCKET_MINUTES)
    if raw_day_volume is not None:
        us.rvol_today[bucket] = raw_day_volume

    baseline_samples = us.rvol_baseline.get("buckets", {}).get(bucket, {}).get("samples", [])
    rvol = ms.compute_rvol(raw_day_volume, baseline_samples, min_days_required=RVOL_MIN_DAYS_REQUIRED)

    price_vs_vwap_abs = price_vs_vwap_pct = None
    if underlying is not None and vs.vwap:
        price_vs_vwap_abs = round(underlying - vs.vwap, 4)
        price_vs_vwap_pct = round((underlying - vs.vwap) / vs.vwap * 100, 4)

    freshness = ms.classify_freshness(
        inside_session_window=True,  # take_snapshot only ever runs inside the session window
        spot_ts=spot_observed_at, now=ts_utc, stale_after_s=STALE_FEED_SECS,
    )

    session_start, _ = _session_bounds(ts_et)
    vwap_partial_session = ms.is_partial_session(vs, session_start)

    momentum = _compute_and_log_momentum(s3, underlying, spot_observed_at, ts_utc, today, us)

    block = {
        "symbol": us.symbol,
        "spot": underlying,
        "spot_ts": spot_ts_str,
        "vwap": vs.vwap,
        "vwap_ts": vs.vwap_ts,
        "vwap_session_date": vs.session_date.isoformat() if vs.session_date else None,
        "vwap_session_started_at": vs.session_started_at.isoformat() if vs.session_started_at else None,
        "vwap_partial_session": vwap_partial_session,
        "price_vs_vwap_abs": price_vs_vwap_abs,
        "price_vs_vwap_pct": price_vs_vwap_pct,
        "session_volume": raw_day_volume,
        "session_volume_ts": spot_ts_str,
        "rvol": {
            "status": rvol.status,
            "multiple": rvol.multiple,
            "bucket_label": bucket,
            "baseline_volume": rvol.baseline_volume,
            "baseline_days_used": rvol.baseline_days_used,
            "baseline_lookback_days": us.rvol_baseline.get("lookback_days", RVOL_LOOKBACK_DAYS),
            "baseline_updated_through": us.rvol_baseline.get("updated_through"),
        },
        "momentum": momentum,
        "source": "dxlink",
        "freshness": freshness,
    }
    if selection is not None:
        block["provenance"] = {**selection.as_provenance(), "freshness": freshness}
    return block


def _compute_and_log_momentum(
    s3, underlying: float | None, spot_observed_at: datetime | None, ts_utc: datetime, today: date,
    us: "UnderlyingSession | None" = None,
) -> dict:
    """Display/log-only time-series momentum -- see market_signals.py's
    module docstring for why this is a separate computation from
    momentum_qqq's own live trading signal, not a shared one.
    """
    us = us or QQQ_SESSION
    if underlying is not None and spot_observed_at is not None:
        us.momentum_history.append(ms.SpotPoint(observed_at=spot_observed_at, price=underlying))
    us.momentum_history = ms.prune_spot_history(us.momentum_history, ts_utc, MOMENTUM_RETAIN_MINUTES)

    reading = ms.compute_time_series_momentum(
        us.momentum_history, ts_utc,
        lookback_minutes=MOMENTUM_LOOKBACK_MINUTES,
        max_anchor_overshoot_minutes=MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN,
        neutral_band_pct=MOMENTUM_NEUTRAL_BAND_PCT,
    )

    log.info(
        f"momentum {us.symbol}: status={reading.status} return_pct={reading.return_pct} "
        f"direction={reading.direction} sample_count={reading.sample_count}"
    )
    _log_momentum_reading(s3, today.strftime("%Y%m%d"), {
        "ts": ts_utc.isoformat(),
        "status": reading.status,
        "return_pct": reading.return_pct,
        "lookback_minutes": reading.lookback_minutes,
        "anchor_age_minutes": reading.anchor_age_minutes,
        "sample_count": reading.sample_count,
        "direction": reading.direction,
    }, us)

    return {
        "status": reading.status,
        "return_pct": reading.return_pct,
        "lookback_minutes": reading.lookback_minutes,
        "anchor_age_minutes": reading.anchor_age_minutes,
        "sample_count": reading.sample_count,
        "direction": reading.direction,
    }


@dataclass(frozen=True)
class UnderlyingSelection:
    """The underlying value actually selected, with the provenance it earned.

    Every field describes the *selected value*, not the snapshot or the
    publication: `receipt_ts` is the collector receipt of the input that
    produced `price`, and `provider_observed_at` is the provider's own event
    time only when the provider supplied one for that same input. Unknown
    stays `None`; nothing here is inferred from the other clock (#146).
    """

    price: float | None = None
    receipt_ts: str | None = None
    basis: str | None = None
    provider_observed_at: str | None = None
    session: str | None = None
    selection_rule: str | None = None
    unavailable_reason: str | None = None
    contributing: dict[str, Any] = field(default_factory=dict)

    def as_provenance(self) -> dict[str, Any]:
        return {
            "schema_version": "underlying_price.v1",
            "price": self.price,
            "price_basis": self.basis,
            "selection_rule": self.selection_rule,
            "collector_received_at": self.receipt_ts,
            "provider_observed_at": self.provider_observed_at,
            "session": self.session,
            "unavailable_reason": self.unavailable_reason,
            "contributing": dict(self.contributing),
        }


def _read_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _older_observed(*pairs: tuple[str | None, str | None]) -> str | None:
    """Provider event time for a two-sided midpoint: only when both sides
    carry one, and then the older side -- a fresh half-quote must not make an
    old half look observed."""
    if not all(ts for _, ts in pairs):
        return None
    parsed = [(_read_time(ts), ts) for _, ts in pairs]
    if any(dt is None for dt, _ in parsed):
        return None
    return min(parsed, key=lambda item: item[0])[1]


def _finite_price(value: Any) -> float | None:
    """A recorded price only when it is a real finite number; else unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _quote_contributing(qqq: dict) -> dict[str, Any]:
    # `last_price` is the last trade's own price, recorded beside the receipt
    # (`last_ts`) and provider event time (`last_observed_at`) that the feed
    # writes with it in the same Trade/TradeETH event. It is evidence only:
    # selection never reads it, so it cannot change the selected spot, and a
    # missing or non-finite price stays None rather than being inferred.
    return {
        "bid_ts": qqq.get("bid_ts"),
        "ask_ts": qqq.get("ask_ts"),
        "last_ts": qqq.get("last_ts"),
        "bid_observed_at": qqq.get("bid_observed_at"),
        "ask_observed_at": qqq.get("ask_observed_at"),
        "last_observed_at": qqq.get("last_observed_at"),
        "last_price": _finite_price(qqq.get("last")),
    }


def _select_underlying_quote(
    qqq: dict, last_spot_price: float | None, last_spot_ts: str | None,
) -> UnderlyingSelection:
    """Select one spot value and bind its provenance to that same value.

    Order and contract: a two-sided bid/ask midpoint wins, but only when both
    sides and both receipts exist; it is stamped with the *older* contributing
    receipt (the contract SPY and the price strip already use). A fresh ask
    must not rejuvenate an older bid or an old last trade. If a midpoint is
    impossible, the last trade is next (its own receipt and provider event
    time), then the carried-forward spot (a remembered value with its own
    receipt, or none). A remembered spot is never stamped with a feed time,
    and a value that came from a fallback is never presented as live.
    """
    bid, ask = qqq.get("bid"), qqq.get("ask")
    last = qqq.get("last")
    bid_ts, ask_ts, last_ts = qqq.get("bid_ts"), qqq.get("ask_ts"), qqq.get("last_ts")
    contributing = _quote_contributing(qqq)
    contributing["midpoint_rejected"] = None

    if bid is not None and ask is not None:
        times = {"bid": _read_time(bid_ts), "ask": _read_time(ask_ts)}
        if bid_ts and ask_ts and all(times.values()):
            older_side = min(times, key=lambda side: times[side])
            return UnderlyingSelection(
                price=round((bid + ask) / 2, 2),
                receipt_ts=bid_ts if older_side == "bid" else ask_ts,
                basis="bid_ask_midpoint",
                provider_observed_at=_older_observed(
                    ("bid", qqq.get("bid_observed_at")), ("ask", qqq.get("ask_observed_at"))),
                session=None,
                selection_rule="both_sides_required_older_receipt",
                contributing=contributing,
            )
        # Both sides exist but the contributing-leg contract cannot be met:
        # keep the computable midpoint, stamp no receipt, and say why. The
        # value remains usable; freshness will classify as stale, never live.
        contributing["midpoint_rejected"] = (
            "unparseable_side_receipt" if bid_ts and ask_ts else "missing_side_receipt")
        return UnderlyingSelection(
            price=round((bid + ask) / 2, 2),
            receipt_ts=None,
            basis="bid_ask_midpoint_unstamped",
            provider_observed_at=_older_observed(
                ("bid", qqq.get("bid_observed_at")), ("ask", qqq.get("ask_observed_at"))),
            session=None,
            selection_rule="midpoint_without_contributing_receipt",
            contributing=contributing,
        )

    if last is not None:
        return UnderlyingSelection(
            price=last,
            receipt_ts=last_ts,
            basis="last_trade",
            provider_observed_at=qqq.get("last_observed_at"),
            session=qqq.get("last_session"),
            selection_rule="last_trade",
            contributing=contributing,
        )

    if last_spot_price is not None:
        return UnderlyingSelection(
            price=round(last_spot_price, 2),
            receipt_ts=last_spot_ts,
            basis="remembered_spot",
            provider_observed_at=None,
            selection_rule="carried_forward_spot",
            contributing=contributing,
        )

    return UnderlyingSelection(
        selection_rule=None,
        unavailable_reason="no_usable_spot",
        contributing=contributing,
    )


def _resolve_underlying_spot(
    qqq: dict, last_spot_price: float | None, last_spot_ts: str | None,
) -> tuple[float | None, str | None]:
    """Compatibility wrapper: the selected price and its paired receipt.

    The full provenance record is `_select_underlying_quote`, which is what
    `take_snapshot` and `weekly.py` use; this stays for callers (and older
    tests) that only need the pair.
    """
    selection = _select_underlying_quote(qqq, last_spot_price, last_spot_ts)
    return selection.price, selection.receipt_ts


def _snapshot_provenance_record(
    *, symbol: str, csv_key: str, constructed_at: datetime, csv_uploaded_at: datetime,
    selection: UnderlyingSelection, freshness: str, row_count: int, column_count: int,
    publication_status: str, publication_reason: str | None,
    latest_published_at: datetime | None = None,
) -> dict[str, Any]:
    """The durable `snapshot_provenance.v1` record for one archived snapshot.

    Written beside the CSV, keyed by the exact archive key, because the CSV
    itself cannot carry the per-value source/basis/times that the live
    selected-price structure holds (and historical CSVs were written before
    those fields existed). Absent fields mean unknown, never inferred: a CSV
    without this record stays readable and its provenance stays unknown.
    """
    return {
        "schema_version": "snapshot_provenance.v1",
        "snapshot_key": csv_key,
        "symbol": symbol,
        "constructed_at": constructed_at.isoformat(),
        "archive_uploaded_at": csv_uploaded_at.isoformat(),
        "latest_published_at": latest_published_at.isoformat() if latest_published_at else None,
        "publication": {"status": publication_status, "reason": publication_reason},
        "underlying": {**selection.as_provenance(), "freshness": freshness},
        "csv": {"object_key": csv_key, "row_count": row_count, "column_count": column_count},
    }


def _put_snapshot_provenance(s3, record: dict[str, Any]) -> None:
    key = record["snapshot_key"][:-4] + ".provenance.json"
    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key=key,
            Body=json.dumps(record, default=str, allow_nan=False).encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
        log.info(f"-> {key}")
    except Exception as e:
        # The CSV and latest.json already exist; losing the sidecar loses
        # provenance for this snapshot, not the snapshot itself. Record the
        # gap rather than failing a collection cycle.
        log.warning(f"provenance sidecar upload failed (non-fatal): {e}")


def take_snapshot(s3, feed: DXLinkFeed, strikes: list[dict],
                  exp_date: str, tier: str, today: date,
                  counters: Counters, tracker: SnapshotTracker,
                  us: "UnderlyingSession | None" = None):
    us     = us or QQQ_SESSION
    state  = feed.get_state()
    ts_et  = datetime.now(ET)
    ts_utc = datetime.now(timezone.utc)

    quote = state.get(us.symbol, {})
    if us.symbol == "SPY":
        spy_price, spy_ts, spy_basis = _observed_spy_price_with_basis(quote)
        if spy_price is not None:
            # Provider time pairs with the same input that produced the
            # price: a midpoint needs both sides (older wins); a last trade
            # needs its own event time. This is the time the settlement
            # mark is stamped with -- never the collector receipt time.
            _, spy_provider, _ = _provider_observed_spy_price(quote)
            selection = UnderlyingSelection(
                price=spy_price,
                receipt_ts=spy_ts,
                basis=spy_basis,
                provider_observed_at=spy_provider,
                selection_rule="spy_quote_required_older_receipt",
                contributing=_quote_contributing(quote),
            )
        elif us.last_spot[0] is not None:
            selection = UnderlyingSelection(
                price=round(us.last_spot[0], 2), receipt_ts=us.last_spot[1],
                basis="remembered_spot", selection_rule="carried_forward_spot",
                contributing=_quote_contributing(quote),
            )
        else:
            selection = UnderlyingSelection(
                unavailable_reason="no_usable_spy_spot",
                contributing=_quote_contributing(quote),
            )
    else:
        selection = _select_underlying_quote(quote, us.last_spot[0], us.last_spot[1])

    underlying, spot_ts_str = selection.price, selection.receipt_ts
    atm = round(underlying) if underlying else None
    from .settlement_marks import publish_settlement_mark, contributing_provider_times_valid
    # Settlement marks need the PROVIDER's event time
    # (selection.provider_observed_at), not the collector receipt time
    # (spot_ts_str): a fresh receipt of an unknown-age price must stay
    # pending, never become an irreversible mark. Unknown/preclose/stale/
    # future/mixed-side provider evidence stays pending; a remembered spot
    # has no provider time and can never publish. QQQ never publishes.
    # The provider time is the OLDEST contributing leg, which proves the
    # oldest leg isn't stale but hides a future newer leg -- so EVERY
    # contributing provider timestamp must pass the window/now check, not
    # just the oldest one. A valid bid plus a FUTURE ask must not publish.
    contributing = selection.contributing or {}
    if selection.basis in ("bid_ask_midpoint", "bid_ask_midpoint_unstamped"):
        provider_times = [contributing.get("bid_observed_at"), contributing.get("ask_observed_at")]
    elif selection.basis == "last_trade":
        provider_times = [contributing.get("last_observed_at")]
    else:
        provider_times = []
    publish_observed_at = (selection.provider_observed_at
                           if contributing_provider_times_valid(provider_times, exp_date, ts_utc)
                           else None)
    publish_settlement_mark(s3, us.symbol, exp_date, underlying, publish_observed_at, ts_utc)

    underlying_market = _compute_underlying_market(
        s3, quote, underlying, spot_ts_str, ts_et, ts_utc, today, us, selection)

    rows = []
    for s in strikes:
        strike = s["strike"]
        if atm is not None and abs(strike - atm) > STRIKE_WINDOW:
            continue
        for option_type, sym_key, occ_key in (
            ("call", "call_sym", "call_occ"),
            ("put",  "put_sym",  "put_occ"),
        ):
            sym  = s[sym_key]
            data = state.get(sym, {})
            b    = data.get("bid")
            a    = data.get("ask")
            mid  = round((b + a) / 2, 4) if b is not None and a is not None else None
            vol  = data.get("volume", 0) or 0
            vol_delta = max(0, vol - us.prev_vol.get(sym, vol))
            us.prev_vol[sym] = vol
            price_cols = {
                lbl.replace("/", "_"): remembered.get("price")
                for lbl, remembered in _last_prices.items()
            }
            rows.append({
                "TradeDate":       today.isoformat(),
                "Expiration":      exp_date,
                "Strike":          strike,
                "Type":            option_type,
                "OptionSymbol":    s[occ_key],
                "DTE":             (date.fromisoformat(exp_date) - today).days,
                "OpenInterest":    data.get("oi", 0) or 0,
                "Volume":          vol,
                "VolDelta":        vol_delta,
                "Bid":             b,
                "Mid":             mid,
                "Ask":             a,
                "Last":            data.get("last"),
                "IV":              data.get("volatility"),
                "Delta":           data.get("delta"),
                "Gamma":           data.get("gamma"),
                "Theta":           data.get("theta"),
                "Vega":            data.get("vega"),
                "UnderlyingPrice": underlying,
                **price_cols,
            })

    if not rows:
        log.warning(f"{us.symbol} snapshot empty -- state not populated yet")
        return

    bid_count = sum(1 for r in rows if r.get("Bid") is not None)

    df      = pd.DataFrame(rows)
    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False)

    date_str = today.strftime("%Y%m%d")
    time_str = ts_et.strftime("%H%M%S%f")   # microsecond precision prevents overwrite on rapid restart
    csv_key  = f"{us.day_prefix(date_str)}snapshot_{time_str}.csv"

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key=csv_key,
            Body=csv_buf.getvalue().encode(),
            ContentType="text/csv",
        )
        csv_uploaded_at = datetime.now(timezone.utc)
        counters.inc_csv()
        log.info(f"-> {csv_key}  ({len(rows)} rows,  underlying={underlying},  bids={bid_count})")
    except Exception as e:
        log.error(f"CSV upload failed: {e}")
        counters.inc_failure()
        raise

    provenance = _snapshot_provenance_record(
        symbol=us.symbol, csv_key=csv_key, constructed_at=ts_utc,
        csv_uploaded_at=csv_uploaded_at, selection=selection,
        freshness=underlying_market["freshness"], row_count=len(rows),
        column_count=len(df.columns), publication_status="pending",
        publication_reason=None,
    )
    readings = build_snapshot_readings(
        rows=rows, symbol=us.symbol, underlying=underlying, snapshot_key=csv_key,
        snapshot_timestamp=ts_utc.isoformat(), calculated_at=ts_utc,
        freshness=underlying_market["freshness"],
        observed_at=selection.provider_observed_at,
        market=underlying_market,
        parameters={
            "vwap": {"method": "snapshot_weighted_volume_delta",
                     "price_basis": selection.basis},
            "rvol": {"bucket_minutes": RVOL_BUCKET_MINUTES,
                     "lookback_days": RVOL_LOOKBACK_DAYS,
                     "min_days_required": RVOL_MIN_DAYS_REQUIRED},
            "momentum": {"lookback_minutes": MOMENTUM_LOOKBACK_MINUTES,
                         "max_anchor_overshoot_minutes": MOMENTUM_MAX_ANCHOR_OVERSHOOT_MIN,
                         "neutral_band_pct": MOMENTUM_NEUTRAL_BAND_PCT,
                         "neutral_band_use": "display_only"},
        },
        partial_session=bool(underlying_market.get("vwap_partial_session")),
    )
    # Durable per-snapshot VWAP/RVOL/momentum plus the reference readings; the
    # same readings travel in latest.json so a researcher needs no bot, and the
    # digest is what a Crassus decision joins to. The digest is computed after
    # publication status is stamped (see below), so it matches the published
    # envelopes exactly.
    provenance["market"] = underlying_market
    provenance["market_readings"] = readings

    if not us.first_snapshot_written:
        first_key = f"{us.day_prefix(date_str)}first.csv"
        try:
            s3.put_object(
                Bucket=R2_BUCKET, Key=first_key,
                Body=csv_buf.getvalue().encode(),
                ContentType="text/csv",
            )
            us.first_snapshot_written = True
            log.info(f"-> {first_key}  (session-open snapshot, mirrors {csv_key})")
        except Exception as e:
            log.warning(f"first.csv upload failed (non-fatal): {e}")

    if bid_count == 0:
        log.warning(f"{us.latest_key} NOT updated -- no option data (DXLink feed down)")
        provenance["publication"] = {"status": "archived_unpublished", "reason": "no_option_quotes"}
        finish_publication(readings, published_at=None, status="archived_unpublished",
                           reason="no_option_quotes")
        provenance["market_readings_digest"] = readings_digest(readings)
        _put_snapshot_provenance(s3, provenance)
        return

    # The payload is only observable if the write succeeds, so its readings
    # carry the publication attempt time as `published_at`; a failed write
    # leaves no payload and the sidecar records `archived_unpublished`.
    publication_time = datetime.now(timezone.utc)
    finish_publication(readings, published_at=publication_time, status="published")
    readings_sha = readings_digest(readings)
    provenance["market_readings_digest"] = readings_sha

    payload = {
        "symbol":           us.symbol,
        "timestamp":        ts_utc.isoformat(),
        "snapshot_time":    ts_et.strftime("%H:%M ET"),
        "date":             today.isoformat(),
        "expiration":       exp_date,
        "tier":             tier,
        "underlying_price": underlying,
        "snapshot_key":     csv_key,
        "rows":             rows,
        "underlying_market": underlying_market,
        "market_readings":  readings,
        "market_readings_digest": readings_sha,
    }
    if us.symbol != "QQQ":
        payload["subscription_coverage"] = _spy_subscription_coverage(
            us.subscription_coverage, _fresh_spy_spot(quote, ts_utc)[0])

    try:
        s3.put_object(
            Bucket=R2_BUCKET, Key=us.latest_key,
            Body=json.dumps(payload, default=str).encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
        counters.inc_snapshot(ts_utc.isoformat())
        tracker.record()
        provenance["latest_published_at"] = publication_time.isoformat()
        provenance["publication"] = {"status": "published", "reason": None}
        log.info(f"-> {us.latest_key} updated")
    except Exception as e:
        provenance["publication"] = {"status": "archived_unpublished", "reason": "latest_write_failed"}
        finish_publication(readings, published_at=None, status="archived_unpublished",
                           reason="latest_write_failed")
        provenance["market_readings_digest"] = readings_digest(readings)
        _put_snapshot_provenance(s3, provenance)
        log.error(f"latest.json upload failed: {e}")
        counters.inc_failure()
        raise

    _put_snapshot_provenance(s3, provenance)



__all__ = [
    "_compute_underlying_market",
    "_compute_and_log_momentum",
    "_resolve_underlying_spot",
    "_select_underlying_quote",
    "UnderlyingSelection",
    "take_snapshot",
]
